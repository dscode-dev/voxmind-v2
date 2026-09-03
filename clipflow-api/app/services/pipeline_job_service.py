"""Creation and lookup of PipelineJobs — the single place a run comes into existence.

Three producers enqueue work: the API (`POST /jobs`), the Telegram control-plane, and the
private scheduler. Before PR-STATE-01 each built its own payload and pushed it to Redis, and
none of them created a PipelineJob — the model existed and nothing wrote to it. Putting
creation here means the three producers share one definition of what starting a run means,
instead of three drifting copies.

**Identity.** Three ids are in play and they are not interchangeable:

* ``worker_job_id`` — the id in the queue payload and the MinIO prefix (``jobs/<id>/...``).
  For an API job it is the ``ClipJob.id``; for a Telegram job it is a UUID the bot minted
  and no ``ClipJob`` exists at all. It identifies *where the bytes live*.
* ``PipelineJob.id`` — identifies *the run*. One per logical execution, reused across the
  reliable queue's retry attempts (``retry_count`` records which attempt is current), so a
  run's history stays in one place instead of fragmenting per delivery.
* ``ClipJob.id`` — the billing-coupled customer job. Optional: it does not exist for
  Telegram-originated runs, which is exactly why PipelineJob cannot hang off it.

A PipelineJob is therefore correlatable to every worker execution, whether or not a ClipJob
exists, and ``worker_job_id`` is the join key the worker already knows.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.enums import PipelineState
from app.models.pipeline_job import PipelineJob
from app.services.pipeline_state_machine import PipelineStateMachine

state_machine = PipelineStateMachine()


class PipelineJobService:
    """Creates runs and resolves them by any of the ids a caller might hold."""

    def create_for_enqueue(
        self,
        db: Session,
        *,
        worker_job_id: str,
        source_url: str | None,
        pipeline_stage: str = "prepare",
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
        preset_id: str | None = None,
        source_storage_key: str | None = None,
        origin: str = "api",
        metadata: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> PipelineJob:
        """Create a run and move it to QUEUED.

        Called *before* the payload reaches Redis, so a claimed job always has a run to
        report against. The reverse order would leave a window in which the worker holds a
        ``pipeline_job_id`` that resolves to nothing.
        """
        job = PipelineJob(
            worker_job_id=str(worker_job_id),
            source_url=source_url,
            source_storage_key=source_storage_key,
            pipeline_stage=pipeline_stage,
            clip_mode=clip_mode,
            video_ratio=video_ratio,
            preset_id=preset_id,
            state=PipelineState.SELECTED,
            metadata_json={
                "origin": origin,
                "worker_job_id": str(worker_job_id),
                **(metadata or {}),
            },
        )
        db.add(job)
        db.flush()

        state_machine.transition(
            db,
            job,
            PipelineState.QUEUED,
            service=origin,
            message=f"queued by {origin}",
            payload={"worker_job_id": str(worker_job_id), "pipeline_stage": pipeline_stage},
        )

        if commit:
            db.commit()
            db.refresh(job)
        return job

    def requeue(
        self,
        db: Session,
        job: PipelineJob,
        *,
        origin: str = "api",
        reason: str = "stage_advance",
        commit: bool = True,
    ) -> PipelineJob:
        """Return an existing run to the queue for its next stage or attempt."""
        state_machine.requeue(db, job, reason=reason, service=origin)
        if commit:
            db.commit()
            db.refresh(job)
        return job

    # ------------------------------------------------------------------ lookup

    def get(self, db: Session, pipeline_job_id: str | uuid.UUID) -> PipelineJob | None:
        parsed = _as_uuid(pipeline_job_id)
        if parsed is None:
            return None
        return db.query(PipelineJob).filter(PipelineJob.id == parsed).first()

    def get_for_update(self, db: Session, pipeline_job_id: str | uuid.UUID) -> PipelineJob | None:
        """Load a run with a row lock held for the rest of the transaction.

        Two workers, or one worker retrying an HTTP call, can report concurrently. Without
        the lock the classic interleaving applies: A reads RENDERING and writes QA while B,
        holding a stale read, writes CUTTING last and wins. The lock serialises the
        read-decide-write, so the second report re-reads the state the first one committed
        and is correctly classified as stale.
        """
        parsed = _as_uuid(pipeline_job_id)
        if parsed is None:
            return None
        return (
            db.query(PipelineJob)
            .filter(PipelineJob.id == parsed)
            .with_for_update()
            .first()
        )

    def get_by_worker_job_id(self, db: Session, worker_job_id: str) -> PipelineJob | None:
        """Resolve the most recent run for a queue/MinIO job id."""
        if not worker_job_id:
            return None
        return (
            db.query(PipelineJob)
            .filter(PipelineJob.worker_job_id == str(worker_job_id))
            .order_by(PipelineJob.created_at.desc())
            .first()
        )

    def resolve(
        self,
        db: Session,
        *,
        pipeline_job_id: str | None = None,
        worker_job_id: str | None = None,
        for_update: bool = False,
    ) -> PipelineJob | None:
        """Find a run by its own id, falling back to the queue id.

        The fallback is what lets a legacy payload — enqueued before this PR and carrying no
        ``pipeline_job_id`` — still be correlated if a run happens to exist for it. It never
        invents one.
        """
        if pipeline_job_id:
            job = (
                self.get_for_update(db, pipeline_job_id)
                if for_update
                else self.get(db, pipeline_job_id)
            )
            if job is not None:
                return job
        if worker_job_id:
            job = self.get_by_worker_job_id(db, worker_job_id)
            if job is not None and for_update:
                return self.get_for_update(db, job.id)
            return job
        return None

    def serialize(self, job: PipelineJob) -> dict[str, Any]:
        metadata = dict(job.metadata_json or {})
        return {
            "pipeline_job_id": str(job.id),
            "state": job.state.value,
            "worker_job_id": job.worker_job_id,
            "pipeline_stage": job.pipeline_stage,
            "attempt": (job.retry_count or 0) + 1,
            "retry_count": job.retry_count or 0,
            "max_retries": job.max_retries,
            "queued_at": job.queued_at.isoformat() if job.queued_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "error_message": job.error_message,
            "publication_eligibility": metadata.get("publication_eligibility"),
            "origin": metadata.get("origin"),
        }


def _as_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
