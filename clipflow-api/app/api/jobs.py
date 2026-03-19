import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.clip_job import ClipJob
from app.models.billing_product import BillingProduct
from app.models.clip_asset import ClipAsset
from app.models.enums import ClipAssetType
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
        "artifact_keys": {
            "transcript": job.transcript_storage_key,
            "transcript_with_speakers": job.transcript_with_speakers_storage_key,
            "speaker_turns": job.speaker_turns_storage_key,
            "candidates": job.candidates_storage_key,
            "prompt": job.prompt_storage_key,
            "ai_response": job.ai_response_storage_key,
            "qa_report": job.qa_report_storage_key,
            "delivery_package": job.delivery_package_storage_key,
            "artifacts_manifest": job.artifacts_manifest_storage_key,
            "runtime_status": job.runtime_status_storage_key,
        },
        "metadata": job.metadata_json,
    }


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


@router.get("/jobs/{job_id}/delivery-package")
def job_delivery_package(
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

    delivery_asset = next(
        (asset for asset in assets if asset.asset_type == ClipAssetType.DELIVERY_PACKAGE),
        None,
    )
    qa_asset = next(
        (asset for asset in assets if asset.asset_type == ClipAssetType.QA_REPORT),
        None,
    )
    clips = [
        asset
        for asset in assets
        if asset.asset_type in {ClipAssetType.SHORT_CLIP, ClipAssetType.MERGED_CLIP}
    ]

    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "pipeline_stage": job.pipeline_stage,
        "delivery_package": {
            "storage_key": job.delivery_package_storage_key or (delivery_asset.storage_key if delivery_asset else None),
            "url": delivery_asset.public_url if delivery_asset else None,
        },
        "qa_report": {
            "storage_key": job.qa_report_storage_key or (qa_asset.storage_key if qa_asset else None),
            "url": qa_asset.public_url if qa_asset else None,
        },
        "artifact_keys": {
            "transcript": job.transcript_storage_key,
            "transcript_with_speakers": job.transcript_with_speakers_storage_key,
            "speaker_turns": job.speaker_turns_storage_key,
            "candidates": job.candidates_storage_key,
            "prompt": job.prompt_storage_key,
            "ai_response": job.ai_response_storage_key,
            "qa_report": job.qa_report_storage_key,
            "delivery_package": job.delivery_package_storage_key,
            "artifacts_manifest": job.artifacts_manifest_storage_key,
            "runtime_status": job.runtime_status_storage_key,
        },
        "clips": [
            {
                "id": str(asset.id),
                "type": asset.asset_type.value,
                "order": asset.order_index,
                "title": asset.title,
                "description": asset.description,
                "storage_key": asset.storage_key,
                "url": asset.public_url,
                "start": float(asset.start_sec),
                "end": float(asset.end_sec),
                "duration": float(asset.duration_sec),
                "merge_group": asset.merge_group,
                "thumbnail_text": asset.thumbnail_text,
                "hashtags": asset.hashtags_json,
                "extra": asset.extra_json,
            }
            for asset in clips
        ],
        "metadata": job.metadata_json,
    }
