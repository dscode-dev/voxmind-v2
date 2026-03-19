from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.clip_job import ClipJob
from app.models.enums import JobStatus
from app.models.job_lease import JobLease
from app.services.job_artifact_sync import JobArtifactSyncService

router = APIRouter()
artifact_sync_service = JobArtifactSyncService()


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
    sync_artifacts: bool = True,
    db: Session = Depends(get_db),
):

    job = db.query(ClipJob).filter(ClipJob.id == job_id).first()

    if not job:
        return {"status": "ignored"}

    if sync_artifacts:
        artifact_sync_service.sync_job(
            db=db,
            job=job,
            pipeline_stage="finalize",
            status=JobStatus.COMPLETED,
        )
    else:
        job.status = JobStatus.COMPLETED
        db.commit()

    return {"status": "ok"}


@router.post("/internal/jobs/{job_id}/sync-artifacts")
def sync_job_artifacts(
    job_id: str,
    pipeline_stage: str | None = None,
    status: JobStatus | None = None,
    error_message: str | None = None,
    db: Session = Depends(get_db),
):

    job = db.query(ClipJob).filter(ClipJob.id == job_id).first()

    if not job:
        return {"status": "ignored"}

    result = artifact_sync_service.sync_job(
        db=db,
        job=job,
        pipeline_stage=pipeline_stage,
        status=status,
        error_message=error_message,
    )

    return {"status": "ok", "result": result}
