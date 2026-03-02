import os
import sys
import uuid
import json
import logging

from worker.app.pipeline.pipeline import Pipeline
from worker.app.settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():

    video_url = settings.video_url
    job_id = os.getenv("JOB_ID", str(uuid.uuid4()))
    pipeline_stage = settings.pipeline_stage

    manual_response_raw = os.getenv("MANUAL_RESPONSE")
    manual_response = None

    if manual_response_raw:
        try:
            manual_response = json.loads(manual_response_raw)
        except json.JSONDecodeError:
            logger.error("Invalid MANUAL_RESPONSE JSON")
            sys.exit(1)

    if not video_url:
        logger.error("VIDEO_URL not provided")
        sys.exit(1)

    logger.info(f"Starting job {job_id} - stage={pipeline_stage}")

    pipeline = Pipeline(
        video_url=video_url,
        job_id=job_id,
        manual_response=manual_response
    )

    try:
        result = pipeline.run()

        if result.get("status") == "error":
            logger.error(f"Pipeline error: {result.get('error')}")
            sys.exit(1)

        if result.get("status") == "awaiting_manual_llm":
            logger.info("Stage prepare completed. Awaiting manual LLM response.")
            sys.exit(0)

        if result.get("status") == "success":
            logger.info("Stage finalize completed successfully.")
            sys.exit(0)

        logger.warning("Pipeline returned unknown status.")
        sys.exit(1)

    except Exception as e:
        logger.exception(f"Job failed unexpectedly: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()