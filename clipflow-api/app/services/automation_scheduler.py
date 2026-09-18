"""Decides *when* the autonomous loop runs. Nothing about what it does.

    tick
     ├── kill switch?          → skip everything
     ├── recover pending enqueue   (orphans before new work)
     └── for each due pipeline:
            acquire lock → run AutonomousPipelineService → persist next_due_at

The split matters: this file contains no discovery, no ranking, no capacity arithmetic and no
idempotency key. It answers four questions — is automation on, which pipelines are due, may this
process run one, and when should it run next — and hands everything else to the orchestrator.

**Three separate protections against running a pipeline twice**, because they guard different
things and none of them subsumes the others:

* a PostgreSQL **advisory lock**, which stops two API replicas from running the same pipeline;
* a persisted **``running_since``**, which stops the *next tick of the same process* from
  re-entering a run that is still going after its own interval expired;
* the services' own **idempotency**, which makes a duplicate harmless if the first two ever
  fail — admission's unique key and selection's status checks were built for exactly this.

**Ordering: recovery before new production.** A run that persisted but never reached the queue
is work already decided on and paid for; starting fresh production while it sits stranded adds
load and leaves the orphan no closer to running.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.automation_state import AutomationState
from app.models.pipeline import Pipeline
from app.services.automation_run_service import AutomationRunService
from app.services.stalled_run_service import StalledRunService
from app.services.automation_service import (
    FAILED,
    AutomationConfig,
    AutomationRunReport,
    AutonomousPipelineService,
)

logger = logging.getLogger(__name__)

# Skip reasons.
GLOBAL_DISABLED = "global_disabled"
PIPELINE_DISABLED = "pipeline_disabled"
NOT_DUE = "not_due"
LOCK_UNAVAILABLE = "lock_unavailable"
OVERLAP = "skip_overlap"
BACKOFF = "failure_backoff"

# Namespace for the advisory lock, so an automation lock can never collide with the
# selection or admission locks that use the same pipeline id.
_LOCK_NAMESPACE = "clipflow:automation:"

# A run still marked running after this long is treated as abandoned — the process that owned
# it died without clearing the flag, and refusing to run the pipeline forever afterwards would be
# worse than re-entering it, which the services' idempotency already covers.
STALE_RUN_MINUTES = 60


@dataclass
class TickReport:
    """What one tick did. The unit an operator reads when asking "is it running?"."""

    tick_id: str
    started_at: datetime
    enabled: bool = True
    pipelines_considered: int = 0
    pending_enqueue_recovered: int = 0
    runs_settled: int = 0
    stalled_rescued: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tick_id": self.tick_id,
            "started_at": self.started_at.isoformat(),
            "enabled": self.enabled,
            "pipelines_considered": self.pipelines_considered,
            "pending_enqueue_recovered": self.pending_enqueue_recovered,
            "runs_settled": self.runs_settled,
            "stalled_rescued": self.stalled_rescued,
            "ran": len(self.runs),
            "skipped": len(self.skipped),
            "runs": self.runs,
            "skips": self.skipped,
            "duration_ms": self.duration_ms,
        }


def deterministic_jitter_seconds(pipeline_id: Any, spread_seconds: int = 120) -> int:
    """Spread pipelines across the interval, stably.

    Every pipeline would otherwise become due at the same instant after a deploy and stampede the
    providers together. Derived from the pipeline id rather than ``random`` so the offset is the
    same on every replica and across restarts — a random jitter would make two replicas
    disagree about when a pipeline is due, and make a schedule impossible to reproduce.
    """
    if spread_seconds <= 0:
        return 0
    digest = hashlib.sha256(str(pipeline_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % spread_seconds



def _republish_run(db: Session, job: Any) -> None:
    """Devolve a run à fila do worker pelo mesmo caminho que a API usa.

    Importado aqui dentro porque `app.api.jobs` importa serviços: no topo do módulo isso
    fecharia um ciclo de import.
    """
    from app.api.jobs import republish_run

    republish_run(db, job)

class AutomationScheduler:
    """Finds due pipelines and runs them, one at a time, under lock."""

    def __init__(
        self,
        pipeline: AutonomousPipelineService | None = None,
        runs: AutomationRunService | None = None,
        stalled: StalledRunService | None = None,
    ) -> None:
        self.pipeline = pipeline or AutonomousPipelineService()
        self.runs = runs or AutomationRunService()
        self.stalled = stalled or StalledRunService(republish=_republish_run)

    # ------------------------------------------------------------------- tick

    def tick(
        self,
        db: Session,
        *,
        now: datetime | None = None,
        max_pipelines: int = 5,
    ) -> TickReport:
        """One scheduler pass. Never raises: a tick that throws stops the loop forever."""
        now = now or datetime.now(timezone.utc)
        report = TickReport(tick_id=str(uuid.uuid4()), started_at=now)

        if not settings.autonomous_pipeline_enabled:
            # The kill switch stops *work*, not the scheduler: the loop keeps ticking and the
            # process stays healthy, so turning automation back on needs no restart.
            report.enabled = False
            report.skipped.append({"reason": GLOBAL_DISABLED})
            logger.debug("automation_tick_disabled", extra={"tick_id": report.tick_id})
            return report

        # Antes de tudo, as runs que pararam de andar. Uma run travada não se resolve
        # sozinha e não aparece em lugar nenhum: ela só ocupa a fila de "em andamento" e
        # some da vista. Primeiro porque é trabalho já pago, como os órfãos abaixo.
        report.stalled_rescued = [r.as_dict() for r in self.stalled.rescue(db, now=now)]

        # Orphans first: work already decided on, stranded before it reached the queue.
        report.pending_enqueue_recovered = self.pipeline.recover_pending_enqueue(db)

        # Then close the cycles whose productions have finished since the last pass. Asked
        # here rather than reported by the worker: the worker knows one production's state and
        # not whether it was the last one its cycle was waiting for.
        report.runs_settled = self.runs.settle_finished(db)

        pipelines = (
            db.query(Pipeline)
            .filter(Pipeline.is_active.is_(True))
            .order_by(Pipeline.created_at.asc())
            .all()
        )
        report.pipelines_considered = len(pipelines)

        for pipeline in pipelines:
            if len(report.runs) >= max_pipelines:
                # Bounded per tick so one tick cannot monopolise the process; the rest stay
                # due and are picked up by the next pass.
                break
            outcome = self.run_pipeline_if_due(db, pipeline=pipeline, now=now)
            if isinstance(outcome, AutomationRunReport):
                report.runs.append(outcome.as_dict())
            elif outcome is not None:
                report.skipped.append(outcome)

        report.duration_ms = int(
            (datetime.now(timezone.utc) - now).total_seconds() * 1000
        )
        return report

    # -------------------------------------------------------------- one pipeline

    def run_pipeline_if_due(
        self,
        db: Session,
        *,
        pipeline: Pipeline,
        now: datetime | None = None,
        force: bool = False,
        trigger: str = "tick",
        actor: str | None = None,
    ) -> AutomationRunReport | dict[str, Any] | None:
        """Run this pipeline if it is due and nothing else is running it.

        Returns the run report, or a skip record explaining why not.

        ``force`` is the manual trigger: it bypasses the due check only. The lock and the
        overlap guard still apply, because a manual run that skipped those could race an
        automatic one and break the caps both are supposed to respect.
        """
        now = now or datetime.now(timezone.utc)
        config = AutomationConfig.from_pipeline(pipeline)

        if not config.enabled:
            return {"pipeline_id": str(pipeline.id), "reason": PIPELINE_DISABLED}

        state = self._state_for(db, pipeline)

        if not force and not self._is_due(state, now):
            return {
                "pipeline_id": str(pipeline.id),
                "reason": NOT_DUE,
                "next_due_at": state.next_due_at.isoformat() if state.next_due_at else None,
            }

        if self._is_running(state, now):
            # The previous run outlived its own interval. Skipping is the correct response:
            # queueing this tick behind it would build a backlog of stale ticks that all fire
            # at once when the long run finally ends.
            return {
                "pipeline_id": str(pipeline.id),
                "reason": OVERLAP,
                "running_since": state.running_since.isoformat() if state.running_since else None,
            }

        with self._pipeline_lock(db, pipeline.id) as acquired:
            if not acquired:
                # Another replica has it. Skip rather than wait: blocking would hold this
                # tick — and the whole loop — behind another process's work.
                return {"pipeline_id": str(pipeline.id), "reason": LOCK_UNAVAILABLE}

            # Re-read under the lock. Between the check above and here another process may
            # have finished a run and pushed next_due_at forward.
            db.refresh(state)
            if self._is_running(state, now):
                return {"pipeline_id": str(pipeline.id), "reason": OVERLAP}
            if not force and not self._is_due(state, now):
                return {"pipeline_id": str(pipeline.id), "reason": NOT_DUE}

            run_id = str(uuid.uuid4())
            state.running_since = now
            state.running_run_id = run_id
            state.last_started_at = now
            db.commit()

            try:
                report = self.pipeline.run_pipeline(
                    db, pipeline=pipeline, config=config, now=now,
                    automation_run_id=run_id, actor="scheduler",
                )
            except Exception as exc:  # noqa: BLE001
                # A crash in the orchestrator must not leave the pipeline marked running forever,
                # and must not be swallowed either — it is a programming error and has to be
                # visible in the logs with its traceback.
                logger.exception(
                    "automation_run_crashed",
                    extra={"automation_run_id": run_id, "pipeline_id": str(pipeline.id)},
                )
                report = AutomationRunReport(
                    automation_run_id=run_id, pipeline_id=str(pipeline.id), started_at=now
                )
                report.status = FAILED
                report.skip_reason = type(exc).__name__
                report.finished_at = datetime.now(timezone.utc)

            self._settle(db, state, config, report, now, trigger=trigger, actor=actor)
            return report

    # ---------------------------------------------------------------- state

    def _state_for(self, db: Session, pipeline: Pipeline) -> AutomationState:
        state = (
            db.query(AutomationState)
            .filter(AutomationState.pipeline_id == pipeline.id)
            .first()
        )
        if state is not None:
            return state

        state = AutomationState(pipeline_id=pipeline.id)
        db.add(state)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            # Another replica created it between the read and the insert. The unique
            # constraint settled it; use the winner.
            db.rollback()
            state = (
                db.query(AutomationState)
                .filter(AutomationState.pipeline_id == pipeline.id)
                .one()
            )
        return state

    @staticmethod
    def _is_due(state: AutomationState, now: datetime) -> bool:
        # Never scheduled is due: a pipeline just switched on should not wait a full interval
        # before its first run.
        if state.next_due_at is None:
            return True
        return _as_utc(state.next_due_at) <= now

    @staticmethod
    def _is_running(state: AutomationState, now: datetime) -> bool:
        if state.running_since is None:
            return False
        age = now - _as_utc(state.running_since)
        if age > timedelta(minutes=STALE_RUN_MINUTES):
            # The owning process died without clearing the flag. Treat it as finished rather
            # than wedging the pipeline permanently.
            logger.warning(
                "automation_stale_run_cleared",
                extra={"pipeline_id": str(state.pipeline_id), "age_minutes": age.total_seconds() / 60},
            )
            return False
        return True

    def _settle(
        self,
        db: Session,
        state: AutomationState,
        config: AutomationConfig,
        report: AutomationRunReport,
        now: datetime,
        trigger: str = "tick",
        actor: str | None = None,
    ) -> None:
        """Record the outcome and schedule the next run.

        This runs whether the report succeeded or failed, so ``running_since`` is always
        cleared — a pipeline must never be left permanently marked as running.
        """
        state.running_since = None
        state.running_run_id = None
        state.last_completed_at = report.finished_at or datetime.now(timezone.utc)
        state.last_status = report.status
        state.last_automation_run_id = report.automation_run_id

        # The cycle becomes a row here, where every path — worked, skipped, crashed — already
        # converges. A cycle that decided to do nothing is a fact worth keeping: it is the
        # answer to "why is nothing happening?".
        run = self.runs.record(
            db,
            pipeline_id=state.pipeline_id,
            report=report,
            trigger=trigger,
            actor=actor,
        )
        if run is not None:
            self.runs.attach(db, run.id, report.admitted_job_ids)

        if report.status == FAILED:
            state.consecutive_failures = (state.consecutive_failures or 0) + 1
        else:
            # Any non-failing run clears the penalty: a pipeline that recovers should not serve
            # out a backoff it no longer deserves.
            state.consecutive_failures = 0

        interval = timedelta(minutes=config.interval_minutes)
        if state.consecutive_failures >= config.max_consecutive_failures:
            # Repeatedly failing: back off rather than hammering the same broken thing every
            # interval. Bounded — this is a pause, not a circuit-breaker framework.
            interval = max(interval, timedelta(minutes=config.failure_backoff_minutes))

        jitter = timedelta(seconds=deterministic_jitter_seconds(state.pipeline_id))
        state.next_due_at = now + interval + jitter
        db.commit()

    # ----------------------------------------------------------------- lock

    @contextmanager
    def _pipeline_lock(self, db: Session, pipeline_id: Any) -> Iterator[bool]:
        """Try to take the pipeline's automation lock; never wait for it.

        ``pg_try_advisory_lock`` returns immediately rather than blocking, which is what makes
        a losing replica skip instead of stalling its whole loop behind another one's run.
        Session-scoped (not transaction-scoped) because the run commits several times, and a
        transaction-scoped lock would be released by the first of those commits — leaving the
        rest of the run unprotected.

        A no-op on other backends: the test suite runs on SQLite, which has one writer anyway.
        """
        if db.bind is None or db.bind.dialect.name != "postgresql":
            yield True
            return

        key = _lock_key(pipeline_id)
        acquired = bool(
            db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
        )
        try:
            yield acquired
        finally:
            if acquired:
                db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                db.commit()


def _lock_key(pipeline_id: Any) -> int:
    """A signed 64-bit key, namespaced so it cannot collide with the other pipeline locks."""
    digest = hashlib.sha256(f"{_LOCK_NAMESPACE}{pipeline_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _as_utc(value: datetime) -> datetime:
    """A timezone(True) column comes back aware on PostgreSQL and naive on SQLite; the
    difference only surfaces the first time two of them are subtracted."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
