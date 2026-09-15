"""Keeping the record of a cycle.

The report the orchestrator builds is already complete — every stage with its counts and its
reasons. It was simply never written down: it went out in the HTTP response of whichever call
triggered the tick, and `AutomationState` kept the id of the last run and nothing else. This
turns that report into a row.

**Two clocks, deliberately.** `finished_at` is when the cycle stopped deciding; `settled_at`
is when the productions it started stopped running. They are minutes apart and confusing them
is how a console ends up reporting success for cuts that have not been rendered. `status`
describes the first, `production_status` the second.

Nothing here may fail a run. A cycle that worked and whose record could not be written is
still a cycle that worked, and the loss is reported rather than raised.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.automation_run import AutomationRun
from app.models.enums import PipelineState
from app.models.pipeline_job import PipelineJob

logger = logging.getLogger(__name__)

# A production that has stopped moving. Everything else is still in flight, and a run with any
# of those is not settled.
TERMINAL_STATES = (
    PipelineState.PUBLISHED,
    PipelineState.FAILED,
    PipelineState.CANCELED,
    PipelineState.REVIEW_REQUIRED,
    PipelineState.READY_TO_PUBLISH,
)

# What the productions of a run are collectively doing.
NONE = "none"
RUNNING = "running"
COMPLETE = "complete"
PARTIAL = "partial"


class AutomationRunService:
    """Writes and settles the record of a cycle."""

    def record(
        self,
        db: Session,
        *,
        pipeline_id: Any,
        report: Any,
        trigger: str,
        actor: str | None = None,
    ) -> AutomationRun | None:
        """Persist one finished cycle. Returns the row, or None if it could not be written."""
        try:
            stages = report.as_dict()
            run = AutomationRun(
                id=_uuid(report.automation_run_id),
                pipeline_id=pipeline_id,
                trigger=trigger,
                actor=actor,
                status=report.status,
                skip_reason=report.skip_reason,
                started_at=report.started_at,
                finished_at=report.finished_at,
                duration_ms=report.duration_ms or 0,
                discovered=_count(stages, "discovery", "new_candidates"),
                selected=_count(stages, "selection", "selected"),
                admitted=_count(stages, "admission", "admitted"),
                publications_queued=_count(stages, "publication", "queued"),
                stages_json={
                    name: stages.get(name)
                    for name in ("discovery", "selection", "admission", "publication")
                },
            )
            # A cycle that admitted nothing has nothing to wait for, so it is settled the
            # moment it ends. One that did is left open until those productions finish.
            if run.admitted > 0:
                run.production_status = RUNNING
            else:
                run.production_status = NONE
                run.settled_at = report.finished_at

            db.add(run)
            db.flush()
            return run
        except Exception:  # noqa: BLE001
            logger.warning(
                "automation_run_not_recorded",
                extra={"pipeline_id": str(pipeline_id), "run_id": report.automation_run_id},
                exc_info=True,
            )
            return None

    def attach(self, db: Session, run_id: Any, job_ids: list[Any]) -> None:
        """Point the productions this cycle started back at it.

        Done in one statement rather than per job: admission can start several at once, and a
        loop of updates here would be a loop of round trips inside a lock.
        """
        if not run_id or not job_ids:
            return
        try:
            db.query(PipelineJob).filter(PipelineJob.id.in_(job_ids)).update(
                {PipelineJob.automation_run_id: _uuid(run_id)},
                synchronize_session=False,
            )
        except Exception:  # noqa: BLE001
            logger.warning("automation_run_jobs_not_attached", exc_info=True)

    def settle_finished(self, db: Session, *, limit: int = 50) -> int:
        """Close the runs whose productions have all stopped.

        Called from the scheduler tick rather than from the worker: the worker reports *one*
        production's state and does not know whether it was the last one its cycle was waiting
        for. Asking that question on a tick keeps the knowledge in one place, and a run that
        settles a minute late is not a problem anyone has.

        Returns how many runs were settled.
        """
        open_runs = (
            db.query(AutomationRun)
            .filter(AutomationRun.production_status == RUNNING)
            .order_by(AutomationRun.started_at.asc())
            .limit(limit)
            .all()
        )
        settled = 0
        for run in open_runs:
            counts = self._production_counts(db, run.id)
            if counts["pending"] > 0:
                continue

            run.published = counts["published"]
            run.production_status = COMPLETE if counts["published"] == counts["total"] else PARTIAL
            run.settled_at = datetime.now(timezone.utc)
            settled += 1

        if settled:
            # Committed here, not left to the caller. This runs at the top of a tick, before
            # any pipeline is considered, and a tick where nothing was due commits nothing —
            # so a settle that waited for the caller would be rolled back on exactly the
            # ticks where it is the only thing that happened.
            db.commit()
            logger.info("automation_runs_settled", extra={"count": settled})
        return settled

    @staticmethod
    def _production_counts(db: Session, run_id: Any) -> dict[str, int]:
        rows = (
            db.query(PipelineJob.state, func.count(PipelineJob.id))
            .filter(PipelineJob.automation_run_id == run_id)
            .group_by(PipelineJob.state)
            .all()
        )
        total = pending = published = 0
        for state, count in rows:
            total += int(count)
            if state == PipelineState.PUBLISHED:
                published += int(count)
            if state not in TERMINAL_STATES:
                pending += int(count)
        return {"total": total, "pending": pending, "published": published}


def _uuid(value: Any) -> uuid.UUID | None:
    """The run id is a UUID string everywhere it is generated; be tolerant if it is not."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _count(stages: dict[str, Any], stage: str, key: str) -> int:
    counts = ((stages.get(stage) or {}).get("counts")) or {}
    try:
        return int(counts.get(key) or 0)
    except (TypeError, ValueError):
        return 0
