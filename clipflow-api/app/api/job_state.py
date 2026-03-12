from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.clip_job import ClipJob
from app.models.clip_asset import ClipAsset
from app.models.job_event import JobEvent
from app.security.auth_middleware import get_current_user
from app.models.user import User
from app.services.pipeline_progress import calculate_progress

router = APIRouter()


@router.get("/jobs/{job_id}/state")
def job_state(
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
        raise HTTPException(status_code=404)

    assets = (
        db.query(ClipAsset)
        .filter(ClipAsset.job_id == job.id)
        .order_by(ClipAsset.order_index)
        .all()
    )

    events = (
        db.query(JobEvent)
        .filter(JobEvent.job_id == job.id)
        .order_by(JobEvent.created_at)
        .all()
    )

    return {
        "job": {
            "id": str(job.id),
            "status": job.status.value,
            "pipeline_stage": job.pipeline_stage,
            "created_at": job.created_at,
            "source_url": job.source_url,
        },
        "assets": [
            {
                "id": str(a.id),
                "type": a.asset_type.value,
                "status": a.status.value,
                "order": a.order_index,
                "title": a.title,
                "description": a.description,
                "url": a.public_url,
                "start": float(a.start_sec),
                "end": float(a.end_sec),
                "duration": float(a.duration_sec),
                "thumbnail_text": a.thumbnail_text,
                "hashtags": a.hashtags_json,
                "extra": a.extra_json,
            }
            for a in assets
        ],
        "events": [
            {
                "type": e.event_type.value,
                "stage": e.stage,
                "message": e.message,
                "payload": e.payload_json,
                "created_at": e.created_at,
            }
            for e in events
        ],
        "progress": calculate_progress(events),
    }
