"""The policy that decides whether a finished run may publish itself.

**This is not a publisher.** It creates nothing, uploads nothing, and touches no provider. It
finds candidates, evaluates a fixed list of gates, and — for the ones that pass — calls the
same ``PublishingService.publish`` an admin calls. The only difference between an automatic
publication and a manual one is who decided, which is recorded on the attempt.

Reusing that path rather than writing a second one is the point: the QA gate, the idempotency
key, the atomic claim and the UNKNOWN rule are the reason publishing is safe, and a parallel
implementation would be a parallel set of ways to get them wrong.

**Fail-closed, everywhere.** Autopublish does not mean "a run in READY_TO_PUBLISH should be
published". It means "a run may be published without a human *only* when every condition holds
and can be checked". A gate that cannot be evaluated has not passed. In doubt: do not publish.

**No LLM is involved.** Every gate is a comparison against persisted state.
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

from app.core.settings import (
    AUTOPUBLISH_CEILING_PER_DAY,
    AUTOPUBLISH_CEILING_PER_TICK,
    AUTOPUBLISH_CEILING_QUEUE_BACKLOG,
    VALID_AUTOPUBLISH_PRIVACY,
    settings,
)
from app.models.content_topic import ContentTopic
from app.models.enums import (
    PipelineEventType,
    PipelineState,
    PublishAttemptStatus,
)
from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt
from app.models.publish_target import PublishTarget
from app.publishing.identity import PublisherHeartbeat
from app.publishing.manifest import ManifestUnavailableError
from app.publishing.publish_queue import PublishQueue
from app.services import event_bus
from app.services.automation_service import AutomationConfig
from app.services.autopublish_budget import (
    AutopublishBudget,
    BudgetUnavailableError,
    utc_today,
)
from app.services.publishing_service import PublishingService

logger = logging.getLogger(__name__)

POLICY_VERSION = "autopublish-v1"
INITIATOR = "automatic"

# Every reason a publication was not started, as an explicit code. "blocked" on its own tells
# an operator nothing and makes the difference between a policy pause and a safety refusal
# invisible - and that difference decides whether publishing it by hand is reasonable.
GLOBAL_PUBLISHING_DISABLED = "global_publishing_disabled"
GLOBAL_AUTOPUBLISH_DISABLED = "global_autopublish_disabled"
TOPIC_AUTOMATION_DISABLED = "topic_automation_disabled"
TOPIC_AUTOPUBLISH_DISABLED = "topic_autopublish_disabled"
TARGET_NOT_CONFIGURED = "publish_target_not_configured"
TARGET_UNKNOWN = "publish_target_unknown"
TARGET_INACTIVE = "target_inactive"
TARGET_DISCONNECTED = "target_disconnected"
TARGET_AUTOPUBLISH_DISABLED = "target_autopublish_disabled"
NOT_READY = "job_not_ready_to_publish"
REVIEW_REQUIRED = "review_required"
PUBLICATION_INELIGIBLE = "publication_ineligible"
ELIGIBILITY_MISSING = "publication_eligibility_missing"
UNRESOLVED_ATTEMPT = "unresolved_attempt"
PREVIOUS_FINAL_FAILURE = "previous_final_failure"
OPERATOR_CANCELED = "operator_canceled"
ALREADY_PUBLISHED = "already_published"
IN_FLIGHT = "publication_in_flight"
HISTORICAL = "historical_before_autopublish_cutoff"
DAILY_LIMIT = "daily_limit_reached"
PER_RUN_LIMIT = "per_run_limit_reached"
PUBLISHER_UNAVAILABLE = "publisher_unavailable"
QUEUE_BACKPRESSURE = "publish_queue_backpressure"
DEAD_LETTER_BACKPRESSURE = "publish_dead_letter_backpressure"
PUBLIC_DISABLED = "public_autopublish_disabled"
PRIVACY_INVALID = "privacy_invalid"
# Another replica is allocating. Not a refusal - the work is still eligible, and the next
# tick will find it.
BUDGET_LOCKED = "budget_locked"
# Nothing left to publish for this run: every media item is already accounted for.
NOTHING_OUTSTANDING = "nothing_outstanding"
# The run's required set cannot be established, so nothing may be allocated against it.
MANIFEST_UNAVAILABLE = "publication_manifest_unavailable"

# Reasons that are a *policy* pause, not a safety refusal. A run blocked for one of these may
# still be published by a human — the system is declining to act on its own, not declaring
# the output unfit. Everything not listed here is a safety refusal, and publishing it by hand
# would be overriding a gate rather than exercising judgement.
POLICY_ONLY_REASONS = frozenset(
    {
        GLOBAL_AUTOPUBLISH_DISABLED,
        TOPIC_AUTOPUBLISH_DISABLED,
        TARGET_AUTOPUBLISH_DISABLED,
        TARGET_NOT_CONFIGURED,
        HISTORICAL,
        DAILY_LIMIT,
        PER_RUN_LIMIT,
        PUBLIC_DISABLED,
        PUBLISHER_UNAVAILABLE,
        QUEUE_BACKPRESSURE,
        DEAD_LETTER_BACKPRESSURE,
        BUDGET_LOCKED,
    }
)


@dataclass
class Candidate:
    """One run considered for automatic publication."""

    pipeline_job_id: str
    topic_id: str | None
    target_id: str | None
    status: str
    reasons: list[str] = field(default_factory=list)
    queued_media: list[str] = field(default_factory=list)
    attempt_ids: list[str] = field(default_factory=list)
    # How many media items this run had left, and how many the budget let it take. Reported
    # so a partial allocation is visible rather than looking like a complete one.
    outstanding: int = 0
    allowance: int = 0
    # Required items this tick could not take because the budget ran out. Reported so a
    # deliberately paced large run is visible as progress rather than as a shortfall.
    deferred: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline_job_id": self.pipeline_job_id,
            "topic_id": self.topic_id,
            "publish_target_id": self.target_id,
            "status": self.status,
            "reasons": self.reasons,
            "queued_media": self.queued_media,
            "attempt_ids": self.attempt_ids,
            "outstanding_media": self.outstanding,
            "allowance": self.allowance,
            "deferred_budget": self.deferred,
            # Said explicitly, because it is the question an operator asks next: may I
            # publish this by hand, or is the system telling me it is not fit to publish?
            "manual_publish_still_allowed": all(
                reason in POLICY_ONLY_REASONS for reason in self.reasons
            ) if self.reasons else True,
        }


@dataclass
class AutopublishReport:
    autopublish_run_id: str
    dry_run: bool
    status: str = "noop"
    considered: int = 0
    eligible: int = 0
    queued: int = 0
    blocked: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    blocked_reasons: dict[str, int] = field(default_factory=dict)
    daily_used: int = 0
    daily_remaining: int = 0
    budget_date: str | None = None
    publisher_workers_alive: int = 0
    queue_backlog: int = 0
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "autopublish_run_id": self.autopublish_run_id,
            "dry_run": self.dry_run,
            "status": self.status,
            "considered": self.considered,
            "eligible": self.eligible,
            "queued": self.queued,
            "blocked": self.blocked,
            "blocked_reasons": self.blocked_reasons,
            "daily_used": self.daily_used,
            "daily_remaining": self.daily_remaining,
            "budget_date": self.budget_date,
            "publisher_workers_alive": self.publisher_workers_alive,
            "queue_backlog": self.queue_backlog,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "duration_ms": self.duration_ms,
        }

    def record(self, candidate: Candidate) -> None:
        self.candidates.append(candidate)
        if candidate.status == "queued":
            # Counted in MEDIA ITEMS, not candidates. Before PR-AUTONOMY-HARDEN-01 this was
            # `+= 1` per run, so a job with three clips spent one unit of a cap of one and
            # published three videos - the per-tick cap said one and meant "one job".
            self.queued += max(1, len(candidate.queued_media))
        elif candidate.status == "blocked":
            self.blocked += 1
            for reason in candidate.reasons:
                self.blocked_reasons[reason] = self.blocked_reasons.get(reason, 0) + 1


class AutonomousPublicationService:
    def __init__(
        self,
        publishing: PublishingService | None = None,
        queue: PublishQueue | None = None,
        heartbeat_reader=None,
        clock=None,
    ) -> None:
        self.publishing = publishing or PublishingService()
        # Injected so the day-boundary behaviour is testable without waiting for midnight.
        self.clock = clock
        self.queue = queue or self.publishing.queue
        # Injected so a test can describe the fleet without a live Redis.
        self._heartbeat_reader = heartbeat_reader or (
            lambda: PublisherHeartbeat.alive(self.queue.redis)
        )

    # ---------------------------------------------------------------------- run

    def run(
        self,
        db: Session,
        *,
        topic: ContentTopic | None = None,
        dry_run: bool = True,
        limit: int | None = None,
        automation_run_id: str | None = None,
        actor: str | None = None,
    ) -> AutopublishReport:
        """Evaluate the policy and, unless this is a dry run, queue what passes."""
        started = time.monotonic()
        report = AutopublishReport(
            autopublish_run_id=str(uuid.uuid4()), dry_run=dry_run
        )

        per_tick = _clamp(
            limit if limit is not None else settings.autopublish_max_per_tick,
            0, AUTOPUBLISH_CEILING_PER_TICK,
        )
        per_day = _clamp(settings.autopublish_max_per_day, 0, AUTOPUBLISH_CEILING_PER_DAY)

        budget = AutopublishBudget(db, limit=per_day, clock=self.clock)
        report.budget_date = budget.date.isoformat()
        report.daily_used = budget.used()
        report.daily_remaining = max(0, per_day - report.daily_used)
        report.publisher_workers_alive = len(self._workers())
        depths = self._depths()
        report.queue_backlog = depths["ready"] + depths["processing"] + depths["delayed"]

        # Gates that stop the whole run rather than one candidate. Evaluated before any
        # database work, so a disabled installation does no queries at all.
        blocked_globally = self._global_gates(report, depths)
        if blocked_globally:
            report.status = "blocked"
            report.blocked_reasons = {reason: 1 for reason in blocked_globally}
            report.duration_ms = _ms(started)
            self._emit_run(db, report, "autopublish.blocked", automation_run_id,
                           event_type=PipelineEventType.WARNING)
            self._log(report, automation_run_id, blocked_globally)
            return report

        candidates = self._ready_jobs(db, topic=topic)
        report.considered = len(candidates)

        if dry_run:
            # A dry run reads the budget and spends none of it, so it takes no lock: holding
            # one would let a preview stall a real allocation for no reason.
            self._evaluate_all(db, candidates, budget=budget, per_tick=per_tick,
                               dry_run=True, report=report,
                               automation_run_id=automation_run_id, actor=actor)
        else:
            try:
                with budget.hold():
                    self._evaluate_all(db, candidates, budget=budget, per_tick=per_tick,
                                       dry_run=False, report=report,
                                       automation_run_id=automation_run_id, actor=actor)
            except BudgetUnavailableError as exc:
                # Another replica is allocating. Skipping is strictly safer than proceeding
                # without the lock, and the work stays eligible for the next tick.
                report.status = "blocked"
                report.blocked_reasons = {BUDGET_LOCKED: 1}
                report.duration_ms = _ms(started)
                logger.info("autopublish_budget_busy", extra={"detail": str(exc)})
                self._emit_run(db, report, "autopublish.blocked", automation_run_id,
                               event_type=PipelineEventType.WARNING)
                return report

        # Re-read after allocating, so the report states what is true now rather than what
        # was true before this run spent part of it.
        report.daily_used = budget.used()
        report.daily_remaining = max(0, per_day - report.daily_used)

        report.eligible = sum(
            1 for c in report.candidates if c.status in ("queued", "would_queue")
        )
        report.status = self._status(report)
        report.duration_ms = _ms(started)

        self._emit_run(db, report, "autopublish.completed", automation_run_id)
        self._log(report, automation_run_id, [])
        return report

    def _evaluate_all(
        self,
        db: Session,
        candidates: list[PipelineJob],
        *,
        budget: AutopublishBudget,
        per_tick: int,
        dry_run: bool,
        report: AutopublishReport,
        automation_run_id: str | None,
        actor: str | None,
    ) -> None:
        """Walk the candidates, spending at most what both caps allow.

        Both caps count **media items**, not runs. Before this PR they counted candidates, so
        a run with three clips spent one unit of a cap of one and published three videos.
        """
        for job in candidates:
            spent = report.queued
            tick_left = max(0, per_tick - spent)
            if tick_left <= 0:
                report.record(Candidate(
                    pipeline_job_id=str(job.id), topic_id=_str(job.topic_id),
                    target_id=None, status="blocked", reasons=[PER_RUN_LIMIT],
                ))
                continue

            if dry_run:
                day_left = max(0, budget.limit - budget.used())
            else:
                # Recomputed under the lock, after everything this run has already created.
                day_left = budget.allocatable(tick_left)
            if day_left <= 0:
                report.record(Candidate(
                    pipeline_job_id=str(job.id), topic_id=_str(job.topic_id),
                    target_id=None, status="blocked", reasons=[DAILY_LIMIT],
                ))
                continue

            report.record(
                self._evaluate(
                    db, job, dry_run=dry_run, report=report,
                    automation_run_id=automation_run_id, actor=actor,
                    allowance=min(tick_left, day_left), budget=budget,
                )
            )

    # ------------------------------------------------------------ global gates

    def _global_gates(self, report: AutopublishReport, depths: dict[str, int]) -> list[str]:
        """Conditions under which nothing may be published automatically at all."""
        blocked: list[str] = []

        if not settings.publishing_enabled:
            blocked.append(GLOBAL_PUBLISHING_DISABLED)
        if not settings.autopublish_enabled:
            blocked.append(GLOBAL_AUTOPUBLISH_DISABLED)

        # No publisher is running. Creating commands now would build a queue nobody drains,
        # and by the time one starts the backlog could be a day's worth of uploads arriving
        # at once. Manual publishing keeps working - a human watching a request is a
        # different situation from a loop firing unattended.
        if report.publisher_workers_alive == 0:
            blocked.append(PUBLISHER_UNAVAILABLE)

        backlog_ceiling = _clamp(
            settings.autopublish_max_queue_backlog, 0, AUTOPUBLISH_CEILING_QUEUE_BACKLOG
        )
        if report.queue_backlog >= backlog_ceiling:
            blocked.append(QUEUE_BACKPRESSURE)

        # Not zero-tolerance: one ancient dead-lettered command must not be able to stop
        # publishing forever, so it takes a real pile before automation pauses.
        if depths["dead"] >= max(1, settings.autopublish_max_dead_letter):
            blocked.append(DEAD_LETTER_BACKPRESSURE)

        if report.daily_remaining <= 0:
            blocked.append(DAILY_LIMIT)

        return blocked

    # ------------------------------------------------------------- candidates

    def _ready_jobs(self, db: Session, *, topic: ContentTopic | None) -> list[PipelineJob]:
        """Runs that finished, cleared the technical gate, and are waiting.

        Ordered by when they first became publishable, ascending: deterministic, derived from
        persisted state, and fair - the run that has waited longest goes first rather than
        whatever the database happened to return.
        """
        query = db.query(PipelineJob).filter(
            PipelineJob.state == PipelineState.READY_TO_PUBLISH
        )
        if topic is not None:
            query = query.filter(PipelineJob.topic_id == topic.id)

        jobs = query.all()
        return sorted(jobs, key=_first_ready_at)

    # ---------------------------------------------------------------- one job

    def _evaluate(
        self,
        db: Session,
        job: PipelineJob,
        *,
        dry_run: bool,
        report: AutopublishReport,
        automation_run_id: str | None,
        actor: str | None,
        allowance: int,
        budget: AutopublishBudget,
    ) -> Candidate:
        candidate = Candidate(
            pipeline_job_id=str(job.id), topic_id=_str(job.topic_id),
            target_id=None, status="blocked",
        )

        # ---- the run itself -------------------------------------------------
        if job.state == PipelineState.REVIEW_REQUIRED:
            # Belt and braces: the query already excludes it. Stated again because
            # "REVIEW_REQUIRED is never published automatically" is an invariant, and an
            # invariant enforced only by a WHERE clause is one refactor from being gone.
            candidate.reasons.append(REVIEW_REQUIRED)
            return candidate
        if job.state != PipelineState.READY_TO_PUBLISH:
            candidate.reasons.append(NOT_READY)
            return candidate

        metadata = job.metadata_json or {}
        eligibility = metadata.get("publication_eligibility")
        if not isinstance(eligibility, dict) or not eligibility:
            # Fail-closed. An unmeasured gate is not a passed gate.
            candidate.reasons.append(ELIGIBILITY_MISSING)
            return candidate
        if not eligibility.get("eligible"):
            candidate.reasons.append(PUBLICATION_INELIGIBLE)
            return candidate

        # ---- the topic ------------------------------------------------------
        topic = job.topic
        if topic is None:
            candidate.reasons.append(TARGET_NOT_CONFIGURED)
            return candidate

        config = AutomationConfig.from_topic(topic)
        if not config.enabled:
            candidate.reasons.append(TOPIC_AUTOMATION_DISABLED)
            return candidate
        if not config.autopublish_enabled:
            candidate.reasons.append(TOPIC_AUTOPUBLISH_DISABLED)
            return candidate
        if not config.publish_target_id:
            # Never "the first active YouTube target": that rule changes meaning silently the
            # day a second channel is connected, and the failure is a video on the wrong
            # channel, which cannot be taken back.
            candidate.reasons.append(TARGET_NOT_CONFIGURED)
            return candidate

        target = (
            db.query(PublishTarget)
            .filter(PublishTarget.id == _as_uuid(config.publish_target_id))
            .first()
        )
        if target is None:
            candidate.reasons.append(TARGET_UNKNOWN)
            return candidate
        candidate.target_id = str(target.id)

        # ---- the target -----------------------------------------------------
        if not target.is_active:
            candidate.reasons.append(TARGET_INACTIVE)
            return candidate
        if target.connection_status.value == "reconnect_required" or (
            not target.refresh_token_encrypted
        ):
            candidate.reasons.append(TARGET_DISCONNECTED)
            return candidate
        if not target.autopublish_enabled:
            candidate.reasons.append(TARGET_AUTOPUBLISH_DISABLED)
            return candidate

        # ---- the backlog cutoff ---------------------------------------------
        cutoff = _latest(
            _as_utc(target.autopublish_enabled_at),
            _parse(config.autopublish_enabled_at),
        )
        ready_at = _first_ready_at(job)
        if cutoff is not None and ready_at < cutoff:
            # Enabling automation must not publish everything that was already waiting.
            # These stay publishable by hand, which is where that decision belongs.
            candidate.reasons.append(HISTORICAL)
            return candidate

        # ---- privacy --------------------------------------------------------
        privacy = self._privacy_for(target)
        if privacy not in VALID_AUTOPUBLISH_PRIVACY:
            candidate.reasons.append(PRIVACY_INVALID)
            return candidate
        if privacy == "public" and not settings.autopublish_public_enabled:
            # A target default of `public` cannot route around the global guard: this is
            # checked on the resolved value, after the target's preference is applied.
            candidate.reasons.append(PUBLIC_DISABLED)
            return candidate

        # ---- existing publications ------------------------------------------
        blocking = self._attempt_gate(db, job, target)
        if blocking:
            candidate.reasons.append(blocking)
            return candidate

        # ---- how much of this run may be published now ----------------------
        #
        # A run can produce several clips, and each is one unit of budget. Resolving them
        # here - rather than letting the publish command take all of them - is what makes
        # both caps exact for multi-clip runs.
        try:
            outstanding = self._outstanding_media(db, job, target)
        except ManifestUnavailableError as exc:
            # Fail-closed: without a required set there is nothing to allocate against.
            logger.warning(
                "autopublish_manifest_unavailable",
                extra={"pipeline_job_id": str(job.id), "detail": str(exc)},
            )
            candidate.reasons.append(MANIFEST_UNAVAILABLE)
            return candidate
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "autopublish_media_unreadable",
                extra={"pipeline_job_id": str(job.id), "error_type": type(exc).__name__},
            )
            candidate.reasons.append("media_unavailable")
            return candidate

        if not outstanding:
            # Every media item is accounted for. Which of the two reasons applies matters to
            # an operator: one means the run is finished, the other that it is mid-flight.
            unsettled = self._has_unsettled_attempt(db, job, target)
            candidate.reasons.append(IN_FLIGHT if unsettled else NOTHING_OUTSTANDING)
            return candidate

        # ---- as much of the run as the budget allows --------------------------
        #
        # Partial allocation, restored now that completion is outstanding-aware. Until
        # PR-PUBLISH-COMPLETE-01 this had to take a whole run or none of it: publishing three
        # clips of five would move the run out of READY_TO_PUBLISH and the publisher would
        # settle it to PUBLISHED, silently dropping the other two. The evaluator now knows
        # what the run still owes, so an unfinished run returns to READY_TO_PUBLISH and its
        # remaining clips are allocated on later ticks.
        #
        # The consequence that matters: a run with more clips than the daily cap now makes
        # progress across days instead of never becoming eligible at all.
        selection = sorted(outstanding)[: max(0, allowance)]
        if not selection:
            candidate.reasons.append(DAILY_LIMIT)
            candidate.outstanding = len(outstanding)
            candidate.allowance = allowance
            return candidate

        candidate.deferred = len(outstanding) - len(selection)

        candidate.allowance = len(selection)
        candidate.outstanding = len(outstanding)

        # ---- act ------------------------------------------------------------
        if dry_run:
            candidate.status = "would_queue"
            candidate.reasons = []
            candidate.queued_media = [f"video_index={index}" for index in selection]
            return candidate

        # The same command an admin issues. Preflight runs again inside it and is the
        # authority: everything above is candidate selection, and the run's state may have
        # changed since the query.
        result = self.publishing.publish(
            db,
            job=job,
            target=target,
            dry_run=False,
            overrides={"privacy": privacy},
            # Bounded to what the budget allowed. Without this the command publishes every
            # outstanding clip, and a cap of one becomes a cap of one *job*.
            media_selection=selection,
            actor=actor or "autopublish",
            initiator=INITIATOR,
            budget_date=budget.date,
            provenance={
                "policy_version": POLICY_VERSION,
                "autopublish_run_id": report.autopublish_run_id,
                "automation_run_id": automation_run_id,
                "topic_id": _str(job.topic_id),
            },
        )

        queued = [item for item in result.items if item.status in ("queued", "pending_enqueue")]
        candidate.queued_media = [item.media_identity for item in queued]
        candidate.attempt_ids = [item.attempt_id for item in queued if item.attempt_id]

        if queued:
            candidate.status = "queued"
            self._emit_candidate(db, job, target, candidate, "autopublish.queued",
                                 report, automation_run_id)
        else:
            candidate.status = "blocked"
            candidate.reasons = sorted(
                {reason for item in result.items for reason in item.blocked_by}
                or set(result.blocked_by)
            ) or ["nothing_to_publish"]
            self._emit_candidate(db, job, target, candidate, "autopublish.blocked",
                                 report, automation_run_id,
                                 event_type=PipelineEventType.WARNING)
        return candidate

    @staticmethod
    def _has_unsettled_attempt(db: Session, job: PipelineJob, target: PublishTarget) -> bool:
        return (
            db.query(PublishAttempt.id)
            .filter(
                PublishAttempt.pipeline_job_id == job.id,
                PublishAttempt.target_id == target.id,
                PublishAttempt.status.in_(
                    [PublishAttemptStatus.PENDING, PublishAttemptStatus.IN_PROGRESS]
                ),
            )
            .first()
            is not None
        )

    def _outstanding_media(
        self, db: Session, job: PipelineJob, target: PublishTarget
    ) -> list[int]:
        """Video indexes this run still owes on this target.

        Read from the manifest rather than from the artifact each time: the manifest is the
        run's required set, fixed before anything was published, so an item cannot quietly
        stop being required because a later re-render changed the package.

        An item carrying an attempt of ANY kind is excluded. Succeeded ones are done;
        retrying, unresolved, blocked and cancelled ones are somebody else's decision, and
        creating a second logical publication for them is precisely what must not happen.
        """
        manifest = self.publishing.manifests.resolve(db, job)
        taken = {
            identity
            for (identity,) in db.query(PublishAttempt.media_identity).filter(
                PublishAttempt.pipeline_job_id == job.id,
                PublishAttempt.target_id == target.id,
            )
        }
        return [
            item.video_index
            for item in manifest.ordered()
            if item.media_identity not in taken
        ]

    # ------------------------------------------------------------- sub-policies

    @staticmethod
    def _privacy_for(target: PublishTarget) -> str:
        """The privacy an automatic publication would use.

        Target preference, else the system default. Never inferred from the content, and
        never read from ``publish_package.json`` - a distribution decision is not editorial
        metadata, and letting the render pipeline choose it would put "who can see this"
        downstream of an LLM.
        """
        configured = (target.config_json or {}).get("default_privacy")
        return str(configured or settings.autopublish_default_privacy).strip().lower()

    @staticmethod
    def _attempt_gate(db: Session, job: PipelineJob, target: PublishTarget) -> str | None:
        """Whether existing attempts forbid starting anything new for this run.

        Deliberately whole-run rather than per media item. Publishing service already
        deduplicates per item, so a partially published run is handled correctly by simply
        calling it - the outstanding clips are queued and the finished ones are not touched.
        What must stop the run entirely is an *unresolved* publication, because "a video may
        already exist and nobody knows" is not a state to add more publications on top of.
        """
        attempts = (
            db.query(PublishAttempt)
            .filter(
                PublishAttempt.pipeline_job_id == job.id,
                PublishAttempt.target_id == target.id,
            )
            .all()
        )
        if not attempts:
            return None

        if any(attempt.needs_human for attempt in attempts):
            # The invariant this whole system is built around, applied one level up: never
            # create a replacement for a publication whose outcome is unknown.
            return UNRESOLVED_ATTEMPT
        if any(a.status == PublishAttemptStatus.FAILED_FINAL for a in attempts):
            # Would fail identically. Recreating it every tick would be a loop that spends
            # quota to learn nothing; a human has to change something first.
            return PREVIOUS_FINAL_FAILURE
        if any(a.status == PublishAttemptStatus.CANCELED for a in attempts):
            # An operator veto. Automation does not overrule it.
            return OPERATOR_CANCELED
        # Deliberately NOT blocking on PENDING or IN_PROGRESS. Once allocation became
        # per-media (PR-AUTONOMY-HARDEN-01), a whole-run block on those would mean a run that
        # got one clip published under a tight budget could never publish the rest: the clip
        # in flight would block its own siblings forever. Per-item deduplication is handled
        # by `_outstanding_media`, and the publish command re-checks each item anyway.
        return None

    def _published_today(self, db: Session, limit: int = 0) -> int:
        """Kept as a thin read for callers that only want the number.

        Delegates to the budget so there is one query, used by both the read model and the
        enforcement path — a second implementation is how a status page starts disagreeing
        with the thing it reports on.
        """
        return AutopublishBudget(db, limit=limit, clock=self.clock).used()

    # ------------------------------------------------------------------ status

    def status(self, db: Session) -> dict[str, Any]:
        """The read model: what the policy would do right now, and why."""
        depths = self._depths()
        per_day = _clamp(settings.autopublish_max_per_day, 0, AUTOPUBLISH_CEILING_PER_DAY)
        # The same object and the same query the enforcement path uses, so the number an
        # operator reads cannot disagree with the number that decides.
        budget = AutopublishBudget(db, limit=per_day, clock=self.clock)
        snapshot = budget.snapshot()
        workers = self._workers()

        ready = db.query(func.count(PipelineJob.id)).filter(
            PipelineJob.state == PipelineState.READY_TO_PUBLISH
        ).scalar() or 0
        unresolved = db.query(func.count(PublishAttempt.id)).filter(
            PublishAttempt.status.in_(
                [PublishAttemptStatus.UNKNOWN,
                 PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION]
            )
        ).scalar() or 0

        return {
            "publishing_enabled": settings.publishing_enabled,
            "autopublish_enabled": settings.autopublish_enabled,
            "public_enabled": settings.autopublish_public_enabled,
            "default_privacy": settings.autopublish_default_privacy,
            "policy_version": POLICY_VERSION,
            "max_per_tick": _clamp(
                settings.autopublish_max_per_tick, 0, AUTOPUBLISH_CEILING_PER_TICK
            ),
            "daily_cap": snapshot["daily_limit"],
            "daily_used": snapshot["daily_used"],
            "daily_remaining": snapshot["daily_remaining"],
            "budget_date": snapshot["budget_date"],
            "publisher_workers_alive": len(workers),
            "publisher_workers": [w.get("worker_id") for w in workers],
            "queue": depths,
            "queue_backlog": depths["ready"] + depths["processing"] + depths["delayed"],
            "ready_jobs": int(ready),
            "unresolved_attempts": int(unresolved),
            "automatic_publications_total": int(
                db.query(func.count(PublishAttempt.id))
                .filter(PublishAttempt.initiator == INITIATOR)
                .scalar() or 0
            ),
        }

    # ----------------------------------------------------------------- helpers

    def _workers(self) -> list[dict[str, Any]]:
        try:
            return self._heartbeat_reader() or []
        except Exception:  # noqa: BLE001
            # Unreadable liveness is not "everything is fine": it resolves to no workers,
            # which blocks automatic publication rather than assuming a fleet exists.
            logger.warning("autopublish_liveness_unreadable")
            return []

    def _depths(self) -> dict[str, int]:
        try:
            return self.queue.depths()
        except Exception:  # noqa: BLE001
            # An unreachable queue reads as saturated, so the policy pauses instead of
            # creating commands it cannot see.
            logger.warning("autopublish_queue_unreadable")
            return {
                "ready": AUTOPUBLISH_CEILING_QUEUE_BACKLOG,
                "processing": 0, "delayed": 0, "dead": 0,
            }

    @staticmethod
    def _status(report: AutopublishReport) -> str:
        if report.queued:
            return "completed"
        if report.considered == 0:
            # Nothing was waiting. A working system is idle most of the time.
            return "noop"
        if report.blocked:
            return "blocked"
        return "noop"

    def _emit_run(
        self,
        db: Session,
        report: AutopublishReport,
        stage: str,
        automation_run_id: str | None,
        *,
        event_type: PipelineEventType = PipelineEventType.INFO,
    ) -> None:
        if report.dry_run:
            # A dry run changes nothing, so it leaves no trace in the run history.
            return
        try:
            event_bus.publish_event(
                db,
                service="autopublish",
                event_type=event_type,
                pipeline_job_id=None,
                stage=stage,
                message=f"autopublish {report.status}",
                payload={
                    "autopublish_run_id": report.autopublish_run_id,
                    "automation_run_id": automation_run_id,
                    "policy_version": POLICY_VERSION,
                    "considered": report.considered,
                    "queued": report.queued,
                    "blocked": report.blocked,
                    "blocked_reasons": report.blocked_reasons,
                    "daily_remaining": report.daily_remaining,
                    "publisher_workers_alive": report.publisher_workers_alive,
                    "queue_backlog": report.queue_backlog,
                },
                commit=True,
            )
        except Exception:  # noqa: BLE001
            logger.warning("autopublish_event_failed",
                           extra={"autopublish_run_id": report.autopublish_run_id})

    def _emit_candidate(
        self,
        db: Session,
        job: PipelineJob,
        target: PublishTarget,
        candidate: Candidate,
        stage: str,
        report: AutopublishReport,
        automation_run_id: str | None,
        *,
        event_type: PipelineEventType = PipelineEventType.INFO,
    ) -> None:
        try:
            event_bus.publish_event(
                db,
                service="autopublish",
                event_type=event_type,
                pipeline_job_id=job.id,
                stage=stage,
                message=f"autopublish {candidate.status} for job {job.id}",
                payload={
                    "pipeline_job_id": str(job.id),
                    "publish_target_id": str(target.id),
                    "topic_id": candidate.topic_id,
                    "autopublish_run_id": report.autopublish_run_id,
                    "automation_run_id": automation_run_id,
                    "policy_version": POLICY_VERSION,
                    "reason": candidate.reasons,
                    "attempt_ids": candidate.attempt_ids,
                },
                commit=True,
            )
        except Exception:  # noqa: BLE001
            logger.warning("autopublish_event_failed",
                           extra={"pipeline_job_id": str(job.id)})

    @staticmethod
    def _log(report: AutopublishReport, automation_run_id: str | None,
             blocked_globally: list[str]) -> None:
        logger.info(
            "autopublish_run",
            extra={
                "autopublish_run_id": report.autopublish_run_id,
                "automation_run_id": automation_run_id,
                "dry_run": report.dry_run,
                "status": report.status,
                "ready_considered": report.considered,
                "eligible": report.eligible,
                "queued": report.queued,
                "blocked": report.blocked,
                "blocked_globally": blocked_globally,
                "daily_remaining": report.daily_remaining,
                "publisher_workers_alive": report.publisher_workers_alive,
                "queue_backlog": report.queue_backlog,
                "duration_ms": report.duration_ms,
            },
        )


# --------------------------------------------------------------------- helpers


def _first_ready_at(job: PipelineJob) -> datetime:
    """When this run FIRST became publishable.

    ``finished_at`` is refreshed when a failed publication releases a run back to
    READY_TO_PUBLISH, so it cannot anchor a historical cutoff - a month-old run would look
    new. The state machine records ``first_ready_at`` once and never moves it; ``finished_at``
    is the fallback for runs that predate that.
    """
    recorded = (job.metadata_json or {}).get("first_ready_at")
    parsed = _parse(recorded)
    if parsed is not None:
        return parsed
    return _as_utc(job.finished_at) or datetime.min.replace(tzinfo=timezone.utc)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes where PostgreSQL returns aware ones."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _latest(*values: datetime | None) -> datetime | None:
    """The strictest cutoff wins: a topic and a target may both have one."""
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _as_uuid(value: Any) -> Any:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return value


def _str(value: Any) -> str | None:
    return str(value) if value else None


def _clamp(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(number, high))


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
