from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from minio import Minio
from minio.error import S3Error
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.clip_asset import ClipAsset
from app.models.clip_job import ClipJob
from app.models.enums import AssetStatus, ClipAssetType, JobEventType, JobStatus
from app.models.job_event import JobEvent


JOB_ARTIFACT_FIELDS = {
    "transcript_storage_key": "jobs/{job_id}/transcript.json",
    "transcript_with_speakers_storage_key": "jobs/{job_id}/transcript_with_speakers.json",
    "speaker_turns_storage_key": "jobs/{job_id}/speaker_turns.json",
    "candidates_storage_key": "jobs/{job_id}/candidates.json",
    "prompt_storage_key": "jobs/{job_id}/prompt.txt",
    "ai_response_storage_key": "jobs/{job_id}/ai_output.json",
    "qa_report_storage_key": "jobs/{job_id}/qa_report.json",
    "delivery_package_storage_key": "jobs/{job_id}/delivery_package.json",
    "artifacts_manifest_storage_key": "jobs/{job_id}/artifacts_manifest.json",
    "runtime_status_storage_key": "jobs/{job_id}/runtime_status.json",
}

SINGLE_ASSET_TYPES = {
    "transcript_storage_key": ClipAssetType.TRANSCRIPT,
    "transcript_with_speakers_storage_key": ClipAssetType.TRANSCRIPT_WITH_SPEAKERS,
    "speaker_turns_storage_key": ClipAssetType.SPEAKER_TURNS,
    "candidates_storage_key": ClipAssetType.CANDIDATES,
    "prompt_storage_key": ClipAssetType.PROMPT,
    "ai_response_storage_key": ClipAssetType.AI_RESPONSE,
    "qa_report_storage_key": ClipAssetType.QA_REPORT,
    "delivery_package_storage_key": ClipAssetType.DELIVERY_PACKAGE,
    "artifacts_manifest_storage_key": ClipAssetType.ARTIFACTS_MANIFEST,
    "runtime_status_storage_key": ClipAssetType.RUNTIME_STATUS,
}

RUNTIME_STEP_TO_EVENT = {
    "download_video": JobEventType.DOWNLOAD_FINISHED,
    "transcribe": JobEventType.TRANSCRIPTION_FINISHED,
    "diarization": JobEventType.DIARIZATION_FINISHED,
    "validate_ai_response": JobEventType.LLM_REQUEST_FINISHED,
    "render_cuts": JobEventType.RENDER_FINISHED,
    "qa": JobEventType.QA_FINISHED,
    "delivery_package": JobEventType.DELIVERY_PACKAGE_READY,
}


class JobArtifactSyncService:

    def __init__(self):
        self.bucket = settings.worker_artifacts_bucket
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=False,
        )

    def sync_job(
        self,
        db: Session,
        job: ClipJob,
        pipeline_stage: str | None = None,
        status: JobStatus | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        found_artifacts: dict[str, str] = {}
        delivery_package: dict[str, Any] | None = None
        qa_report: dict[str, Any] | None = None
        runtime_status: dict[str, Any] | None = None
        artifacts_manifest: dict[str, Any] | None = None

        for field_name, template in JOB_ARTIFACT_FIELDS.items():
            object_name = template.format(job_id=job.id)
            if self._object_exists(object_name):
                setattr(job, field_name, object_name)
                found_artifacts[field_name] = object_name

        runtime_status = self._load_json(job.runtime_status_storage_key)
        artifacts_manifest = self._load_json(job.artifacts_manifest_storage_key)
        delivery_package = self._load_json(job.delivery_package_storage_key)
        qa_report = self._load_json(job.qa_report_storage_key)

        if pipeline_stage:
            job.pipeline_stage = pipeline_stage
        else:
            inferred_stage = self._infer_pipeline_stage(found_artifacts, runtime_status)
            if inferred_stage:
                job.pipeline_stage = inferred_stage

        if status:
            job.status = status
        else:
            inferred_status = self._infer_status(found_artifacts, job)
            if inferred_status is not None:
                job.status = inferred_status

        if error_message is not None:
            job.error_message = error_message

        now = datetime.now(timezone.utc)
        if job.started_at is None and status in {JobStatus.PREPARING, JobStatus.FINALIZING, JobStatus.COMPLETED}:
            job.started_at = now
        if status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
            job.finished_at = now

        self._sync_single_assets(db, job, found_artifacts)

        if isinstance(delivery_package, dict):
            self._sync_clip_assets(db, job, delivery_package, qa_report if isinstance(qa_report, dict) else None)
        self._merge_job_metadata(
            job,
            delivery_package if isinstance(delivery_package, dict) else None,
            qa_report if isinstance(qa_report, dict) else None,
            runtime_status if isinstance(runtime_status, dict) else None,
            artifacts_manifest if isinstance(artifacts_manifest, dict) else None,
        )
        self._sync_job_events(
            db,
            job,
            found_artifacts=found_artifacts,
            runtime_status=runtime_status if isinstance(runtime_status, dict) else None,
            qa_report=qa_report if isinstance(qa_report, dict) else None,
            delivery_package=delivery_package if isinstance(delivery_package, dict) else None,
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        return {
            "job_id": str(job.id),
            "status": job.status.value,
            "pipeline_stage": job.pipeline_stage,
            "synced_artifacts": found_artifacts,
            "delivery_package_found": isinstance(delivery_package, dict),
            "qa_report_found": isinstance(qa_report, dict),
            "runtime_status_found": isinstance(runtime_status, dict),
        }

    def _sync_single_assets(
        self,
        db: Session,
        job: ClipJob,
        found_artifacts: dict[str, str],
    ) -> None:
        for field_name, object_name in found_artifacts.items():
            asset_type = SINGLE_ASSET_TYPES.get(field_name)
            if asset_type is None:
                continue

            asset = (
                db.query(ClipAsset)
                .filter(
                    ClipAsset.job_id == job.id,
                    ClipAsset.asset_type == asset_type,
                    ClipAsset.storage_key == object_name,
                )
                .first()
            )

            if asset is None:
                asset = ClipAsset(
                    job_id=job.id,
                    asset_type=asset_type,
                    status=AssetStatus.READY,
                    order_index=0,
                    storage_key=object_name,
                    start_sec=Decimal("0"),
                    end_sec=Decimal("0"),
                    duration_sec=Decimal("0"),
                )

            asset.status = AssetStatus.READY
            asset.storage_key = object_name
            db.add(asset)

    def _sync_clip_assets(
        self,
        db: Session,
        job: ClipJob,
        delivery_package: dict[str, Any],
        qa_report: dict[str, Any] | None,
    ) -> None:
        qa_by_file = {
            clip.get("file_name"): clip
            for clip in (qa_report or {}).get("clips", [])
            if clip.get("file_name")
        }

        for clip in delivery_package.get("clips", []):
            file_name = clip.get("file_name")
            if not file_name:
                continue

            storage_key = f"jobs/{job.id}/cuts/{file_name}"
            asset = (
                db.query(ClipAsset)
                .filter(
                    ClipAsset.job_id == job.id,
                    ClipAsset.storage_key == storage_key,
                )
                .first()
            )

            if asset is None:
                asset = ClipAsset(
                    job_id=job.id,
                    asset_type=ClipAssetType.SHORT_CLIP,
                    status=AssetStatus.READY,
                    order_index=int(clip.get("clip_index", 0)),
                    storage_key=storage_key,
                    start_sec=Decimal("0"),
                    end_sec=Decimal("0"),
                    duration_sec=Decimal("0"),
                )

            asset.asset_type = ClipAssetType.SHORT_CLIP
            asset.status = AssetStatus.READY
            asset.order_index = int(clip.get("clip_index", 0))
            asset.title = clip.get("title")
            asset.description = clip.get("description")
            asset.start_sec = Decimal(str(clip.get("start", 0.0)))
            asset.end_sec = Decimal(str(clip.get("end", 0.0)))
            asset.duration_sec = Decimal(str(clip.get("duration", 0.0)))
            asset.merge_group = clip.get("merge_group")
            asset.thumbnail_text = clip.get("thumbnail")
            asset.hashtags_json = clip.get("hashtags", [])
            asset.extra_json = {
                "hook": clip.get("hook"),
                "qa": qa_by_file.get(file_name),
            }
            asset.storage_key = storage_key
            db.add(asset)

    def _merge_job_metadata(
        self,
        job: ClipJob,
        delivery_package: dict[str, Any] | None,
        qa_report: dict[str, Any] | None,
        runtime_status: dict[str, Any] | None,
        artifacts_manifest: dict[str, Any] | None,
    ) -> None:
        metadata = dict(job.metadata_json or {})
        if delivery_package:
            metadata["delivery_status"] = delivery_package.get("delivery_status")
            metadata["qa_decision"] = delivery_package.get("qa_decision")
            metadata["clip_count"] = delivery_package.get("clip_count")
            metadata["clip_mode"] = delivery_package.get("clip_mode")
            metadata["video_ratio"] = delivery_package.get("video_ratio")
            metadata["long_video_script"] = delivery_package.get("long_video_script")

        if qa_report:
            metadata["qa_summary"] = qa_report.get("summary")
            metadata["qa_decision"] = qa_report.get("decision")

        if runtime_status:
            metadata["runtime"] = {
                "pipeline_stage": runtime_status.get("pipeline_stage"),
                "step": runtime_status.get("step"),
                "status": runtime_status.get("status"),
                "updated_at": runtime_status.get("updated_at"),
                "details": runtime_status.get("details", {}),
            }

        if artifacts_manifest:
            artifacts = artifacts_manifest.get("artifacts", {})
            metadata["artifact_count"] = len(artifacts)
            metadata["artifact_names"] = sorted(artifacts.keys())

        job.metadata_json = metadata

    def _sync_job_events(
        self,
        db: Session,
        job: ClipJob,
        found_artifacts: dict[str, str],
        runtime_status: dict[str, Any] | None,
        qa_report: dict[str, Any] | None,
        delivery_package: dict[str, Any] | None,
    ) -> None:
        existing_events = {
            (event.event_type, event.stage, event.message or "")
            for event in job.events
        }

        runtime_step = str(runtime_status.get("step")) if runtime_status else ""
        runtime_state = str(runtime_status.get("status")) if runtime_status else ""
        runtime_details = runtime_status.get("details", {}) if runtime_status else {}

        if runtime_step in RUNTIME_STEP_TO_EVENT and runtime_state in {"completed", "skipped"}:
            self._create_event_once(
                db,
                job,
                existing_events,
                event_type=RUNTIME_STEP_TO_EVENT[runtime_step],
                stage=job.pipeline_stage,
                message=f"{runtime_step}:{runtime_state}",
                payload_json=runtime_details or None,
            )

        if "prompt_storage_key" in found_artifacts and "ai_response_storage_key" not in found_artifacts:
            self._create_event_once(
                db,
                job,
                existing_events,
                event_type=JobEventType.TRANSCRIPTION_FINISHED,
                stage="prepare",
                message="prepare_artifacts_ready",
                payload_json={"awaiting_manual_llm": True},
            )

        if "ai_response_storage_key" in found_artifacts:
            self._create_event_once(
                db,
                job,
                existing_events,
                event_type=JobEventType.LLM_REQUEST_FINISHED,
                stage="finalize",
                message="manual_llm_response_synced",
            )

        if qa_report:
            self._create_event_once(
                db,
                job,
                existing_events,
                event_type=JobEventType.QA_FINISHED,
                stage="finalize",
                message=f"qa:{qa_report.get('decision', 'unknown')}",
                payload_json=qa_report.get("summary"),
            )

        if delivery_package:
            self._create_event_once(
                db,
                job,
                existing_events,
                event_type=JobEventType.DELIVERY_PACKAGE_READY,
                stage="finalize",
                message=f"delivery:{delivery_package.get('delivery_status', 'ready')}",
                payload_json={
                    "clip_count": delivery_package.get("clip_count", 0),
                    "qa_decision": delivery_package.get("qa_decision"),
                },
            )

        if job.status == JobStatus.COMPLETED:
            self._create_event_once(
                db,
                job,
                existing_events,
                event_type=JobEventType.JOB_COMPLETED,
                stage=job.pipeline_stage,
                message="job_completed",
            )
        elif job.status == JobStatus.FAILED:
            self._create_event_once(
                db,
                job,
                existing_events,
                event_type=JobEventType.JOB_FAILED,
                stage=job.pipeline_stage,
                message="job_failed",
                payload_json={"error_message": job.error_message} if job.error_message else None,
            )

    def _create_event_once(
        self,
        db: Session,
        job: ClipJob,
        existing_events: set[tuple[JobEventType, str | None, str]],
        event_type: JobEventType,
        stage: str | None,
        message: str,
        payload_json: dict[str, Any] | None = None,
    ) -> None:
        identity = (event_type, stage, message)
        if identity in existing_events:
            return

        db.add(
            JobEvent(
                job_id=job.id,
                event_type=event_type,
                stage=stage,
                message=message,
                payload_json=payload_json,
            )
        )
        existing_events.add(identity)

    def _infer_pipeline_stage(
        self,
        found_artifacts: dict[str, str],
        runtime_status: dict[str, Any] | None,
    ) -> str | None:
        runtime_stage = runtime_status.get("pipeline_stage") if runtime_status else None
        if isinstance(runtime_stage, str) and runtime_stage:
            return runtime_stage
        if "delivery_package_storage_key" in found_artifacts or "qa_report_storage_key" in found_artifacts:
            return "finalize"
        if found_artifacts:
            return "prepare"
        return None

    def _infer_status(
        self,
        found_artifacts: dict[str, str],
        job: ClipJob,
    ) -> JobStatus | None:
        if "delivery_package_storage_key" in found_artifacts:
            return JobStatus.COMPLETED
        if "ai_response_storage_key" in found_artifacts or job.pipeline_stage == "finalize":
            return JobStatus.FINALIZING
        if "prompt_storage_key" in found_artifacts:
            return JobStatus.AWAITING_MANUAL_LLM
        if found_artifacts:
            return JobStatus.PREPARING
        return None

    def _object_exists(self, object_name: str) -> bool:
        try:
            self.client.stat_object(self.bucket, object_name)
            return True
        except S3Error:
            return False

    def _load_json(self, object_name: str | None) -> dict[str, Any] | list[Any] | None:
        if not object_name:
            return None

        try:
            response = self.client.get_object(self.bucket, object_name)
            data = json.loads(response.read().decode("utf-8"))
            response.close()
            response.release_conn()
            return data
        except Exception:
            return None
