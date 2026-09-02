import json
import shutil
import time
import uuid
import redis

from pathlib import Path

from app.integrations.clipflow_api_client import ClipFlowApiClient
from app.observability import bind_context, configure_logging, get_logger, log_context
from app.pipeline.pipeline import Pipeline
from app.pipeline.presets import resolve_job_preset
from app.runtime.failures import classify, is_retryable
from app.runtime.heartbeat import WorkerHeartbeat
from app.runtime.identity import WORKER_ID
from app.runtime.reliable_queue import ClaimedJob, ReliableQueue
from app.settings import settings
from app.storage.minio_client import MinioStorage

configure_logging()
logger = get_logger(__name__)

# Every log record from this process is attributable to the worker that produced it.
bind_context(worker_id=WORKER_ID)


class PipelineExecutionError(RuntimeError):
    """Raised so the queue runner can distinguish success from failure.

    ``run_pipeline`` still performs all of its existing side effects (API status sync,
    Telegram error notification); this only propagates the outcome to the caller.
    """

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


def _cleanup_workdir(work_dir: Path | None, *, succeeded: bool, job_id: str | None) -> None:
    """Delete the per-job scratch directory.

    Artifacts that matter were already uploaded to MinIO; /work only holds intermediates
    (source video, cut files, prepared/transitioned renders). On failure the directory is
    preserved when KEEP_WORKDIR_ON_FAILURE is true so a run can be inspected.
    """
    if work_dir is None:
        return

    if not succeeded and settings.keep_workdir_on_failure:
        logger.info(
            "Preserving workdir for diagnosis",
            extra={"job_id": job_id, "step": "workdir_cleanup", "status": "skipped"},
        )
        return

    try:
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.info(
            "Workdir removed",
            extra={"job_id": job_id, "step": "workdir_cleanup", "status": "completed"},
        )
    except Exception:
        logger.warning(
            "Failed to remove workdir",
            extra={"job_id": job_id, "step": "workdir_cleanup", "status": "failed"},
        )


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


def run_pipeline(job: dict, queue: ReliableQueue | None = None):

    video_url = job.get("video_url")
    job_id = job.get("job_id") or str(uuid.uuid4())
    pipeline_stage = job.get("pipeline_stage", "prepare")
    manual_response = job.get("manual_response")
    source_storage_key = job.get("source_storage_key")
    preset = resolve_job_preset(
        job.get("job_preset") or job.get("preset_id"),
        job.get("clip_mode", "short_serie"),
        job.get("video_ratio", "portrait"),
    )

    # ==========================================
    # validações por estágio
    # ==========================================

    if pipeline_stage == "prepare":
        if not video_url and not source_storage_key:
            logger.error(
                "Prepare job received without video_url or source_storage_key",
                extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "validate_job", "status": "failed"},
            )
            raise ValueError("Prepare job received without video_url or source_storage_key")

    if pipeline_stage == "finalize":
        if not manual_response:
            logger.error(
                "Finalize job received without manual_response",
                extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "validate_job", "status": "failed"},
            )
            raise ValueError("Finalize job received without manual_response")

        if (
            not preset.is_raw_edit
            and "shorts_content" not in manual_response
            and "final_videos" not in manual_response
        ):
            logger.error(
                "Finalize job received invalid manual_response",
                extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "validate_job", "status": "failed"},
            )
            raise ValueError("Finalize job received invalid manual_response")

        if not video_url and not source_storage_key:
            logger.error(
                "Finalize job received without video_url or source_storage_key",
                extra={"job_id": job_id, "pipeline_stage": pipeline_stage, "step": "validate_job", "status": "failed"},
            )
            raise ValueError("Finalize job received without video_url or source_storage_key")

    # ==========================================
    # força o stage atual no settings global
    # ==========================================

    settings.pipeline_stage = pipeline_stage
    logger.info(
        f"Starting pipeline {job_id} ({pipeline_stage})",
        extra={
            "job_id": job_id,
            "pipeline_stage": pipeline_stage,
            "step": "pipeline",
            "status": "started",
            "preset_id": preset.preset_id,
            "clip_mode": preset.clip_mode,
            "video_ratio": preset.video_ratio,
        },
    )

    # Resolve AI mode: explicit per-job build_ia wins; otherwise fall back to the worker
    # default (AI_MODE, default "automatic"). Manual mode keeps the legacy Telegram flow.
    build_ia_raw = job.get("build_ia")
    if build_ia_raw is None:
        automatic = settings.ai_mode == "automatic"
    else:
        automatic = bool(build_ia_raw)

    pipeline = Pipeline(
        video_url=video_url,
        job_id=job_id,
        manual_response=manual_response,
        clip_mode=preset.clip_mode,
        video_ratio=preset.video_ratio,
        job_preset=preset.preset_id,
        build_ia=automatic,
        source_storage_key=source_storage_key,
        edit_brief=job.get("edit_brief"),
    )

    storage = MinioStorage()
    api_client = ClipFlowApiClient()

    succeeded = False
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
            succeeded = True

            # Automatic mode attached a follow-up finalize job. Prepare artifacts are now in
            # storage, so it is safe to enqueue finalize for the next worker pickup.
            auto_finalize_job = result.get("auto_finalize_job")
            if auto_finalize_job:
                try:
                    # Published through the reliable queue so the follow-up job carries the
                    # same attempt/max_attempts envelope as any other job.
                    if queue is not None:
                        queue.enqueue(auto_finalize_job)
                    else:
                        redis.Redis(
                            host=settings.redis_host,
                            port=settings.redis_port,
                            decode_responses=True,
                        ).lpush(settings.redis_queue_name, json.dumps(auto_finalize_job))
                    logger.info(
                        "Auto-enqueued finalize job",
                        extra={
                            "job_id": job_id,
                            "pipeline_stage": "finalize",
                            "step": "auto_finalize_enqueue",
                            "status": "queued",
                        },
                    )
                except Exception:
                    logger.exception(
                        "Failed to auto-enqueue finalize job",
                        extra={
                            "job_id": job_id,
                            "pipeline_stage": "finalize",
                            "step": "auto_finalize_enqueue",
                            "status": "failed",
                        },
                    )
            return

        # ==========================================
        # FINALIZE
        # ==========================================

        if result["status"] == "success":

            # salva resposta da IA
            #
            # Canonical key is ai_response.json — the name the automatic pipeline already
            # writes at prepare time. ai_output.json is still written as a legacy alias so
            # readers (and jobs) that predate this PR keep working.
            if manual_response:
                ai_output_path = Path(f"/tmp/{job_id}_ai_output.json")

                with open(ai_output_path, "w", encoding="utf-8") as f:
                    json.dump(manual_response, f, ensure_ascii=False, indent=2)

                if ai_output_path.exists():
                    for object_name, artifact_name in (
                        (f"jobs/{job_id}/ai_response.json", "ai_response"),
                        (f"jobs/{job_id}/ai_output.json", "ai_output"),
                    ):
                        storage.upload(str(ai_output_path), object_name)
                        pipeline.artifacts.mark_remote(
                            artifact_name,
                            pipeline_stage,
                            object_name,
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
            succeeded = True
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
        error_message = result.get("error") or "Unexpected pipeline result"
        _sync_clipflow_api(
            api_client,
            job_id=job_id,
            pipeline_stage=pipeline_stage,
            status="failed",
            error_message=error_message,
        )
        raise PipelineExecutionError(error_message)

    except PipelineExecutionError:
        raise

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
        # Propagate so the queue runner can retry or dead-letter this payload.
        raise PipelineExecutionError(str(e), cause=e) from e

    finally:
        _cleanup_workdir(
            getattr(pipeline, "work_dir", None),
            succeeded=succeeded,
            job_id=job_id,
        )


def process_claimed_job(
    job: ClaimedJob,
    queue: ReliableQueue,
    heartbeat: WorkerHeartbeat | None = None,
) -> str:
    """Run one claimed job and settle it on the queue.

    Returns the outcome: "acknowledged", "retried" or "dead_lettered".

    The job stays in the processing list for the whole of this function. It leaves only
    through acknowledge/retry/dead_letter, so a crash anywhere in here leaves the payload
    in-flight with an expiring lease, ready for recover_stale.
    """
    if heartbeat is not None:
        heartbeat.mark_busy(job)

    logger.info(
        "Job claimed",
        extra={
            "job_id": job.job_id,
            "pipeline_stage": job.payload.get("pipeline_stage"),
            "step": "queue_claim",
            "status": "claimed",
            "attempt": job.attempt,
        },
    )

    try:
        run_pipeline(job.payload, queue=queue)
    except Exception as exc:
        cause = getattr(exc, "cause", None) or exc
        retryable = is_retryable(cause)

        if retryable and not job.is_last_attempt:
            delay = queue.retry(job, cause)
            logger.warning(
                "Job failed; scheduled for retry",
                extra={
                    "job_id": job.job_id,
                    "step": "queue_retry",
                    "status": "retry_scheduled",
                    "attempt": job.attempt,
                    "retry_in_sec": delay,
                    "error_class": classify(cause),
                },
            )
            return "retried"

        reason = (
            "attempts_exhausted" if retryable else "non_retryable_failure"
        )
        queue.dead_letter(job, cause, reason=reason)
        logger.error(
            "Job moved to the dead-letter queue",
            extra={
                "job_id": job.job_id,
                "step": "queue_dead_letter",
                "status": "dead_lettered",
                "attempt": job.attempt,
                "reason": reason,
            },
        )
        return "dead_lettered"

    # ACK point: the pipeline finished, artifacts were uploaded and the API sync ran.
    queue.acknowledge(job)
    logger.info(
        "Job acknowledged",
        extra={
            "job_id": job.job_id,
            "step": "queue_ack",
            "status": "acknowledged",
            "attempt": job.attempt,
        },
    )
    return "acknowledged"


def main():
    if settings.worker_mode == "scheduler":
        run_private_scheduler()
        return

    redis_client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
    )

    queue = ReliableQueue(redis_client, worker_id=WORKER_ID)
    heartbeat = WorkerHeartbeat(redis_client, WORKER_ID, queue=queue)
    heartbeat.start()

    # A worker that died mid-job left its payload in the processing list. Sweep on boot so
    # those jobs are recovered rather than stranded.
    recovered = queue.recover_stale()
    if recovered["recovered"] or recovered["dead_lettered"]:
        logger.warning(
            "Recovered in-flight jobs left behind by a previous worker",
            extra={"step": "queue_recover", "status": "completed", **recovered},
        )

    logger.info(
        "VOXMIND WORKER READY — waiting for jobs",
        extra={"step": "worker_boot", "status": "ready", **queue.depths()},
    )

    try:
        while True:
            job = queue.claim()

            if job is None:
                # Idle tick: also the moment to reclaim anything whose lease expired.
                queue.recover_stale()
                continue

            with log_context(
                job_id=job.job_id,
                attempt=job.attempt,
                worker_id=WORKER_ID,
            ):
                try:
                    process_claimed_job(job, queue, heartbeat)
                finally:
                    if heartbeat is not None:
                        heartbeat.mark_idle()
    finally:
        heartbeat.stop()
        heartbeat.clear()


def run_private_scheduler():
    redis_client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
    )
    api_client = ClipFlowApiClient()
    worker_id = f"private-scheduler-{WORKER_ID}"
    queue = ReliableQueue(redis_client, worker_id=worker_id)

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
            queue.enqueue(payload)
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
