import uuid
import io
import json

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from minio import Minio
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db.session import get_db
from app.models.clip_job import ClipJob
from app.models.billing_product import BillingProduct
from app.models.purchase import Purchase
from app.models.clip_asset import ClipAsset
from app.models.enums import BillingProvider, ClipAssetType, ProductType, PurchaseStatus
from app.models.user import User
from app.security.auth_middleware import get_current_user
from app.security.access_control import can_bypass_credits, is_admin, scope_job_query
from app.models.enums import JobStatus
from app.services.asset_url_service import AssetUrlService
from app.services.artifact_content_service import ArtifactContentService
from app.services.audit_service import AuditService

router = APIRouter()
asset_url_service = AssetUrlService()
artifact_content_service = ArtifactContentService()
audit_service = AuditService()
artifact_storage_client = Minio(
    settings.minio_endpoint,
    access_key=settings.minio_access_key,
    secret_key=settings.minio_secret_key,
    secure=settings.minio_secure,
)


class CreateJobInput(BaseModel):
    source_url: str
    product_id: str | None = None
    clip_mode: str = Field(default="short_serie")
    video_ratio: str = Field(default="portrait")
    build_ia: bool = Field(default=False)
    language_mode: str = Field(default="auto")
    output_language: str | None = None
    subtitle_language: str | None = None
    prompt_mode: str = Field(default="manual")


class SubmitAiResponseInput(BaseModel):
    response_json: dict


def _expected_artifact_keys(job_id: str) -> dict[str, str]:
    return {
        "transcript": f"jobs/{job_id}/transcript.json",
        "transcript_with_speakers": f"jobs/{job_id}/transcript_with_speakers.json",
        "speaker_turns": f"jobs/{job_id}/speaker_turns.json",
        "candidates": f"jobs/{job_id}/candidates.json",
        "span_catalog": f"jobs/{job_id}/span_catalog.json",
        "hook_candidates": f"jobs/{job_id}/hook_candidates.json",
        "language_detection": f"jobs/{job_id}/language_detection.json",
        "prompt": f"jobs/{job_id}/prompt.txt",
        "ai_response": f"jobs/{job_id}/ai_output.json",
        "qa_report": f"jobs/{job_id}/qa_report.json",
        "delivery_package": f"jobs/{job_id}/delivery_package.json",
        "artifacts_manifest": f"jobs/{job_id}/artifacts_manifest.json",
        "runtime_status": f"jobs/{job_id}/runtime_status.json",
    }


def _job_config(job: ClipJob) -> dict:
    metadata = dict(job.metadata_json or {})
    config = dict(metadata.get("job_config") or {})
    return {
        "clip_mode": str(config.get("clip_mode") or metadata.get("clip_mode") or "short_serie"),
        "video_ratio": str(config.get("video_ratio") or metadata.get("video_ratio") or "portrait"),
        "build_ia": bool(config.get("build_ia") if config.get("build_ia") is not None else metadata.get("build_ia", False)),
        "language_mode": str(config.get("language_mode") or metadata.get("language_mode") or "auto"),
        "output_language": config.get("output_language") or metadata.get("output_language"),
        "subtitle_language": config.get("subtitle_language") or metadata.get("subtitle_language"),
        "prompt_mode": str(config.get("prompt_mode") or job.prompt_mode or "manual"),
    }


def _ensure_internal_default_product(db: Session) -> BillingProduct:
    product = (
        db.query(BillingProduct)
        .filter(BillingProduct.code == ProductType.VIDEO_UP_TO_4H)
        .order_by(BillingProduct.created_at.asc())
        .first()
    )
    if product:
        return product

    product = BillingProduct(
        code=ProductType.VIDEO_UP_TO_4H,
        name=settings.internal_default_product_name,
        description=settings.internal_default_product_description,
        currency="BRL",
        price_amount=1.00,
        max_video_duration_sec=settings.internal_default_product_max_video_duration_sec,
        max_shorts_generated=settings.internal_default_product_max_shorts_generated,
        is_active=True,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _ensure_internal_purchase(
    db: Session,
    *,
    user_id,
    product: BillingProduct,
) -> Purchase:
    purchase = Purchase(
        user_id=user_id,
        product_id=product.id,
        billing_provider=BillingProvider.MANUAL,
        status=PurchaseStatus.PAID,
        currency=product.currency,
        amount_total=product.price_amount,
        provider_payment_id="internal-default",
        provider_checkout_url=None,
        provider_raw_payload={
            "kind": "internal_default_purchase",
            "reason": "job_created_without_checkout",
        },
    )
    db.add(purchase)
    db.commit()
    db.refresh(purchase)
    return purchase


def _serialize_asset(asset: ClipAsset) -> dict:
    return {
        "id": str(asset.id),
        "type": asset.asset_type.value,
        "status": asset.status.value,
        "order": asset.order_index,
        "title": asset.title,
        "description": asset.description,
        "url": asset_url_service.build_signed_url(asset.storage_key) or asset.public_url,
        "storage_key": asset.storage_key,
        "start": float(asset.start_sec),
        "end": float(asset.end_sec),
        "duration": float(asset.duration_sec),
        "thumbnail_text": asset.thumbnail_text,
        "hashtags": asset.hashtags_json,
        "extra": asset.extra_json,
    }


def _clip_review_summary(assets: list[ClipAsset]) -> dict[str, int]:
    summary = {
        "total": 0,
        "approved": 0,
        "rejected": 0,
        "needs_changes": 0,
        "pending": 0,
    }

    for asset in assets:
        summary["total"] += 1
        review = (asset.extra_json or {}).get("review", {}) if asset.extra_json else {}
        decision = review.get("decision")
        if decision in {"approved", "rejected", "needs_changes"}:
            summary[decision] += 1
        else:
            summary["pending"] += 1

    return summary


def _enrich_delivery_package(job_id: str, payload: dict | None) -> dict:
    package = dict(payload or {})

    videos = []
    for video in package.get("videos") or []:
        item = dict(video or {})
        file_name = item.get("final_file_name")
        if file_name:
            storage_key = f"jobs/{job_id}/final_clips/{file_name}"
            item["final_url"] = asset_url_service.build_signed_url(storage_key)
            item["final_storage_key"] = storage_key
        videos.append(item)
    package["videos"] = videos

    final_assets = dict(package.get("final_assets") or {})
    final_clips = []
    for clip in final_assets.get("final_clips") or []:
        item = dict(clip or {})
        file_name = item.get("file_name")
        if file_name:
            storage_key = f"jobs/{job_id}/final_clips/{file_name}"
            item["storage_key"] = storage_key
            item["url"] = asset_url_service.build_signed_url(storage_key)
        final_clips.append(item)
    final_assets["final_clips"] = final_clips
    package["final_assets"] = final_assets
    return package


@router.post("/jobs")
def create_job(
    payload: CreateJobInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    product = None
    if payload.product_id:
        product = db.query(BillingProduct).filter(BillingProduct.id == payload.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail="Invalid product")
    else:
        product = (
            db.query(BillingProduct)
            .filter(BillingProduct.is_active == True)
            .order_by(BillingProduct.price_amount.asc(), BillingProduct.created_at.asc())
            .first()
        )
        if not product:
            product = db.query(BillingProduct).order_by(BillingProduct.created_at.asc()).first()
        if not product:
            product = _ensure_internal_default_product(db)

    if user.credits <= 0 and not can_bypass_credits(user):
        raise HTTPException(status_code=402, detail="No credits")

    purchase = _ensure_internal_purchase(
        db,
        user_id=user.id,
        product=product,
    )

    job = ClipJob(
        user_id=user.id,
        purchase_id=purchase.id,
        product_id=product.id,
        source_url=payload.source_url,
        status=JobStatus.QUEUED,
        pipeline_stage="prepare",
        prompt_mode=payload.prompt_mode,
        queued_at=datetime.now(timezone.utc),
        metadata_json={
            "job_config": {
                "clip_mode": payload.clip_mode,
                "video_ratio": payload.video_ratio,
                "build_ia": payload.build_ia,
                "language_mode": payload.language_mode,
                "output_language": payload.output_language,
                "subtitle_language": payload.subtitle_language,
                "prompt_mode": payload.prompt_mode,
            }
        },
    )

    db.add(job)

    if not can_bypass_credits(user):
        user.credits -= 1
        if user.credits == 0:
            user.token_version += 1

    db.commit()
    db.refresh(job)

    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "job_config": _job_config(job),
    }


@router.get("/jobs")
def list_jobs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    jobs = scope_job_query(db.query(ClipJob), user, ClipJob).all()

    return [
        {
            "id": str(j.id),
            "status": j.status.value,
            "pipeline_stage": j.pipeline_stage,
            "source_url": j.source_url,
            "created_at": j.created_at,
            "job_config": _job_config(j),
            "metadata": {
                **(j.metadata_json or {}),
                "review_summary": _clip_review_summary(
                    db.query(ClipAsset)
                    .filter(
                        ClipAsset.job_id == j.id,
                        ClipAsset.asset_type.in_([ClipAssetType.SHORT_CLIP, ClipAssetType.MERGED_CLIP]),
                    )
                    .all()
                ),
            },
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
        scope_job_query(db.query(ClipJob), user, ClipJob)
        .filter(ClipJob.id == job_id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404)

    return {
        "id": str(job.id),
        "status": job.status.value,
        "source_url": job.source_url,
        "pipeline_stage": job.pipeline_stage,
        "created_at": job.created_at,
        "job_config": _job_config(job),
        "artifact_keys": {
            **_expected_artifact_keys(str(job.id)),
            "transcript": job.transcript_storage_key or _expected_artifact_keys(str(job.id))["transcript"],
            "transcript_with_speakers": job.transcript_with_speakers_storage_key or _expected_artifact_keys(str(job.id))["transcript_with_speakers"],
            "speaker_turns": job.speaker_turns_storage_key or _expected_artifact_keys(str(job.id))["speaker_turns"],
            "candidates": job.candidates_storage_key or _expected_artifact_keys(str(job.id))["candidates"],
            "prompt": job.prompt_storage_key or _expected_artifact_keys(str(job.id))["prompt"],
            "ai_response": job.ai_response_storage_key or _expected_artifact_keys(str(job.id))["ai_response"],
            "qa_report": job.qa_report_storage_key or _expected_artifact_keys(str(job.id))["qa_report"],
            "delivery_package": job.delivery_package_storage_key or _expected_artifact_keys(str(job.id))["delivery_package"],
            "artifacts_manifest": job.artifacts_manifest_storage_key or _expected_artifact_keys(str(job.id))["artifacts_manifest"],
            "runtime_status": job.runtime_status_storage_key or _expected_artifact_keys(str(job.id))["runtime_status"],
        },
        "metadata": job.metadata_json,
    }


@router.get("/jobs/{job_id}/assets")
def job_assets(
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
        raise HTTPException(status_code=404)

    assets = (
        db.query(ClipAsset)
        .filter(ClipAsset.job_id == job.id)
        .all()
    )

    return [
        _serialize_asset(a)
        for a in assets
    ]


@router.get("/jobs/{job_id}/delivery-package")
def job_delivery_package(
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
        raise HTTPException(status_code=404)

    delivery_package_content = artifact_content_service.load_json(job.delivery_package_storage_key)
    package_payload = _enrich_delivery_package(
        str(job.id),
        delivery_package_content if isinstance(delivery_package_content, dict) else {},
    )

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
        "job_config": _job_config(job),
        "delivery_package": {
            "storage_key": job.delivery_package_storage_key or (delivery_asset.storage_key if delivery_asset else None),
            "url": asset_url_service.build_signed_url(
                job.delivery_package_storage_key or (delivery_asset.storage_key if delivery_asset else None)
            ) or (delivery_asset.public_url if delivery_asset else None),
        },
        "videos": package_payload.get("videos") or [],
        "final_assets": package_payload.get("final_assets") or {},
        "language": package_payload.get("language") or {},
        "response_validation": package_payload.get("response_validation") or {},
        "qa_report": {
            "storage_key": job.qa_report_storage_key or (qa_asset.storage_key if qa_asset else None),
            "url": asset_url_service.build_signed_url(
                job.qa_report_storage_key or (qa_asset.storage_key if qa_asset else None)
            ) or (qa_asset.public_url if qa_asset else None),
        },
        "artifact_keys": {
            **_expected_artifact_keys(str(job.id)),
            "transcript": job.transcript_storage_key or _expected_artifact_keys(str(job.id))["transcript"],
            "transcript_with_speakers": job.transcript_with_speakers_storage_key or _expected_artifact_keys(str(job.id))["transcript_with_speakers"],
            "speaker_turns": job.speaker_turns_storage_key or _expected_artifact_keys(str(job.id))["speaker_turns"],
            "candidates": job.candidates_storage_key or _expected_artifact_keys(str(job.id))["candidates"],
            "prompt": job.prompt_storage_key or _expected_artifact_keys(str(job.id))["prompt"],
            "ai_response": job.ai_response_storage_key or _expected_artifact_keys(str(job.id))["ai_response"],
            "qa_report": job.qa_report_storage_key or _expected_artifact_keys(str(job.id))["qa_report"],
            "delivery_package": job.delivery_package_storage_key or _expected_artifact_keys(str(job.id))["delivery_package"],
            "artifacts_manifest": job.artifacts_manifest_storage_key or _expected_artifact_keys(str(job.id))["artifacts_manifest"],
            "runtime_status": job.runtime_status_storage_key or _expected_artifact_keys(str(job.id))["runtime_status"],
        },
        "clips": [
            {
                "id": str(asset.id),
                "type": asset.asset_type.value,
                "order": asset.order_index,
                "title": asset.title,
                "description": asset.description,
                "storage_key": asset.storage_key,
                "url": asset_url_service.build_signed_url(asset.storage_key) or asset.public_url,
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


@router.post("/jobs/{job_id}/submit-ai-response")
def submit_ai_response(
    job_id: str,
    payload: SubmitAiResponseInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = (
        scope_job_query(db.query(ClipJob), user, ClipJob)
        .filter(ClipJob.id == job_id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    storage_key = _expected_artifact_keys(str(job.id))["ai_response"]
    content = json.dumps(payload.response_json, ensure_ascii=False, indent=2).encode("utf-8")
    artifact_storage_client.put_object(
        settings.worker_artifacts_bucket,
        storage_key,
        io.BytesIO(content),
        len(content),
        content_type="application/json",
    )

    job.ai_response_storage_key = storage_key
    job.pipeline_stage = "finalize"
    job.status = JobStatus.QUEUED
    job.error_message = None

    metadata = dict(job.metadata_json or {})
    metadata["manual_finalize"] = {
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "submitted_by_user_id": str(user.id),
    }
    job.metadata_json = metadata
    db.add(job)

    audit_service.log(
        db,
        action="job.submit_ai_response",
        outcome="success",
        actor_user=user,
        target_type="clip_job",
        target_id=str(job.id),
        metadata={
            "storage_key": storage_key,
            "pipeline_stage": "finalize",
        },
    )

    db.commit()
    db.refresh(job)

    return {
        "status": "queued",
        "job_id": str(job.id),
        "pipeline_stage": job.pipeline_stage,
        "artifact_key": storage_key,
    }


@router.post("/jobs/{job_id}/clips/{asset_id}/review")
def review_clip(
    job_id: str,
    asset_id: str,
    decision: str,
    note: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    if decision not in {"approved", "rejected", "needs_changes"}:
        raise HTTPException(status_code=400, detail="Invalid decision")

    asset = (
        db.query(ClipAsset)
        .join(ClipJob)
        .filter(
            ClipAsset.id == asset_id,
            ClipAsset.job_id == job_id,
            ClipAsset.asset_type.in_([ClipAssetType.SHORT_CLIP, ClipAssetType.MERGED_CLIP]),
        )
        .first()
    )
    if asset and not is_admin(user) and asset.job.user_id != user.id:
        asset = None

    if not asset:
        raise HTTPException(status_code=404, detail="Clip not found")

    extra = dict(asset.extra_json or {})
    extra["review"] = {
        "decision": decision,
        "note": note,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    asset.extra_json = extra

    db.add(asset)
    audit_service.log(
        db,
        action="clip.review",
        outcome="success",
        actor_user=user,
        target_type="clip_asset",
        target_id=str(asset.id),
        metadata={
            "job_id": str(job_id),
            "decision": decision,
            "admin_actor": is_admin(user),
        },
    )
    db.commit()
    db.refresh(asset)

    return {
        "status": "ok",
        "asset": _serialize_asset(asset),
    }


@router.post("/jobs/{job_id}/review/approve-all")
def approve_all_clips(
    job_id: str,
    note: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    job = (
        scope_job_query(db.query(ClipJob), user, ClipJob)
        .filter(ClipJob.id == job_id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    assets = (
        db.query(ClipAsset)
        .filter(
            ClipAsset.job_id == job.id,
            ClipAsset.asset_type.in_([ClipAssetType.SHORT_CLIP, ClipAssetType.MERGED_CLIP]),
        )
        .all()
    )

    if not assets:
        raise HTTPException(status_code=400, detail="Job has no clips to approve")

    reviewed_at = datetime.now(timezone.utc).isoformat()
    for asset in assets:
        extra = dict(asset.extra_json or {})
        extra["review"] = {
            "decision": "approved",
            "note": note,
            "reviewed_at": reviewed_at,
            "bulk_action": True,
        }
        asset.extra_json = extra
        db.add(asset)

    metadata = dict(job.metadata_json or {})
    metadata["review"] = {
        "all_clips_approved": True,
        "ready_for_publication": True,
        "approved_at": reviewed_at,
        "note": note,
    }
    job.metadata_json = metadata
    db.add(job)
    audit_service.log(
        db,
        action="job.review.approve_all",
        outcome="success",
        actor_user=user,
        target_type="clip_job",
        target_id=str(job.id),
        metadata={
            "approved_clip_count": len(assets),
            "admin_actor": is_admin(user),
        },
    )

    db.commit()
    db.refresh(job)

    return {
        "status": "ok",
        "job_id": str(job.id),
        "review_summary": _clip_review_summary(assets),
        "ready_for_publication": True,
    }


@router.get("/jobs/{job_id}/editorial-context")
def job_editorial_context(
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
        raise HTTPException(status_code=404)

    transcript_with_speakers = artifact_content_service.load_json(
        job.transcript_with_speakers_storage_key
    )
    qa_report = artifact_content_service.load_json(job.qa_report_storage_key)
    speaker_turns = artifact_content_service.load_json(job.speaker_turns_storage_key)
    expected = _expected_artifact_keys(str(job.id))
    span_catalog = artifact_content_service.load_json(expected["span_catalog"])
    hook_candidates = artifact_content_service.load_json(expected["hook_candidates"])
    language_detection = artifact_content_service.load_json(expected["language_detection"])

    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "pipeline_stage": job.pipeline_stage,
        "job_config": _job_config(job),
        "qa_report": qa_report if isinstance(qa_report, dict) else None,
        "transcript_with_speakers": transcript_with_speakers if isinstance(transcript_with_speakers, list) else [],
        "speaker_turns": speaker_turns if isinstance(speaker_turns, list) else [],
        "span_catalog": span_catalog if isinstance(span_catalog, list) else [],
        "hook_candidates": hook_candidates if isinstance(hook_candidates, list) else [],
        "language_detection": language_detection if isinstance(language_detection, dict) else {},
        "artifact_keys": expected,
        "metadata": job.metadata_json,
    }
