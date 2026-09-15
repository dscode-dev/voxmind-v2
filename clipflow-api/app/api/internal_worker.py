from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db.session import get_db
from app.models.clip_job import ClipJob
from app.models.enums import JobStatus
from app.models.job_lease import JobLease
from app.models.user import User
from app.security.auth_middleware import require_internal_api_token
from app.services.audit_service import AuditService
from app.services.job_artifact_sync import JobArtifactSyncService
from app.services.pipeline_job_service import PipelineJobService

router = APIRouter()
artifact_sync_service = JobArtifactSyncService()
audit_service = AuditService()
pipeline_job_service = PipelineJobService()


class RuntimeUpdateInput(BaseModel):
    pipeline_stage: str
    step: str
    status: str
    details: dict | None = None
    worker_id: str | None = None


def _job_config(job: ClipJob) -> dict:
    metadata = dict(job.metadata_json or {})
    config = dict(metadata.get("job_config") or {})
    return {
        "job_preset": config.get("job_preset") or config.get("preset_id") or metadata.get("job_preset"),
        "preset_id": config.get("preset_id") or config.get("job_preset") or metadata.get("preset_id"),
        "render_intent": config.get("render_intent") or metadata.get("render_intent"),
        "clip_mode": str(config.get("clip_mode") or metadata.get("clip_mode") or "short_serie"),
        "video_ratio": str(config.get("video_ratio") or metadata.get("video_ratio") or "portrait"),
        "build_ia": bool(config.get("build_ia") if config.get("build_ia") is not None else metadata.get("build_ia", False)),
        "language_mode": str(config.get("language_mode") or metadata.get("language_mode") or "auto"),
        "output_language": config.get("output_language") or metadata.get("output_language"),
        "subtitle_language": config.get("subtitle_language") or metadata.get("subtitle_language"),
        "prompt_mode": str(config.get("prompt_mode") or job.prompt_mode or "manual"),
    }


@router.post("/internal/worker/next-job")
def next_job(
    worker_id: str,
    _: None = Depends(require_internal_api_token),
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

    job.status = JobStatus.FINALIZING if job.pipeline_stage == "finalize" else JobStatus.PREPARING
    audit_service.log(
        db,
        action="internal.worker.next_job",
        outcome="success",
        target_type="clip_job",
        target_id=str(job.id),
        metadata={"worker_id": worker_id},
    )

    db.commit()

    config = _job_config(job)
    return {
        "job_id": str(job.id),
        "source_url": job.source_url,
        "pipeline_stage": job.pipeline_stage,
        **config,
    }
    
    
@router.post("/internal/jobs/{job_id}/complete")
def complete_job(
    job_id: str,
    sync_artifacts: bool = True,
    _: None = Depends(require_internal_api_token),
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
        audit_service.log(
            db,
            action="internal.worker.complete_job",
            outcome="success",
            target_type="clip_job",
            target_id=str(job.id),
            metadata={"sync_artifacts": False},
        )
        db.commit()

    return {"status": "ok"}


@router.get("/internal/jobs/{job_id}/source")
def get_job_source(
    job_id: str,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    job = db.query(ClipJob).filter(ClipJob.id == job_id).first()

    if not job:
        return {"status": "ignored", "source_url": None}

    return {
        "status": "ok",
        "job_id": str(job.id),
        "source_url": job.source_url,
    }


@router.post("/internal/jobs/{job_id}/sync-artifacts")
def sync_job_artifacts(
    job_id: str,
    pipeline_stage: str | None = None,
    status: JobStatus | None = None,
    error_message: str | None = None,
    _: None = Depends(require_internal_api_token),
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
    audit_service.log(
        db,
        action="internal.worker.sync_artifacts",
        outcome="success",
        target_type="clip_job",
        target_id=str(job.id),
        metadata={
            "pipeline_stage": pipeline_stage,
            "status": status.value if status else None,
            "error_message": error_message,
        },
    )
    db.commit()

    return {"status": "ok", "result": result}


@router.post("/internal/jobs/{job_id}/runtime")
def update_job_runtime(
    job_id: str,
    payload: RuntimeUpdateInput,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    job = db.query(ClipJob).filter(ClipJob.id == job_id).first()

    if not job:
        return {"status": "ignored"}

    metadata = dict(job.metadata_json or {})
    metadata["runtime"] = {
        "pipeline_stage": payload.pipeline_stage,
        "step": payload.step,
        "status": payload.status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "details": payload.details or {},
    }
    job.metadata_json = metadata
    job.pipeline_stage = payload.pipeline_stage

    if payload.pipeline_stage == "prepare":
        job.status = JobStatus.PREPARING
    elif payload.pipeline_stage == "finalize":
        job.status = JobStatus.FINALIZING

    audit_service.log(
        db,
        action="internal.worker.update_runtime",
        outcome="success",
        target_type="clip_job",
        target_id=str(job.id),
        metadata={
            "pipeline_stage": payload.pipeline_stage,
            "step": payload.step,
            "status": payload.status,
            "worker_id": payload.worker_id,
        },
    )
    db.commit()

    return {"status": "ok"}
