import json
import time
import uuid
import redis

from pathlib import Path

from app.integrations.clipflow_api_client import ClipFlowApiClient
from app.observability import configure_logging, get_logger
from app.pipeline.pipeline import Pipeline
from app.settings import settings
from app.storage.minio_client import MinioStorage

configure_logging()
logger = get_logger(__name__)


def _sync_clipflow_api(
    api_client: ClipFlowApiClient,
    job_id: str,
    pipeline_stage: str,
    status: str | None = None,
    error_message: str | None = None,
) -> None:
    result = api_client.sync_job_artifacts_safe(
        job_id=job_id,
        pipeline_stage=pipeline_stage,
        status=status,
        error_message=error_message,
    )
    if result:
        logger.info(
            "ClipFlow API sync completed",
            extra={
                "job_id": job_id,
                "pipeline_stage": pipeline_stage,
                "step": "clipflow_api_sync",
                "status": "completed",
            },
        )


def _upload_if_exists(
    storage: MinioStorage,
    local_path: str | None,
    object_name: str,
) -> bool:
    if not local_path:
        return False

    path = Path(local_path)
    if not path.exists():
        return False

    storage.upload(str(path), object_name)
    return True


def run_pipeline(job: dict):

    video_url = job.get("video_url")
    job_id = job.get("job_id") or str(uuid.uuid4())
    pipeline_stage = job.get("pipeline_stage", "prepare")
    manual_response = job.get("manual_response")

    # ==========================================
    # validações por estágio
    # ==========================================

    if pipeline_stage == "prepare":
        if not video_url:
            logger.error(
                "Prepare job received without video_url",
                extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "validate_job", "status": "failed"},
            )
            return

    if pipeline_stage == "finalize":
        if not manual_response:
            logger.error(
                "Finalize job received without manual_response",
                extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "validate_job", "status": "failed"},
            )
            return

        if "shorts_content" not in manual_response and "final_videos" not in manual_response:
            logger.error(
                "Finalize job received invalid manual_response",
                extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "validate_job", "status": "failed"},
            )
            return

        if not video_url:
            logger.error(
                "Finalize job received without video_url",
                extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "validate_job", "status": "failed"},
            )
            return

    # ==========================================
    # força o stage atual no settings global
    # ==========================================

    settings.pipeline_stage = pipeline_stage

    logger.info(
        f"Starting pipeline {job_id} ({pipeline_stage})",
        extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "pipeline", "status": "started"},
    )

    pipeline = Pipeline(
        video_url=video_url,
        job_id=job_id,
        manual_response=manual_response,
        clip_mode=job.get("clip_mode", "short_serie"),
        video_ratio=job.get("video_ratio", "portrait"),
        build_ia=bool(job.get("build_ia", False)),
    )

    storage = MinioStorage()
    api_client = ClipFlowApiClient()

    stage_status = "preparing" if pipeline_stage == "prepare" else "finalizing"
    _sync_clipflow_api(
        api_client,
        job_id=job_id,
        pipeline_stage=pipeline_stage,
        status=stage_status,
    )

    try:

        result = pipeline.run()

        # ==========================================
        # PREPARE
        # ==========================================

        if result["status"] == "awaiting_manual_llm":

            transcript_path = result.get("transcript_path")
            transcript_with_speakers_path = result.get("transcript_with_speakers_path")
            candidates_path = result.get("candidates_path")
            span_catalog_path = result.get("span_catalog_path")
            hook_candidates_path = result.get("hook_candidates_path")
            language_detection_path = result.get("language_detection_path")
            prompt_path = result.get("prompt_path")
            runtime_status_path = result.get("runtime_status_path")
            artifacts_manifest_path = result.get("artifacts_manifest_path")

            if _upload_if_exists(storage, transcript_path, f"jobs/{job_id}/transcript.json"):
                pipeline.artifacts.mark_remote(
                    "transcript",
                    pipeline_stage,
                    f"jobs/{job_id}/transcript.json",
                    transcript_path,
                )

            if _upload_if_exists(
                storage,
                transcript_with_speakers_path,
                f"jobs/{job_id}/transcript_with_speakers.json",
            ):
                pipeline.artifacts.mark_remote(
                    "transcript_with_speakers",
                    pipeline_stage,
                    f"jobs/{job_id}/transcript_with_speakers.json",
                    transcript_with_speakers_path,
                )

            if _upload_if_exists(storage, candidates_path, f"jobs/{job_id}/candidates.json"):
                pipeline.artifacts.mark_remote(
                    "candidates",
                    pipeline_stage,
                    f"jobs/{job_id}/candidates.json",
                    candidates_path,
                )

            if _upload_if_exists(storage, span_catalog_path, f"jobs/{job_id}/span_catalog.json"):
                pipeline.artifacts.mark_remote(
                    "span_catalog",
                    pipeline_stage,
                    f"jobs/{job_id}/span_catalog.json",
                    span_catalog_path,
                )

            if _upload_if_exists(storage, hook_candidates_path, f"jobs/{job_id}/hook_candidates.json"):
                pipeline.artifacts.mark_remote(
                    "hook_candidates",
                    pipeline_stage,
                    f"jobs/{job_id}/hook_candidates.json",
                    hook_candidates_path,
                )

            if _upload_if_exists(storage, language_detection_path, f"jobs/{job_id}/language_detection.json"):
                pipeline.artifacts.mark_remote(
                    "language_detection",
                    pipeline_stage,
                    f"jobs/{job_id}/language_detection.json",
                    language_detection_path,
                )

            if _upload_if_exists(storage, prompt_path, f"jobs/{job_id}/prompt.txt"):
                pipeline.artifacts.mark_remote(
                    "prompt",
                    pipeline_stage,
                    f"jobs/{job_id}/prompt.txt",
                    prompt_path,
                )

            speaker_turns_path = str(Path(pipeline.work_dir) / "speaker_turns.json")
            if _upload_if_exists(
                storage,
                speaker_turns_path,
                f"jobs/{job_id}/speaker_turns.json",
            ):
                pipeline.artifacts.mark_remote(
                    "speaker_turns",
                    pipeline_stage,
                    f"jobs/{job_id}/speaker_turns.json",
                    speaker_turns_path,
                )

            diarization_diagnostics_path = str(Path(pipeline.work_dir) / "diarization_diagnostics.json")
            if _upload_if_exists(
                storage,
                diarization_diagnostics_path,
                f"jobs/{job_id}/diarization_diagnostics.json",
            ):
                pipeline.artifacts.mark_remote(
                    "diarization_diagnostics",
                    pipeline_stage,
                    f"jobs/{job_id}/diarization_diagnostics.json",
                    diarization_diagnostics_path,
                )

            _upload_if_exists(
                storage,
                runtime_status_path,
                f"jobs/{job_id}/runtime_status.json",
            )
            if runtime_status_path:
                pipeline.artifacts.mark_remote(
                    "runtime_status",
                    pipeline_stage,
                    f"jobs/{job_id}/runtime_status.json",
                    runtime_status_path,
                )
            _upload_if_exists(
                storage,
                artifacts_manifest_path,
                f"jobs/{job_id}/artifacts_manifest.json",
            )
            if artifacts_manifest_path:
                pipeline.artifacts.mark_remote(
                    "artifacts_manifest",
                    pipeline_stage,
                    f"jobs/{job_id}/artifacts_manifest.json",
                    artifacts_manifest_path,
                )

            logger.info(
                f"{job_id} prepare stage uploaded to MinIO",
                extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "upload_artifacts", "status": "completed"},
            )
            _sync_clipflow_api(
                api_client,
                job_id=job_id,
                pipeline_stage="prepare",
                status="awaiting_manual_llm",
            )
            return

        # ==========================================
        # FINALIZE
        # ==========================================

        if result["status"] == "success":

            # salva resposta da IA
            if manual_response:
                ai_output_path = Path(f"/tmp/{job_id}_ai_output.json")

                with open(ai_output_path, "w", encoding="utf-8") as f:
                    json.dump(manual_response, f, ensure_ascii=False, indent=2)

                if ai_output_path.exists():
                    storage.upload(
                        str(ai_output_path),
                        f"jobs/{job_id}/ai_output.json",
                    )
                    pipeline.artifacts.mark_remote(
                        "ai_output",
                        pipeline_stage,
                        f"jobs/{job_id}/ai_output.json",
                        ai_output_path,
                    )

                try:
                    ai_output_path.unlink()
                except Exception:
                    pass

            # salva cortes
            cut_files = result.get("cut_files", [])
            final_clip_files = result.get("final_clip_files", [])
            final_reel_path = result.get("final_reel_path")
            subtitle_path = result.get("subtitle_path")
            qa_report_path = result.get("qa_report_path")
            render_plan_path = result.get("render_plan_path")
            delivery_package_path = result.get("delivery_package_path")
            publish_package_path = result.get("publish_package_path")

            for file_path in final_clip_files:
                path_obj = Path(file_path)

                if path_obj.exists():
                    storage.upload(
                        str(path_obj),
                        f"jobs/{job_id}/final_clips/{path_obj.name}",
                    )
                    pipeline.artifacts.mark_remote(
                        path_obj.stem,
                        pipeline_stage,
                        f"jobs/{job_id}/final_clips/{path_obj.name}",
                        path_obj,
                    )

            if _upload_if_exists(
                storage,
                final_reel_path,
                f"jobs/{job_id}/final_reel.mp4",
            ):
                pipeline.artifacts.mark_remote(
                    "final_reel",
                    pipeline_stage,
                    f"jobs/{job_id}/final_reel.mp4",
                    final_reel_path,
                )

            if _upload_if_exists(
                storage,
                subtitle_path,
                f"jobs/{job_id}/{Path(subtitle_path).name}" if subtitle_path else f"jobs/{job_id}/final_reel.ass",
            ):
                pipeline.artifacts.mark_remote(
                    "final_reel_subtitles",
                    pipeline_stage,
                    f"jobs/{job_id}/{Path(subtitle_path).name}" if subtitle_path else f"jobs/{job_id}/final_reel.ass",
                    subtitle_path,
                )

            if _upload_if_exists(
                storage,
                qa_report_path,
                f"jobs/{job_id}/qa_report.json",
            ):
                pipeline.artifacts.mark_remote(
                    "qa_report",
                    pipeline_stage,
                    f"jobs/{job_id}/qa_report.json",
                    qa_report_path,
                )

            if _upload_if_exists(
                storage,
                render_plan_path,
                f"jobs/{job_id}/render_plan.json",
            ):
                pipeline.artifacts.mark_remote(
                    "render_plan",
                    pipeline_stage,
                    f"jobs/{job_id}/render_plan.json",
                    render_plan_path,
                )

            if _upload_if_exists(
                storage,
                delivery_package_path,
                f"jobs/{job_id}/delivery_package.json",
            ):
                pipeline.artifacts.mark_remote(
                    "delivery_package",
                    pipeline_stage,
                    f"jobs/{job_id}/delivery_package.json",
                    delivery_package_path,
                )

            if _upload_if_exists(
                storage,
                publish_package_path,
                f"jobs/{job_id}/publish_package.json",
            ):
                pipeline.artifacts.mark_remote(
                    "publish_package",
                    pipeline_stage,
                    f"jobs/{job_id}/publish_package.json",
                    publish_package_path,
                )

            _upload_if_exists(
                storage,
                result.get("runtime_status_path"),
                f"jobs/{job_id}/runtime_status.json",
            )
            if result.get("runtime_status_path"):
                pipeline.artifacts.mark_remote(
                    "runtime_status",
                    pipeline_stage,
                    f"jobs/{job_id}/runtime_status.json",
                    result.get("runtime_status_path"),
                )
            _upload_if_exists(
                storage,
                result.get("artifacts_manifest_path"),
                f"jobs/{job_id}/artifacts_manifest.json",
            )
            if result.get("artifacts_manifest_path"):
                pipeline.artifacts.mark_remote(
                    "artifacts_manifest",
                    pipeline_stage,
                    f"jobs/{job_id}/artifacts_manifest.json",
                    result.get("artifacts_manifest_path"),
                )

            logger.info(
                f"{job_id} finalize stage uploaded to MinIO",
                extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "upload_artifacts", "status": "completed"},
            )
            _sync_clipflow_api(
                api_client,
                job_id=job_id,
                pipeline_stage="finalize",
                status="completed",
            )
            return

        # ==========================================
        # erro / retorno inesperado
        # ==========================================

        logger.error(
            f"Unexpected pipeline result: {result}",
            extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "pipeline", "status": "unexpected_result"},
        )

        _upload_if_exists(
            storage,
            result.get("runtime_status_path"),
            f"jobs/{job_id}/runtime_status.json",
        )
        _upload_if_exists(
            storage,
            result.get("artifacts_manifest_path"),
            f"jobs/{job_id}/artifacts_manifest.json",
        )
        _sync_clipflow_api(
            api_client,
            job_id=job_id,
            pipeline_stage=pipeline_stage,
            status="failed",
            error_message=result.get("error") or "Unexpected pipeline result",
        )

    except Exception as e:
        logger.exception(
            f"Pipeline failed for job {job_id}: {e}",
            extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "pipeline", "status": "failed"},
        )
        _sync_clipflow_api(
            api_client,
            job_id=job_id,
            pipeline_stage=pipeline_stage,
            status="failed",
            error_message=str(e),
        )


def main():
    if settings.worker_mode == "scheduler":
        run_private_scheduler()
        return

    redis_client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
    )

    logger.info(
        "VOXMIND WORKER READY — waiting for jobs",
        extra={"step": "worker_boot", "status": "ready"},
    )

    while True:

        _, payload = redis_client.brpop(settings.redis_queue_name)

        try:
            job = json.loads(payload)
        except Exception:
            logger.error(
                "Invalid job payload",
                extra={"step": "parse_job", "status": "failed"},
            )
            continue

        run_pipeline(job)


def run_private_scheduler():
    redis_client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
    )
    api_client = ClipFlowApiClient()
    worker_id = f"private-scheduler-{uuid.uuid4()}"

    logger.info(
        "VOXMIND PRIVATE SCHEDULER READY",
        extra={"step": "private_scheduler_boot", "status": "ready"},
    )

    while True:
        claimed = api_client.claim_due_private_scheduler_runs_safe(
            worker_id=worker_id,
            limit=3,
        ) or {"runs": []}

        for item in claimed.get("runs", []):
            payload = item.get("job_payload")
            if not payload:
                continue
            redis_client.lpush(settings.redis_queue_name, json.dumps(payload))
            logger.info(
                "Queued private scheduler job",
                extra={
                    "job_id": payload.get("job_id"),
                    "pipeline_stage": payload.get("pipeline_stage"),
                    "step": "private_scheduler_enqueue",
                    "status": "queued",
                },
            )

        time.sleep(settings.scheduler_poll_interval_sec)


if __name__ == "__main__":
    main()
