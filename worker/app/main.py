import json
import logging
import uuid
import redis

from pathlib import Path

from app.pipeline.pipeline import Pipeline
from app.settings import settings
from app.storage.minio_client import MinioStorage

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


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
            logger.error("Prepare job received without video_url")
            return

    if pipeline_stage == "finalize":
        if not manual_response:
            logger.error("Finalize job received without manual_response")
            return

        if "shorts_content" not in manual_response:
            logger.error("Finalize job received invalid manual_response")
            return

        if not video_url:
            logger.error("Finalize job received without video_url")
            return

    # ==========================================
    # força o stage atual no settings global
    # ==========================================

    settings.pipeline_stage = pipeline_stage

    logger.info(f"Starting pipeline {job_id} ({pipeline_stage})")

    pipeline = Pipeline(
        video_url=video_url,
        job_id=job_id,
        manual_response=manual_response,
    )

    storage = MinioStorage()

    try:

        result = pipeline.run()

        # ==========================================
        # PREPARE
        # ==========================================

        if result["status"] == "awaiting_manual_llm":

            transcript_path = result.get("transcript_path")
            candidates_path = result.get("candidates_path")
            prompt_path = result.get("prompt_path")

            if transcript_path and Path(transcript_path).exists():
                storage.upload(
                    str(transcript_path),
                    f"jobs/{job_id}/transcript.json",
                )

            if candidates_path and Path(candidates_path).exists():
                storage.upload(
                    str(candidates_path),
                    f"jobs/{job_id}/candidates.json",
                )

            if prompt_path and Path(prompt_path).exists():
                storage.upload(
                    str(prompt_path),
                    f"jobs/{job_id}/prompt.txt",
                )

            logger.info(f"{job_id} prepare stage uploaded to MinIO")
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

                try:
                    ai_output_path.unlink()
                except Exception:
                    pass

            # salva cortes
            cut_files = result.get("cut_files", [])

            for file_path in cut_files:
                path_obj = Path(file_path)

                if path_obj.exists():
                    storage.upload(
                        str(path_obj),
                        f"jobs/{job_id}/cuts/{path_obj.name}",
                    )

            logger.info(f"{job_id} finalize stage uploaded to MinIO")
            return

        # ==========================================
        # erro / retorno inesperado
        # ==========================================

        logger.error(f"Unexpected pipeline result: {result}")

    except Exception as e:
        logger.exception(f"Pipeline failed for job {job_id}: {e}")


def main():

    redis_client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
    )

    logger.info("VOXMIND WORKER READY — waiting for jobs")

    while True:

        _, payload = redis_client.brpop(settings.redis_queue_name)

        try:
            job = json.loads(payload)
        except Exception:
            logger.error("Invalid job payload")
            continue

        run_pipeline(job)


if __name__ == "__main__":
    main()