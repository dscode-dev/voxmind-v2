"""Per-job event history and live stream.

Both endpoints used to call ``JobArtifactSyncService.sync_job`` before answering — the list
once per request, the stream once every two seconds for the lifetime of every connection.
Each of those calls issues eleven ``stat_object`` probes and up to four ``get_object``
downloads, so a single client watching a job produced roughly fifteen MinIO round-trips every
two seconds, and object presence was the thing that decided the job's status.

Since PR-STATE-01 the state is persisted by a validated transition, so reads are database
reads. Artifact reconciliation still exists — it is how a legacy job without a PipelineJob is
interpreted, and how a run can be repaired after the fact — but it is an explicit operation
(``POST /internal/jobs/{id}/sync-artifacts``), not something a read triggers.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, get_db
from app.models.clip_job import ClipJob
from app.models.job_event import JobEvent
from app.models.enums import JobEventType
from app.models.pipeline_event import PipelineEvent
from app.security.access_control import scope_job_query
from app.security.auth_middleware import get_current_user, require_internal_api_token
from app.services.audit_service import AuditService
from app.services.pipeline_job_service import PipelineJobService
from app.models.user import User

router = APIRouter()
audit_service = AuditService()
pipeline_job_service = PipelineJobService()

_FINISHED = {"completed", "failed", "canceled"}


@router.get("/jobs/{job_id}/events")
def list_job_events(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The job's timeline.

    Built from ``PipelineEvent`` when the job has a run: those rows are the recorded
    transitions, so the timeline is what actually happened rather than a reconstruction from
    timestamps and object listings. Legacy jobs fall back to ``JobEvent``.
    """
    job = (
        scope_job_query(db.query(ClipJob), user, ClipJob)
        .filter(ClipJob.id == job_id)
        .first()
    )

    if not job:
        return []

    run = pipeline_job_service.get_by_worker_job_id(db, str(job.id))
    if run is not None:
        events = (
            db.query(PipelineEvent)
            .filter(PipelineEvent.pipeline_job_id == run.id)
            .order_by(PipelineEvent.created_at.asc())
            .all()
        )
        return [
            {
                "type": event.event_type.name,
                "stage": event.stage,
                "message": event.message,
                "payload": event.payload_json,
                "created_at": event.created_at,
                "source": "pipeline_event",
            }
            for event in events
        ]

    legacy = (
        db.query(JobEvent)
        .filter(JobEvent.job_id == job.id)
        .order_by(JobEvent.created_at.asc())
        .all()
    )
    return [
        {
            "type": event.event_type.name,
            "stage": event.stage,
            "message": event.message,
            "payload": event.payload_json,
            "created_at": event.created_at,
            "source": "legacy_job_event",
        }
        for event in legacy
    ]


@router.get("/jobs/{job_id}/stream")
async def stream_job_events(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = (
        scope_job_query(db.query(ClipJob), user, ClipJob)
        .filter(ClipJob.id == job_id)
        .first()
    )

    if not job:
        return StreamingResponse(iter(()), media_type="text/event-stream")

    async def event_generator():
        last_signature: tuple | None = None
        finished = False

        while True:
            if await request.is_disconnected():
                break

            stream_db = SessionLocal()
            try:
                current_job = (
                    scope_job_query(stream_db.query(ClipJob), user, ClipJob)
                    .filter(ClipJob.id == job.id)
                    .first()
                )

                if current_job is None:
                    break

                # Database reads only. No object storage is touched on this path.
                run = pipeline_job_service.get_by_worker_job_id(stream_db, str(current_job.id))
                if run is not None:
                    payload, signature, finished = _run_frame(stream_db, current_job, run)
                else:
                    payload, signature, finished = _legacy_frame(stream_db, current_job)

                if signature != last_signature:
                    yield f"event: job_update\ndata: {json.dumps(payload, default=str)}\n\n"
                    last_signature = signature
                else:
                    yield ": keepalive\n\n"
            finally:
                stream_db.close()

            if finished:
                break

            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _run_frame(db: Session, job: ClipJob, run) -> tuple[dict, tuple, bool]:
    """A frame built from the authoritative run."""
    from app.services.pipeline_state_machine import COMPLETION_STATES, TERMINAL_STATES

    last_event = (
        db.query(PipelineEvent)
        .filter(PipelineEvent.pipeline_job_id == run.id)
        .order_by(PipelineEvent.created_at.desc())
        .first()
    )
    event_count = (
        db.query(PipelineEvent)
        .filter(PipelineEvent.pipeline_job_id == run.id)
        .count()
    )
    view = pipeline_job_service.serialize(run)

    payload = {
        "job_id": str(job.id),
        "status": job.status.value,
        "state": view["state"],
        "state_source": "pipeline",
        "pipeline_job_id": view["pipeline_job_id"],
        "attempt": view["attempt"],
        "pipeline_stage": run.pipeline_stage,
        "publication_eligibility": view["publication_eligibility"],
        "event_count": event_count,
        "last_event": {
            "type": last_event.event_type.name,
            "stage": last_event.stage,
            "message": last_event.message,
            "created_at": last_event.created_at.isoformat(),
        }
        if last_event
        else None,
    }
    signature = (view["state"], view["attempt"], event_count, str(last_event.id) if last_event else None)
    # REVIEW_REQUIRED is a resting state, not a final one, but nothing further will arrive
    # without a human acting, so the stream closes rather than idling forever.
    finished = run.state in TERMINAL_STATES or run.state in COMPLETION_STATES
    return payload, signature, finished


def _legacy_frame(db: Session, job: ClipJob) -> tuple[dict, tuple, bool]:
    """A frame for a job enqueued before runs existed. Still no storage access."""
    last_event = (
        db.query(JobEvent)
        .filter(JobEvent.job_id == job.id)
        .order_by(JobEvent.created_at.desc())
        .first()
    )
    event_count = db.query(JobEvent).filter(JobEvent.job_id == job.id).count()
    runtime = ((job.metadata_json or {}).get("runtime") or {}) if job.metadata_json else {}

    payload = {
        "job_id": str(job.id),
        "status": job.status.value,
        "state": None,
        "state_source": "legacy_artifact_inference",
        "pipeline_job_id": None,
        "pipeline_stage": job.pipeline_stage,
        "runtime": runtime,
        "event_count": event_count,
        "last_event": {
            "type": last_event.event_type.name,
            "stage": last_event.stage,
            "message": last_event.message,
            "created_at": last_event.created_at.isoformat(),
        }
        if last_event
        else None,
    }
    signature = (
        job.status.value,
        job.pipeline_stage,
        event_count,
        runtime.get("updated_at"),
    )
    return payload, signature, job.status.value in _FINISHED


@router.post("/internal/jobs/{job_id}/events")
def create_job_event(
    job_id: str,
    event_type: JobEventType,
    stage: str | None = None,
    message: str | None = None,
    worker_id: str | None = None,
    payload_json: dict | None = None,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):

    job = db.query(ClipJob).filter(ClipJob.id == job_id).first()

    if not job:
        return {"status": "ignored"}

    event = JobEvent(
        job_id=job.id,
        event_type=event_type,
        stage=stage,
        message=message,
        worker_id=worker_id,
        payload_json=payload_json,
    )

    db.add(event)

    if stage:
        job.pipeline_stage = stage

    audit_service.log(
        db,
        action="internal.worker.create_event",
        outcome="success",
        target_type="clip_job",
        target_id=str(job.id),
        metadata={
            "event_type": event_type.value,
            "stage": stage,
            "worker_id": worker_id,
        },
    )

    db.commit()

    return {"status": "ok"}
