import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, get_db
from app.models.clip_job import ClipJob
from app.models.job_event import JobEvent
from app.models.enums import JobEventType
from app.security.access_control import is_admin, scope_job_query
from app.security.auth_middleware import get_current_user, require_internal_api_token
from app.services.audit_service import AuditService
from app.models.user import User

router = APIRouter()
audit_service = AuditService()



@router.get("/jobs/{job_id}/events")
def list_job_events(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    job = (
        scope_job_query(db.query(ClipJob), user, ClipJob)
        .filter(ClipJob.id == job_id)
        .first()
    )

    if not job:
        return []

    events = (
        db.query(JobEvent)
        .filter(JobEvent.job_id == job.id)
        .order_by(JobEvent.created_at.asc())
        .all()
    )

    return [
        {
            "type": e.event_type.name,
            "stage": e.stage,
            "message": e.message,
            "payload": e.payload_json,
            "created_at": e.created_at,
        }
        for e in events
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
        last_signature: tuple[str | None, str | None, int, str | None] | None = None

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

                last_event = (
                    stream_db.query(JobEvent)
                    .filter(JobEvent.job_id == current_job.id)
                    .order_by(JobEvent.created_at.desc())
                    .first()
                )
                event_count = (
                    stream_db.query(JobEvent)
                    .filter(JobEvent.job_id == current_job.id)
                    .count()
                )

                signature = (
                    current_job.status.value,
                    current_job.pipeline_stage,
                    event_count,
                    last_event.event_type.name if last_event else None,
                )

                if signature != last_signature:
                    payload = {
                        "job_id": str(current_job.id),
                        "status": current_job.status.value,
                        "pipeline_stage": current_job.pipeline_stage,
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
                    yield f"event: job_update\ndata: {json.dumps(payload)}\n\n"
                    last_signature = signature
                else:
                    yield ": keepalive\n\n"
            finally:
                stream_db.close()

            if signature[0] in {"completed", "failed", "canceled", "awaiting_manual_llm"}:
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
