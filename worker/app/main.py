import os
import sys
import uuid
import logging

from worker.app.pipeline.pipeline import Pipeline
from worker.app.storage.minio_client import MinioStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    video_url = os.getenv("VIDEO_URL")
    job_id = os.getenv("JOB_ID", str(uuid.uuid4()))

    if not video_url:
        logger.error("VIDEO_URL not provided")
        sys.exit(1)

    pipeline = Pipeline(video_url=video_url, job_id=job_id)

    try:
        result = pipeline.run()

        storage = MinioStorage()

        # Upload transcript
        storage.upload_with_retry(
            result["transcript_path"],
            f"{job_id}/transcript.json"
        )

        # Upload cuts metadata
        storage.upload_with_retry(
            result["cuts_path"],
            f"{job_id}/cuts.json"
        )

        # Upload video cuts
        for file_path in result["cut_files"]:
            filename = file_path.split("/")[-1]
            storage.upload_with_retry(
                file_path,
                f"{job_id}/cuts/{filename}"
            )

        logger.info("Job completed successfully")

    except Exception as e:
        logger.exception(f"Job failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()