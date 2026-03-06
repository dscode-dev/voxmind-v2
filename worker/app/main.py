import os
import sys
import uuid
import json
import logging

from pathlib import Path

from app.pipeline.pipeline import Pipeline
from app.settings import settings
from app.storage.minio_client import MinioStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():

    video_url = settings.video_url

    if not video_url or not video_url.strip():
        logger.error("VIDEO_URL not provided. Worker will not start.")
        sys.exit(1)

    job_id = os.getenv("JOB_ID", str(uuid.uuid4()))
    pipeline_stage = settings.pipeline_stage

    manual_response_raw = os.getenv("MANUAL_RESPONSE")
    manual_response = None

    if manual_response_raw:
        try:
            manual_response = json.loads(manual_response_raw)
        except json.JSONDecodeError:
            logger.exception("Invalid MANUAL_RESPONSE JSON")
            sys.exit(1)

    pipeline = Pipeline(
        video_url=video_url.strip(),
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

            logger.info("Stage prepare uploaded to MinIO.")
            sys.exit(0)

        if result["status"] == "success":

            storage.upload(result["cuts_path"], f"{job_id}/cuts.json")

            for file_path in result["cut_files"]:
                filename = Path(file_path).name
                storage.upload(file_path, f"{job_id}/cuts/{filename}")

            logger.info("Stage finalize uploaded to MinIO.")
            sys.exit(0)

        logger.error(f"Unexpected pipeline result: {result}")
        sys.exit(1)

    except Exception as e:
        logger.exception(f"Job failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()