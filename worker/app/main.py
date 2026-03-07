import json
import logging
import uuid
import redis

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

    logger.info(f"Starting pipeline: {job_id} ({pipeline_stage})")

    pipeline = Pipeline(
        video_url=video_url,
        job_id=job_id,
        manual_response=manual_response
    )

    storage = MinioStorage()

    try:

        result = pipeline.run()

        if result["status"] == "awaiting_manual_llm":

            storage.upload(result["transcript_path"], f"{job_id}/transcript.json")
            storage.upload(result["candidates_path"], f"{job_id}/candidates.json")
            storage.upload(result["prompt_path"], f"{job_id}/prompt.txt")

            logger.info(f"{job_id} prepare stage uploaded to MinIO")

        elif result["status"] == "success":

            storage.upload(result["cuts_path"], f"{job_id}/cuts.json")

            for file_path in result["cut_files"]:
                filename = file_path.split("/")[-1]
                storage.upload(file_path, f"{job_id}/cuts/{filename}")

            logger.info(f"{job_id} finalize stage uploaded to MinIO")

        else:
            logger.error(f"Unexpected pipeline result: {result}")

    except Exception as e:
        logger.exception(f"Pipeline failed: {job_id} - {e}")


def main():

    redis_client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True
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