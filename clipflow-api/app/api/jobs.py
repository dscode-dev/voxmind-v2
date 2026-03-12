import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.clip_job import ClipJob
from app.models.billing_product import BillingProduct
from app.models.user import User
from app.security.auth_middleware import get_current_user
from app.models.enums import JobStatus

router = APIRouter()


@router.post("/jobs")
def create_job(
    source_url: str,
    product_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    product = db.query(BillingProduct).filter(BillingProduct.id == product_id).first()

    if not product:
        raise HTTPException(status_code=404, detail="Invalid product")

    if user.credits <= 0:
        raise HTTPException(status_code=402, detail="No credits")

    job = ClipJob(
        user_id=user.id,
        purchase_id=None,
        product_id=product.id,
        source_url=source_url,
        status=JobStatus.QUEUED,
    )

    db.add(job)

    user.credits -= 1

    if user.credits == 0:
        user.token_version += 1

    db.commit()
    db.refresh(job)

    return {
        "job_id": str(job.id),
        "status": job.status.name,
    }


@router.get("/jobs")
def list_jobs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    jobs = db.query(ClipJob).filter(ClipJob.user_id == user.id).all()

    return [
        {
            "id": str(j.id),
            "status": j.status.name,
            "source_url": j.source_url,
            "created_at": j.created_at,
        }
        for j in jobs
    ]


@router.get("/jobs/{job_id}")
def job_detail(
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

    return {
        "id": str(job.id),
        "status": job.status.name,
        "source_url": job.source_url,
        "pipeline_stage": job.pipeline_stage,
        "created_at": job.created_at,
    }


from app.models.clip_asset import ClipAsset


@router.get("/jobs/{job_id}/assets")
def job_assets(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    assets = (
        db.query(ClipAsset)
        .join(ClipJob)
        .filter(
            ClipJob.id == job_id,
            ClipJob.user_id == user.id,
        )
        .all()
    )

    return [
        {
            "id": str(a.id),
            "type": a.asset_type.value,
            "status": a.status.value,
            "order": a.order_index,
            "title": a.title,
            "description": a.description,
            "url": a.public_url,
            "storage_key": a.storage_key,
            "start": float(a.start_sec),
            "end": float(a.end_sec),
            "duration": float(a.duration_sec),
            "thumbnail_text": a.thumbnail_text,
            "hashtags": a.hashtags_json,
            "extra": a.extra_json,
        }
        for a in assets
    ]
