from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.clip_job import ClipJob
from app.models.job_event import JobEvent
from app.models.enums import JobEventType
from app.security.auth_middleware import get_current_user
from app.models.user import User

router = APIRouter()



@router.get("/jobs/{job_id}/events")
def list_job_events(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    job = (
        db.query(ClipJob)
        .filter(
            ClipJob.id == job_id,
            ClipJob.user_id == user.id,
        )
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


@router.post("/internal/jobs/{job_id}/events")
def create_job_event(
    job_id: str,
    event_type: JobEventType,
    stage: str | None = None,
    message: str | None = None,
    worker_id: str | None = None,
    payload_json: dict | None = None,
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

    db.commit()

    return {"status": "ok"}