from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.clip_job import ClipJob
from app.models.job_lease import JobLease
from app.models.enums import JobStatus

router = APIRouter()


@router.post("/internal/worker/next-job")
def next_job(
    worker_id: str,
    db: Session = Depends(get_db),
):

    job = (
        db.query(ClipJob)
        .filter(ClipJob.status == JobStatus.QUEUED)
        .order_by(ClipJob.created_at)
        .first()
    )

    if not job:
        return {"job": None}

    lease = JobLease(
        job_id=job.id,
        worker_id=worker_id,
    )

    db.add(lease)

    job.status = JobStatus.PREPARING

    db.commit()

    return {
        "job_id": str(job.id),
        "source_url": job.source_url,
    }
    
    
@router.post("/internal/jobs/{job_id}/complete")
def complete_job(
    job_id: str,
    db: Session = Depends(get_db),
):

    job = db.query(ClipJob).filter(ClipJob.id == job_id).first()

    if not job:
        return {"status": "ignored"}

    job.status = JobStatus.COMPLETED

    db.commit()

    return {"status": "ok"}