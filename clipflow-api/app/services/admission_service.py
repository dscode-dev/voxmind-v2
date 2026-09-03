"""Production admission: the boundary where a selected candidate becomes real work.

Selection answered *"should we produce this?"*. Admission answers *"can we start it now?"* —
a different question with different inputs. A candidate can be the best one available and
still not be admissible: the workers may be saturated, the day's budget spent, or the run may
already exist. Ranking has nothing to say about any of that.

    VideoCandidate SELECTED
            ↓  admission checks   ← status, availability, capacity, idempotency
            ↓  PipelineJob        ← created QUEUED, carrying a frozen snapshot
            ↓  enqueue            ← the payload reaches Redis
    VideoCandidate CONSUMED       ← only after the handoff is real

**The ordering is the design.** The row is committed *before* the enqueue, and the candidate
is marked CONSUMED *after* it. That sequence is chosen so every crash point leaves something
recoverable:

* crash before commit → nothing happened; the candidate is still SELECTED.
* crash between commit and enqueue → a PipelineJob exists with ``enqueued_at IS NULL``. It is
  findable by exactly that predicate and re-dispatchable by ``retry_pending_enqueue`` — the
  admission key stops the retry creating a second run.
* crash after enqueue, before the candidate is updated → the run is live and the candidate is
  still SELECTED; re-admitting it returns ``already_admitted`` and repairs the status rather
  than starting a second production.

The alternative — enqueue first, then commit — has no recoverable middle: a message would be
in flight for a run that does not exist, and nothing on either side could find it. This is not
exactly-once; it is at-least-once with a database-enforced identity that makes the duplicate
harmless.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.content_topic import ContentTopic
from app.models.enums import PipelineEventType, PipelineState, VideoCandidateStatus
from app.models.pipeline_job import PipelineJob
from app.models.video_candidate import VideoCandidate
from app.services import event_bus
from app.services.pipeline_job_service import PipelineJobService
from app.services.work_queue import EnqueueError, RedisWorkQueue, WorkQueue

logger = logging.getLogger(__name__)

# The identity version. Bumping it makes a *deliberate* re-production of the same candidate
# possible: a new key, a new run, and the old admission still on record. Not a timestamp —
# a timestamp would make every retry a new identity, which is the opposite of idempotency.
ADMISSION_PROFILE = "v1"

# Nothing beyond this may run at once, whatever the configuration says. A topic that sets
# max_active_jobs to 10000 is a mistake, and this is where it stops being one.
HARD_MAX_ACTIVE_JOBS = 20
HARD_MAX_ADMISSIONS_PER_RUN = 10

# States where a run is holding worker capacity. Deliberately derived from the machine's own
# happy path rather than listed by hand, so a state added there cannot silently stop counting.
def _active_states() -> frozenset[PipelineState]:
    from app.services.pipeline_state_machine import COMPLETION_STATES, HAPPY_PATH, TERMINAL_STATES

    resting = TERMINAL_STATES | COMPLETION_STATES | {
        PipelineState.FAILED,
        PipelineState.REVIEW_REQUIRED,
    }
    return frozenset(state for state in HAPPY_PATH if state not in resting)


ACTIVE_STATES = _active_states()

# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

ADMITTED = "admitted"
ALREADY_ADMITTED = "already_admitted"
TEMPORARILY_BLOCKED = "temporarily_blocked"
PERMANENTLY_BLOCKED = "permanently_blocked"
ENQUEUE_FAILED = "enqueue_failed"
INVALID_STATE = "invalid_state"

# Reason codes
NOT_SELECTED = "not_selected"
CANDIDATE_UNAVAILABLE = "candidate_unavailable"
MISSING_SOURCE_URL = "missing_source_url"
CAPACITY_LIMIT = "capacity_limit"
DAILY_ADMISSION_CAP = "daily_admission_cap"
RUN_LIMIT_REACHED = "run_limit_reached"
QUEUE_UNAVAILABLE = "queue_unavailable"
PAYLOAD_REJECTED = "payload_rejected"


@dataclass(frozen=True)
class AdmissionConfig:
    """Capacity, independent of the selection caps.

    Selection limits how much is *chosen*; this limits how much is *started*. They are
    different budgets: a topic may reasonably select five candidates a day while only two
    productions can run at once.
    """

    max_active_jobs: int = 3
    max_admissions_per_run: int = 3
    max_admissions_per_day: int = 12

    def with_overrides(self, overrides: dict[str, Any] | None) -> "AdmissionConfig":
        if not overrides:
            return self
        fields = {name: getattr(self, name) for name in self.__dataclass_fields__}
        for key, value in overrides.items():
            if key not in fields or value is None:
                continue
            try:
                fields[key] = int(value)
            except (TypeError, ValueError):
                continue
        # Clamped after the overrides, so no configuration path reaches the ceiling.
        fields["max_active_jobs"] = max(0, min(fields["max_active_jobs"], HARD_MAX_ACTIVE_JOBS))
        fields["max_admissions_per_run"] = max(
            0, min(fields["max_admissions_per_run"], HARD_MAX_ADMISSIONS_PER_RUN)
        )
        return AdmissionConfig(**fields)

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass
class AdmissionDecision:
    """What happened to one candidate, and why."""

    candidate_id: str
    # Built incrementally as the checks run, so it starts undecided rather than defaulting to
    # any real outcome — a default of "admitted" or "blocked" would be a lie on the way there.
    outcome: str = "not_evaluated"
    reasons: list[str] = field(default_factory=list)
    pipeline_job_id: str | None = None
    worker_job_id: str | None = None
    admission_key: str | None = None
    title: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "title": self.title,
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "pipeline_job_id": self.pipeline_job_id,
            "worker_job_id": self.worker_job_id,
            "admission_key": self.admission_key,
        }


@dataclass
class AdmissionRunReport:
    run_id: str
    topic_id: str | None
    dry_run: bool
    capacity_limit: int = 0
    active_jobs: int = 0
    available_slots: int = 0
    requested_limit: int = 0
    selected_waiting: int = 0
    decisions: list[AdmissionDecision] = field(default_factory=list)
    duration_ms: int = 0

    def _by(self, outcome: str) -> list[AdmissionDecision]:
        return [d for d in self.decisions if d.outcome == outcome]

    def as_dict(self) -> dict[str, Any]:
        return {
            "admission_run_id": self.run_id,
            "topic_id": self.topic_id,
            "dry_run": self.dry_run,
            "selected_waiting": self.selected_waiting,
            "capacity_limit": self.capacity_limit,
            "active_jobs": self.active_jobs,
            "available_slots": self.available_slots,
            "requested_limit": self.requested_limit,
            "admitted": [d.as_dict() for d in self._by(ADMITTED)],
            "already_admitted": [d.as_dict() for d in self._by(ALREADY_ADMITTED)],
            "blocked": [
                d.as_dict()
                for d in self.decisions
                if d.outcome in (TEMPORARILY_BLOCKED, PERMANENTLY_BLOCKED, INVALID_STATE)
            ],
            "enqueue_failures": [d.as_dict() for d in self._by(ENQUEUE_FAILED)],
            "counts": {
                "admitted": len(self._by(ADMITTED)),
                "already_admitted": len(self._by(ALREADY_ADMITTED)),
                "temporarily_blocked": len(self._by(TEMPORARILY_BLOCKED)),
                "permanently_blocked": len(self._by(PERMANENTLY_BLOCKED)),
                "invalid_state": len(self._by(INVALID_STATE)),
                "enqueue_failed": len(self._by(ENQUEUE_FAILED)),
            },
            "duration_ms": self.duration_ms,
        }


def admission_key_for(candidate_id: Any, profile: str = ADMISSION_PROFILE) -> str:
    """``admit:<candidate>:<profile>`` — deterministic, and never time-based.

    A retried request must produce the same key, or the constraint protects nothing.
    """
    return f"admit:{candidate_id}:{profile}"


class ProductionAdmissionService:
    """The single path from a selected candidate to queued production.

    Every caller — the admission run, the direct endpoint, and any future scheduler — goes
    through :meth:`admit_candidate`. A second implementation would be a second place for the
    capacity check and the idempotency key to drift out of agreement.
    """

    def __init__(self, queue: WorkQueue | None = None) -> None:
        self.queue = queue or RedisWorkQueue()
        self.runs = PipelineJobService()

    # ------------------------------------------------------------------ run

    def run(
        self,
        db: Session,
        *,
        topic: ContentTopic | None,
        limit: int | None = None,
        dry_run: bool = True,
        now: datetime | None = None,
        actor: str | None = None,
    ) -> AdmissionRunReport:
        now = now or datetime.now(timezone.utc)
        started = time.monotonic()
        config = self._config_for(topic)

        if not dry_run:
            # Serialise committed admission runs. Without it two runs both read the same
            # active count, both find slots free, and the capacity limit is exceeded while
            # each run believes it obeyed it. Transaction-scoped, released on commit.
            self._lock(db, topic.id if topic else None)

        active = self._active_jobs(db, topic)
        admitted_today = self._admitted_today(db, topic, now)
        slots = max(0, config.max_active_jobs - active)
        day_slots = max(0, config.max_admissions_per_day - admitted_today)
        requested = config.max_admissions_per_run if limit is None else max(0, min(
            int(limit), HARD_MAX_ADMISSIONS_PER_RUN, config.max_admissions_per_run
        ))

        report = AdmissionRunReport(
            run_id=str(uuid.uuid4()),
            topic_id=str(topic.id) if topic else None,
            dry_run=dry_run,
            capacity_limit=config.max_active_jobs,
            active_jobs=active,
            available_slots=min(slots, day_slots),
            requested_limit=requested,
        )

        waiting = self._selected_waiting(db, topic)
        report.selected_waiting = len(waiting)

        budget = min(requested, slots, day_slots)
        for candidate in waiting:
            if budget <= 0:
                report.decisions.append(
                    AdmissionDecision(
                        candidate_id=str(candidate.id),
                        title=candidate.title,
                        outcome=TEMPORARILY_BLOCKED,
                        reasons=[
                            CAPACITY_LIMIT if slots <= 0
                            else DAILY_ADMISSION_CAP if day_slots <= 0
                            else RUN_LIMIT_REACHED
                        ],
                    )
                )
                continue

            decision = self.admit_candidate(
                db, candidate=candidate, dry_run=dry_run, now=now,
                skip_capacity_check=True, actor=actor,
            )
            report.decisions.append(decision)
            if decision.outcome == ADMITTED:
                budget -= 1
                slots -= 1
                day_slots -= 1

        report.duration_ms = int((time.monotonic() - started) * 1000)
        self._emit_run(db, topic, report)
        self._log_run(report)
        return report

    # ------------------------------------------------------------- one candidate

    def admit_candidate(
        self,
        db: Session,
        *,
        candidate: VideoCandidate,
        dry_run: bool = False,
        now: datetime | None = None,
        skip_capacity_check: bool = False,
        actor: str | None = None,
    ) -> AdmissionDecision:
        """Admit one candidate. Never raises: every failure is a classified outcome.

        An admission that throws would leave the caller unable to tell "already running" from
        "Redis is down" from "this candidate was rejected" — three situations with three
        different responses.
        """
        now = now or datetime.now(timezone.utc)
        key = admission_key_for(candidate.id)
        decision = AdmissionDecision(
            candidate_id=str(candidate.id), title=candidate.title, admission_key=key
        )

        existing = (
            db.query(PipelineJob).filter(PipelineJob.admission_key == key).first()
        )
        if existing is not None:
            return self._already_admitted(db, candidate, existing, decision, dry_run, now)

        blocked = self._check_eligibility(candidate)
        if blocked:
            decision.outcome = blocked[0]
            decision.reasons = blocked[1]
            return decision

        if not skip_capacity_check:
            topic = db.query(ContentTopic).filter(
                ContentTopic.id == candidate.topic_id
            ).first()
            config = self._config_for(topic)
            if self._active_jobs(db, topic) >= config.max_active_jobs:
                decision.outcome = TEMPORARILY_BLOCKED
                decision.reasons = [CAPACITY_LIMIT]
                return decision

        if dry_run:
            decision.outcome = ADMITTED
            decision.reasons = ["would_admit"]
            return decision

        return self._perform(db, candidate, decision, key, now, actor)

    # --------------------------------------------------------------- internals

    def _perform(
        self,
        db: Session,
        candidate: VideoCandidate,
        decision: AdmissionDecision,
        key: str,
        now: datetime,
        actor: str | None,
    ) -> AdmissionDecision:
        topic = db.query(ContentTopic).filter(ContentTopic.id == candidate.topic_id).first()
        snapshot = self._snapshot(candidate, topic, now)
        worker_job_id = str(uuid.uuid4())

        try:
            with db.begin_nested():
                run = self.runs.create_for_enqueue(
                    db,
                    worker_job_id=worker_job_id,
                    source_url=snapshot["source_url"],
                    pipeline_stage="prepare",
                    clip_mode=snapshot["clip_mode"],
                    video_ratio=snapshot["video_ratio"],
                    origin="admission",
                    metadata={
                        "admission": {
                            "admission_key": key,
                            "admitted_at": now.isoformat(),
                            "actor": actor,
                        },
                        # Provenance: which candidate, which selection, which score version.
                        "provenance": snapshot["provenance"],
                        # Frozen inputs. The topic's defaults can be edited an hour from now;
                        # a run already in flight must not change shape underneath the worker.
                        "snapshot": snapshot["frozen"],
                    },
                    commit=False,
                )
                run.admission_key = key
                run.topic_id = candidate.topic_id
                run.candidate_id = candidate.id
                db.flush()
        except IntegrityError:
            # Another request won the race between the lookup above and this insert. The
            # unique index settled it; report the winner rather than starting a second run.
            db.rollback()
            winner = db.query(PipelineJob).filter(PipelineJob.admission_key == key).first()
            if winner is None:
                raise
            db.refresh(candidate)
            return self._already_admitted(db, candidate, winner, decision, False, now)

        # Commit BEFORE the enqueue. A crash here leaves a row with enqueued_at IS NULL, which
        # `retry_pending_enqueue` finds; enqueueing first would leave a message referring to a
        # run that does not exist, which nothing can find.
        db.commit()
        db.refresh(run)

        payload = self._payload(run, snapshot)
        try:
            self.queue.publish(payload)
        except EnqueueError as exc:
            decision.outcome = ENQUEUE_FAILED
            decision.reasons = [QUEUE_UNAVAILABLE if exc.retryable else PAYLOAD_REJECTED]
            decision.pipeline_job_id = str(run.id)
            decision.worker_job_id = worker_job_id
            # The candidate stays SELECTED and the run stays un-enqueued. Both are true, and
            # together they are recoverable: neither the work nor the intent is lost.
            logger.warning(
                "admission_enqueue_failed",
                extra={
                    "candidate_id": str(candidate.id),
                    "pipeline_job_id": str(run.id),
                    "retryable": exc.retryable,
                },
            )
            return decision

        run.enqueued_at = now
        # Only now is the candidate finished with: the work is genuinely in the queue.
        candidate.status = VideoCandidateStatus.CONSUMED
        metadata = dict(candidate.metadata_json or {})
        metadata["production"] = {
            "pipeline_job_id": str(run.id),
            "worker_job_id": worker_job_id,
            "admission_key": key,
            "admitted_at": now.isoformat(),
        }
        candidate.metadata_json = metadata
        db.commit()

        decision.outcome = ADMITTED
        decision.reasons = ["admitted"]
        decision.pipeline_job_id = str(run.id)
        decision.worker_job_id = worker_job_id
        self._emit_admitted(db, candidate, run, decision)
        return decision

    def _already_admitted(
        self,
        db: Session,
        candidate: VideoCandidate,
        run: PipelineJob,
        decision: AdmissionDecision,
        dry_run: bool,
        now: datetime,
    ) -> AdmissionDecision:
        """A run already exists for this admission identity.

        This is the normal answer to a retried request, and also the repair path: if the
        previous attempt enqueued but crashed before updating the candidate, the status is
        corrected here instead of a second production being started.
        """
        decision.outcome = ALREADY_ADMITTED
        decision.pipeline_job_id = str(run.id)
        decision.worker_job_id = run.worker_job_id
        decision.reasons = ["existing_admission"]

        if run.enqueued_at is None:
            decision.reasons.append("pending_enqueue")

        if (
            not dry_run
            and run.enqueued_at is not None
            and candidate.status != VideoCandidateStatus.CONSUMED
        ):
            candidate.status = VideoCandidateStatus.CONSUMED
            metadata = dict(candidate.metadata_json or {})
            metadata.setdefault(
                "production",
                {
                    "pipeline_job_id": str(run.id),
                    "worker_job_id": run.worker_job_id,
                    "admission_key": run.admission_key,
                    "admitted_at": (run.enqueued_at or now).isoformat(),
                },
            )
            candidate.metadata_json = metadata
            decision.reasons.append("candidate_status_repaired")
            db.commit()

        return decision

    def retry_pending_enqueue(
        self,
        db: Session,
        *,
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[AdmissionDecision]:
        """Re-dispatch admissions that persisted but never reached the queue.

        Idempotent and bounded: it re-publishes runs found by ``enqueued_at IS NULL``, and the
        admission key means a concurrent normal admission of the same candidate cannot create
        a second run. Deliberately a service method with a limit rather than a background
        loop — a retry loop with no operator in front of it is how a broken queue turns into
        an unbounded republish storm.
        """
        now = now or datetime.now(timezone.utc)
        pending = (
            db.query(PipelineJob)
            .filter(
                PipelineJob.admission_key.isnot(None),
                PipelineJob.enqueued_at.is_(None),
                PipelineJob.state == PipelineState.QUEUED,
            )
            .order_by(PipelineJob.created_at.asc())
            .limit(max(1, min(limit, HARD_MAX_ADMISSIONS_PER_RUN)))
            .all()
        )

        decisions: list[AdmissionDecision] = []
        for run in pending:
            candidate = (
                db.query(VideoCandidate).filter(VideoCandidate.id == run.candidate_id).first()
                if run.candidate_id
                else None
            )
            decision = AdmissionDecision(
                candidate_id=str(run.candidate_id) if run.candidate_id else "",
                title=candidate.title if candidate else None,
                admission_key=run.admission_key,
                pipeline_job_id=str(run.id),
                worker_job_id=run.worker_job_id,
            )
            metadata = dict(run.metadata_json or {})
            snapshot = {
                "frozen": metadata.get("snapshot") or {},
                "provenance": metadata.get("provenance") or {},
                "source_url": run.source_url,
                "clip_mode": run.clip_mode,
                "video_ratio": run.video_ratio,
            }
            try:
                self.queue.publish(self._payload(run, snapshot))
            except EnqueueError as exc:
                decision.outcome = ENQUEUE_FAILED
                decision.reasons = [
                    QUEUE_UNAVAILABLE if exc.retryable else PAYLOAD_REJECTED
                ]
                decisions.append(decision)
                continue

            run.enqueued_at = now
            if candidate is not None and candidate.status != VideoCandidateStatus.CONSUMED:
                candidate.status = VideoCandidateStatus.CONSUMED
                candidate_metadata = dict(candidate.metadata_json or {})
                candidate_metadata["production"] = {
                    "pipeline_job_id": str(run.id),
                    "worker_job_id": run.worker_job_id,
                    "admission_key": run.admission_key,
                    "admitted_at": now.isoformat(),
                }
                candidate.metadata_json = candidate_metadata
            db.commit()

            decision.outcome = ADMITTED
            decision.reasons = ["recovered_pending_enqueue"]
            decisions.append(decision)

        return decisions

    # ------------------------------------------------------------- checks

    @staticmethod
    def _check_eligibility(candidate: VideoCandidate) -> tuple[str, list[str]] | None:
        """Revalidated at admission time, not trusted from discovery.

        Discovery may have run hours ago. What is rechecked is only what is already persisted:
        no provider call is made, because spending YouTube quota on every admission would cost
        one search's worth of allowance per production. A video that has since become
        unavailable is caught by the downstream download failure, which the reliable queue
        already classifies and dead-letters.
        """
        if candidate.status == VideoCandidateStatus.CONSUMED:
            # Consumed but no admission row: an older manual path produced it. Nothing to do.
            return ALREADY_ADMITTED, ["already_consumed"]
        if candidate.status != VideoCandidateStatus.SELECTED:
            return INVALID_STATE, [NOT_SELECTED, f"status_{candidate.status.value}"]

        normalized = dict((candidate.metadata_json or {}).get("normalized") or {})
        if normalized.get("available") is False:
            return PERMANENTLY_BLOCKED, [CANDIDATE_UNAVAILABLE]
        if not (candidate.url or "").strip():
            return PERMANENTLY_BLOCKED, [MISSING_SOURCE_URL]
        return None

    def _config_for(self, topic: ContentTopic | None) -> AdmissionConfig:
        overrides = ((topic.metadata_json or {}).get("admission") if topic else None)
        return AdmissionConfig().with_overrides(overrides)

    def _active_jobs(self, db: Session, topic: ContentTopic | None) -> int:
        query = db.query(func.count(PipelineJob.id)).filter(
            PipelineJob.state.in_(list(ACTIVE_STATES))
        )
        if topic is not None:
            query = query.filter(PipelineJob.topic_id == topic.id)
        return query.scalar() or 0

    def _admitted_today(
        self, db: Session, topic: ContentTopic | None, now: datetime
    ) -> int:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        query = db.query(func.count(PipelineJob.id)).filter(
            PipelineJob.admission_key.isnot(None),
            PipelineJob.created_at >= midnight,
        )
        if topic is not None:
            query = query.filter(PipelineJob.topic_id == topic.id)
        return query.scalar() or 0

    @staticmethod
    def _selected_waiting(db: Session, topic: ContentTopic | None) -> list[VideoCandidate]:
        """Selected candidates awaiting admission, in a deterministic order.

        Highest score first, then oldest selection — never the database's arbitrary order, or
        which candidate gets the last free slot would depend on physical row layout.
        """
        query = db.query(VideoCandidate).filter(
            VideoCandidate.status == VideoCandidateStatus.SELECTED
        )
        if topic is not None:
            query = query.filter(VideoCandidate.topic_id == topic.id)
        return query.order_by(
            VideoCandidate.relevance_score.desc().nullslast(),
            VideoCandidate.selected_at.asc().nullsfirst(),
            VideoCandidate.id.asc(),
        ).all()

    # ------------------------------------------------------------- snapshot

    @staticmethod
    def _snapshot(
        candidate: VideoCandidate, topic: ContentTopic | None, now: datetime
    ) -> dict[str, Any]:
        """Freeze the inputs this production will run on.

        A run reads its configuration once, at admission. Editing the topic's defaults an hour
        later must not reshape a job already in flight — the worker would then produce
        something nobody asked for, with no record of why.

        Compact on purpose: the candidate's full metadata is not copied. Only what the worker
        consumes, plus enough provenance to answer where the run came from.
        """
        selection = dict((candidate.metadata_json or {}).get("selection") or {})
        scores = dict(candidate.scores_json or {})
        return {
            "source_url": candidate.url,
            "clip_mode": (topic.default_clip_mode if topic else "short_serie"),
            "video_ratio": (topic.default_video_ratio if topic else "portrait"),
            "frozen": {
                "source_url": candidate.url,
                "clip_mode": (topic.default_clip_mode if topic else "short_serie"),
                "video_ratio": (topic.default_video_ratio if topic else "portrait"),
                "build_ia": bool(
                    ((topic.metadata_json or {}).get("admission") or {}).get("build_ia", True)
                    if topic else True
                ),
                "topic_name": topic.name if topic else None,
                "frozen_at": now.isoformat(),
            },
            "provenance": {
                "video_candidate_id": str(candidate.id),
                "topic_id": str(candidate.topic_id) if candidate.topic_id else None,
                "external_id": candidate.external_id,
                "selection_method": selection.get("method"),
                "selection_run_id": selection.get("selection_run_id"),
                "selection_score": scores.get("final_score"),
                "score_version": scores.get("version") or selection.get("score_version"),
                "selected_at": (
                    candidate.selected_at.isoformat() if candidate.selected_at else None
                ),
            },
        }

    @staticmethod
    def _payload(run: PipelineJob, snapshot: dict[str, Any]) -> dict[str, Any]:
        """The queue message.

        Deliberately small. The worker needs the source, the shape and the two ids; it does
        not need the candidate's description, raw provider metadata or score breakdown, and
        putting them on the queue would grow every message for data the worker never reads.
        """
        frozen = dict(snapshot.get("frozen") or {})
        return {
            "job_id": run.worker_job_id,
            "pipeline_job_id": str(run.id),
            "video_url": frozen.get("source_url") or run.source_url,
            "pipeline_stage": "prepare",
            "clip_mode": frozen.get("clip_mode") or run.clip_mode,
            "video_ratio": frozen.get("video_ratio") or run.video_ratio,
            "build_ia": bool(frozen.get("build_ia", True)),
            "manual_response": None,
            "origin": "admission",
        }

    # -------------------------------------------------------- observability

    @staticmethod
    def _lock(db: Session, topic_id) -> None:
        """Serialise committed admission runs.

        PostgreSQL advisory lock, transaction-scoped. A no-op on other backends: the test
        suite runs on SQLite, which serialises writers anyway.
        """
        if db.bind is None or db.bind.dialect.name != "postgresql":
            return
        key = int(uuid.UUID(str(topic_id)).int % (2**31)) if topic_id else 7_777_001
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def _emit_run(
        self, db: Session, topic: ContentTopic | None, report: AdmissionRunReport
    ) -> None:
        payload = report.as_dict()
        event_bus.publish_event(
            db,
            service="admission",
            event_type=PipelineEventType.INFO,
            pipeline_job_id=None,
            stage="admission.completed",
            message=(
                f"admission {'dry-run' if report.dry_run else 'run'}: "
                f"{payload['counts']['admitted']} admitted of {report.selected_waiting} waiting"
            ),
            payload={
                "admission_run_id": report.run_id,
                "topic_id": report.topic_id,
                "dry_run": report.dry_run,
                "selected_waiting": report.selected_waiting,
                "capacity_limit": report.capacity_limit,
                "active_jobs": report.active_jobs,
                "available_slots": report.available_slots,
                **payload["counts"],
                "duration_ms": report.duration_ms,
            },
        )

    def _emit_admitted(
        self,
        db: Session,
        candidate: VideoCandidate,
        run: PipelineJob,
        decision: AdmissionDecision,
    ) -> None:
        event_bus.publish_event(
            db,
            service="admission",
            event_type=PipelineEventType.INFO,
            # Attached to the run: from here on this candidate's story is the run's story.
            pipeline_job_id=run.id,
            stage="candidate.admitted",
            message=f"admitted: {(candidate.title or '')[:120]}",
            payload={
                "candidate_id": str(candidate.id),
                "pipeline_job_id": str(run.id),
                "worker_job_id": run.worker_job_id,
                "admission_key": decision.admission_key,
            },
        )

    @staticmethod
    def _log_run(report: AdmissionRunReport) -> None:
        counts = report.as_dict()["counts"]
        logger.info(
            "admission_run",
            extra={
                "admission_run_id": report.run_id,
                "topic_id": report.topic_id,
                "dry_run": report.dry_run,
                "selected_waiting": report.selected_waiting,
                "capacity_limit": report.capacity_limit,
                "active_jobs": report.active_jobs,
                "available_slots": report.available_slots,
                "requested_limit": report.requested_limit,
                "admitted": counts["admitted"],
                "already_admitted": counts["already_admitted"],
                "temporarily_blocked": counts["temporarily_blocked"],
                "enqueue_failures": counts["enqueue_failed"],
                "duration_ms": report.duration_ms,
            },
        )
