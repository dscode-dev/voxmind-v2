"""The autonomous loop: discovery, then selection, then admission, for one topic.

    AutonomousPipelineService
        ├── DiscoveryService     "what content exists?"
        ├── SelectionService     "what is worth producing?"
        └── AdmissionService     "can we start it now?"

This is a **coordinator, not an engine**. It owns no ranking, no dedup, no capacity arithmetic
and no idempotency key — every one of those already lives in the service that owns the
question, and duplicating any of them here would create a second place for the rules to drift.
What it owns is the order, the stage switches, and what to do when a stage fails.

**Stages fail independently.** The instinct is to abort the run when discovery fails, and it is
wrong: an exhausted YouTube quota says nothing about the candidates already sitting in
SELECTED waiting for a worker slot. Refusing to admit them because an unrelated provider is
down would turn one degraded source into a full production stop. So each stage is attempted on
its own, and the run's status reflects what actually happened rather than the first thing that
went wrong.

**Explicit calls, not an event chain.** Discovery does not publish an event that selection
listens for. A chain like that scatters the control flow across three files and makes a run
impossible to follow in a stack trace; a caller that invokes A, then B, then C can be read
top to bottom and stepped through in a debugger.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.content_topic import ContentTopic
from app.models.enums import PipelineEventType, VideoCandidateStatus
from app.models.video_candidate import VideoCandidate
from app.selection.engine import SelectionEngine
from app.selection.semantic import build_evaluator
from app.services import event_bus
from app.services.admission_service import ProductionAdmissionService
from app.services.discovery_service import build_default_service
from app.services.selection_service import SelectionService

logger = logging.getLogger(__name__)

# Run outcomes.
COMPLETED = "completed"   # every enabled stage ran
PARTIAL = "partial"       # at least one stage failed, at least one succeeded
FAILED = "failed"         # every enabled stage failed
NOOP = "noop"             # everything ran and there was simply nothing to do
SKIPPED = "skipped"       # the run did not start
RUNNING = "running"       # the run has begun and no stage has reported yet

# Stage outcomes.
OK = "ok"
STAGE_FAILED = "failed"
DISABLED = "disabled"
STAGE_SKIPPED = "skipped"

# Ceilings. Automation config is edited by hand and a typo must not become a runaway.
HARD_MAX_SELECTION_LIMIT = 25
HARD_MAX_ADMISSION_LIMIT = 10
HARD_MAX_PENDING_SWEEP = 10
# One topic cannot start more publications in a tick than this, whatever it asks for.
HARD_MAX_AUTOPUBLISH_LIMIT = 5

# Config keys that are text, not numbers. Everything else is coerced to bool or int.
_TEXT_FIELDS = frozenset({"publish_target_id", "autopublish_enabled_at"})
MIN_INTERVAL_MINUTES = 5


@dataclass(frozen=True)
class AutomationConfig:
    """Per-topic automation settings, read from ``ContentTopic.metadata_json["automation"]``.

    Policy lives beside the editorial intention it serves, not in environment variables: a
    topic that should be discovered hourly and one discovered daily are an editorial
    difference, not a deployment one.
    """

    enabled: bool = False
    interval_minutes: int = 60

    discovery_enabled: bool = True
    selection_enabled: bool = True
    admission_enabled: bool = True

    # Off by default, and off for every topic that already exists. Enabling automation for a
    # topic is a separate editorial decision from enabling it globally: a channel may be
    # discovering and producing happily for weeks before anyone is ready to let it publish.
    autopublish_enabled: bool = False
    # How many publications this topic may start in one tick. Independent of the global
    # per-tick cap, which bounds the whole system rather than one topic.
    autopublish_limit: int = 1

    selection_limit: int = 3
    admission_limit: int = 1

    # How many candidates may sit in SELECTED awaiting admission before selection pauses.
    # Without it, a topic with three worker slots happily accumulates a thousand selected
    # candidates that will never all be produced — and the oldest go stale while it grows.
    max_selected_backlog: int = 10

    # Backoff after repeated whole-run failures.
    failure_backoff_minutes: int = 30
    max_consecutive_failures: int = 5

    # Which channel this topic publishes to, as a PublishTarget id.
    #
    # Required for automation and deliberately not inferred. "The first active YouTube
    # target" would be a rule that silently changes meaning the day a second channel is
    # connected - and the failure mode is a video on the wrong channel, which cannot be
    # taken back. No target configured means no automatic publication.
    publish_target_id: str | None = None
    # ISO-8601. Set when a topic's automation is switched on, and compared against when a
    # run first became publishable, so turning this on does not publish the backlog.
    autopublish_enabled_at: str | None = None

    @classmethod
    def from_topic(cls, topic: ContentTopic) -> "AutomationConfig":
        raw = ((topic.metadata_json or {}).get("automation")) or {}
        config = cls()
        if not isinstance(raw, dict):
            return config

        fields: dict[str, Any] = {name: getattr(config, name) for name in cls.__dataclass_fields__}
        for key, value in raw.items():
            if key not in fields or value is None:
                continue
            if key in _TEXT_FIELDS:
                # Kept as text rather than pushed through the int coercion below, which
                # would silently drop a target id and leave automation pointing nowhere.
                text = str(value).strip()
                fields[key] = text or None
                continue
            current = fields[key]
            try:
                fields[key] = bool(value) if isinstance(current, bool) else int(value)
            except (TypeError, ValueError):
                # A malformed value falls back to the default rather than crashing a tick or
                # silently reshaping the schedule.
                continue

        fields["interval_minutes"] = max(MIN_INTERVAL_MINUTES, fields["interval_minutes"])
        fields["selection_limit"] = max(0, min(fields["selection_limit"], HARD_MAX_SELECTION_LIMIT))
        fields["admission_limit"] = max(0, min(fields["admission_limit"], HARD_MAX_ADMISSION_LIMIT))
        fields["max_selected_backlog"] = max(0, fields["max_selected_backlog"])
        fields["autopublish_limit"] = max(
            0, min(fields["autopublish_limit"], HARD_MAX_AUTOPUBLISH_LIMIT)
        )
        return cls(**fields)

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass
class StageResult:
    name: str
    status: str = STAGE_SKIPPED
    counts: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    run_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "counts": self.counts,
            "reasons": self.reasons,
            "run_id": self.run_id,
        }


@dataclass
class AutomationRunReport:
    automation_run_id: str
    topic_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str = SKIPPED
    skip_reason: str | None = None
    discovery: StageResult = field(default_factory=lambda: StageResult("discovery"))
    selection: StageResult = field(default_factory=lambda: StageResult("selection"))
    admission: StageResult = field(default_factory=lambda: StageResult("admission"))
    publication: StageResult = field(default_factory=lambda: StageResult("publication"))
    pending_enqueue_recovered: int = 0
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "automation_run_id": self.automation_run_id,
            "topic_id": self.topic_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "status": self.status,
            "skip_reason": self.skip_reason,
            "discovery": self.discovery.as_dict(),
            "selection": self.selection.as_dict(),
            "admission": self.admission.as_dict(),
            "publication": self.publication.as_dict(),
            "pending_enqueue_recovered": self.pending_enqueue_recovered,
            "duration_ms": self.duration_ms,
        }


class AutonomousPipelineService:
    """Runs one topic through the loop. Knows the order; owns none of the rules."""

    def __init__(
        self,
        *,
        discovery=None,
        selection: SelectionService | None = None,
        admission: ProductionAdmissionService | None = None,
        publication=None,
    ) -> None:
        self.discovery = discovery or build_default_service(
            settings.youtube_api_key,
            timeout_sec=settings.discovery_http_timeout_sec,
            max_results=settings.discovery_max_results,
            freshness_days=settings.discovery_freshness_days,
        )
        self.selection = selection or SelectionService(
            engine=SelectionEngine(
                evaluator=build_evaluator(
                    settings.selection_openai_api_key,
                    model=settings.selection_model,
                    timeout_sec=settings.selection_timeout_sec,
                )
            )
        )
        self.admission = admission or ProductionAdmissionService()
        # Built lazily: importing the publication policy at module scope would make the
        # scheduler depend on the publishing package at import time, and the boundary this
        # PR is careful about is exactly that one.
        self._publication = publication

    def run_topic(
        self,
        db: Session,
        *,
        topic: ContentTopic,
        config: AutomationConfig | None = None,
        now: datetime | None = None,
        automation_run_id: str | None = None,
        actor: str | None = None,
    ) -> AutomationRunReport:
        """Discovery, selection, admission — each attempted, none allowed to abort the rest."""
        now = now or datetime.now(timezone.utc)
        config = config or AutomationConfig.from_topic(topic)
        started = time.monotonic()

        report = AutomationRunReport(
            automation_run_id=automation_run_id or str(uuid.uuid4()),
            topic_id=str(topic.id),
            started_at=now,
        )

        # Said explicitly, because the report's own default is SKIPPED: a run that has begun
        # is running, and an event claiming "skipped" at the moment work starts would be read
        # by an operator as the exact opposite of what happened.
        report.status = RUNNING
        self._emit(db, topic, report, "automation.started", PipelineEventType.INFO)

        self._discovery_stage(db, topic, config, report)
        self._selection_stage(db, topic, config, report)
        self._admission_stage(db, topic, config, report, actor)
        # Last, and working on a different set of runs: whatever admission just produced is
        # minutes of GPU time away from being publishable, so this stage acts on the backlog
        # of runs that finished in earlier ticks. It never waits for a publication.
        self._publication_stage(db, topic, config, report, actor)

        report.finished_at = datetime.now(timezone.utc)
        report.duration_ms = int((time.monotonic() - started) * 1000)
        report.status = self._resolve_status(report)

        self._emit(
            db, topic, report,
            f"automation.{report.status}",
            PipelineEventType.ERROR if report.status == FAILED
            else PipelineEventType.WARNING if report.status == PARTIAL
            else PipelineEventType.INFO,
        )
        self._log(report)
        return report

    # ------------------------------------------------------------------ stages

    def _discovery_stage(
        self, db: Session, topic: ContentTopic, config: AutomationConfig, report: AutomationRunReport
    ) -> None:
        if not config.discovery_enabled:
            report.discovery.status = DISABLED
            return
        try:
            results = self.discovery.run_topic(db, topic=topic, commit=True)
        except Exception as exc:  # noqa: BLE001 — one bad source must not end the run
            report.discovery.status = STAGE_FAILED
            # The message is not carried: a provider that interpolates its request into an
            # exception would put an API key into a stored event.
            report.discovery.reasons = [type(exc).__name__]
            logger.exception(
                "automation_discovery_crashed",
                extra={"automation_run_id": report.automation_run_id, "topic_id": str(topic.id)},
            )
            return

        errors = [
            error.get("error_type")
            for result in results
            for error in (result.errors or [])
        ]
        report.discovery.counts = {
            "sources": len(results),
            "results_received": sum(r.results_received for r in results),
            "new_candidates": sum(r.new_candidates for r in results),
            "existing_candidates": sum(r.existing_candidates for r in results),
        }
        report.discovery.reasons = sorted({str(e) for e in errors if e})
        # A source that failed is a degraded discovery, not a failed one, as long as another
        # source produced something. Only losing every source is a stage failure.
        succeeded = [r for r in results if r.status in ("completed", "partial")]
        if results and not succeeded:
            report.discovery.status = STAGE_FAILED
        else:
            report.discovery.status = OK

    def _selection_stage(
        self, db: Session, topic: ContentTopic, config: AutomationConfig, report: AutomationRunReport
    ) -> None:
        if not config.selection_enabled or config.selection_limit <= 0:
            report.selection.status = DISABLED
            return

        backlog = self._selected_backlog(db, topic)
        if backlog >= config.max_selected_backlog:
            # Backpressure. Selecting more when nothing can be admitted only grows a queue of
            # candidates that go stale before their turn.
            report.selection.status = STAGE_SKIPPED
            report.selection.reasons = ["selected_backlog_limit"]
            report.selection.counts = {
                "selected_backlog": backlog, "max_selected_backlog": config.max_selected_backlog
            }
            return

        # Never select past the backlog cap, even when the limit would allow it.
        headroom = max(0, config.max_selected_backlog - backlog)
        limit = min(config.selection_limit, headroom)

        try:
            result = self.selection.run(db, topic=topic, limit=limit, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            report.selection.status = STAGE_FAILED
            report.selection.reasons = [type(exc).__name__]
            logger.exception(
                "automation_selection_crashed",
                extra={"automation_run_id": report.automation_run_id, "topic_id": str(topic.id)},
            )
            return

        report.selection.status = OK
        report.selection.run_id = result.run_id
        report.selection.counts = {
            "considered": result.outcome.considered,
            "eligible": result.outcome.eligible,
            "selected": len(result.outcome.selected),
            "blocked": len(result.outcome.blocked),
            "selected_backlog_before": backlog,
            "limit_applied": limit,
        }

    def _admission_stage(
        self,
        db: Session,
        topic: ContentTopic,
        config: AutomationConfig,
        report: AutomationRunReport,
        actor: str | None,
    ) -> None:
        if not config.admission_enabled or config.admission_limit <= 0:
            report.admission.status = DISABLED
            return
        try:
            result = self.admission.run(
                db, topic=topic, limit=config.admission_limit, dry_run=False, actor=actor
            )
        except Exception as exc:  # noqa: BLE001
            report.admission.status = STAGE_FAILED
            report.admission.reasons = [type(exc).__name__]
            logger.exception(
                "automation_admission_crashed",
                extra={"automation_run_id": report.automation_run_id, "topic_id": str(topic.id)},
            )
            return

        payload = result.as_dict()
        report.admission.status = OK
        report.admission.run_id = result.run_id
        report.admission.counts = {
            **payload["counts"],
            "selected_waiting": payload["selected_waiting"],
            "active_jobs": payload["active_jobs"],
            "available_slots": payload["available_slots"],
        }
        if payload["counts"]["temporarily_blocked"] and not payload["counts"]["admitted"]:
            # A full worker pool is a normal outcome, not a fault.
            report.admission.reasons = ["capacity_blocked"]

    # ------------------------------------------------------------- maintenance

    def recover_pending_enqueue(
        self, db: Session, *, limit: int = HARD_MAX_PENDING_SWEEP
    ) -> int:
        """Re-dispatch admissions that persisted but never reached the queue.

        Bounded per tick on purpose: an unattended sweep against a queue that is still down is
        a republish storm, and the admission key already makes each attempt harmless rather
        than urgent.
        """
        try:
            decisions = self.admission.retry_pending_enqueue(
                db, limit=max(1, min(limit, HARD_MAX_PENDING_SWEEP))
            )
        except Exception:  # noqa: BLE001
            logger.exception("automation_pending_sweep_crashed")
            return 0
        return sum(1 for decision in decisions if decision.outcome == "admitted")

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _selected_backlog(db: Session, topic: ContentTopic) -> int:
        return (
            db.query(func.count(VideoCandidate.id))
            .filter(
                VideoCandidate.topic_id == topic.id,
                VideoCandidate.status == VideoCandidateStatus.SELECTED,
            )
            .scalar()
            or 0
        )

    def _publication_stage(
        self,
        db: Session,
        topic: ContentTopic,
        config: AutomationConfig,
        report: AutomationRunReport,
        actor: str | None,
    ) -> None:
        """Ask the publication policy whether anything of this topic may publish itself.

        The scheduler does not publish. It does not know what YouTube is, holds no
        credential, and calls no adapter - it calls one application service that evaluates
        policy and, at most, queues a command. The upload happens later, in the publisher
        process, and this tick does not wait for it.
        """
        if not config.autopublish_enabled:
            report.publication.status = DISABLED
            return

        try:
            result = self.publication.run(
                db,
                topic=topic,
                dry_run=False,
                limit=config.autopublish_limit,
                automation_run_id=report.automation_run_id,
                actor=actor or "scheduler",
            )
        except Exception as exc:  # noqa: BLE001
            # Isolated like every other stage: a publication problem must not cost this tick
            # the discovery and selection it already did. The message is dropped and only the
            # type kept, for the same reason as the stages above.
            report.publication.status = STAGE_FAILED
            report.publication.reasons = [type(exc).__name__]
            logger.exception(
                "automation_publication_crashed",
                extra={"automation_run_id": report.automation_run_id,
                       "topic_id": str(topic.id)},
            )
            return

        report.publication.status = OK
        report.publication.run_id = result.autopublish_run_id
        report.publication.counts = {
            "considered": result.considered,
            "queued": result.queued,
            "blocked": result.blocked,
            "daily_remaining": result.daily_remaining,
            "publisher_workers_alive": result.publisher_workers_alive,
        }
        report.publication.reasons = sorted(result.blocked_reasons)

    @property
    def publication(self):
        if self._publication is None:
            from app.services.autopublish_service import AutonomousPublicationService

            self._publication = AutonomousPublicationService()
        return self._publication

    @staticmethod
    def _resolve_status(report: AutomationRunReport) -> str:
        stages = [report.discovery, report.selection, report.admission, report.publication]
        attempted = [s for s in stages if s.status in (OK, STAGE_FAILED)]
        failed = [s for s in stages if s.status == STAGE_FAILED]

        if not attempted:
            return SKIPPED
        if failed and len(failed) == len(attempted):
            return FAILED
        if failed:
            return PARTIAL

        # Everything ran and nothing happened. That is a correct, quiet run — a topic with no
        # new content is not an error, and reporting it as one would train operators to ignore
        # the alarm.
        produced = (
            report.discovery.counts.get("new_candidates", 0)
            or report.selection.counts.get("selected", 0)
            or report.admission.counts.get("admitted", 0)
            or report.publication.counts.get("queued", 0)
            or report.pending_enqueue_recovered
        )
        return COMPLETED if produced else NOOP

    def _emit(
        self,
        db: Session,
        topic: ContentTopic,
        report: AutomationRunReport,
        stage: str,
        event_type: PipelineEventType,
    ) -> None:
        payload = report.as_dict()
        event_bus.publish_event(
            db,
            service="automation",
            event_type=event_type,
            # Automation is not a PipelineJob. A tick, a discovery and a selection are not
            # production runs; only admission creates one.
            pipeline_job_id=None,
            stage=stage,
            message=f"{stage} for '{topic.name}'",
            payload={
                "automation_run_id": report.automation_run_id,
                "topic_id": report.topic_id,
                "status": report.status,
                "discovery": payload["discovery"]["status"],
                "selection": payload["selection"]["status"],
                "admission": payload["admission"]["status"],
                "new_candidates": report.discovery.counts.get("new_candidates"),
                "selected": report.selection.counts.get("selected"),
                "admitted": report.admission.counts.get("admitted"),
                "pending_enqueue_recovered": report.pending_enqueue_recovered,
                "duration_ms": report.duration_ms,
            },
        )

    @staticmethod
    def _log(report: AutomationRunReport) -> None:
        logger.info(
            "automation_run",
            extra={
                "automation_run_id": report.automation_run_id,
                "topic_id": report.topic_id,
                "status": report.status,
                "discovery_status": report.discovery.status,
                "discovered_new": report.discovery.counts.get("new_candidates", 0),
                "selection_status": report.selection.status,
                "selected": report.selection.counts.get("selected", 0),
                "admission_status": report.admission.status,
                "admitted": report.admission.counts.get("admitted", 0),
                "active_jobs": report.admission.counts.get("active_jobs"),
                "pending_enqueue_recovered": report.pending_enqueue_recovered,
                "duration_ms": report.duration_ms,
            },
        )
