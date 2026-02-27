import os
import logging
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

from app.job_creator import JobCreator
from app.job_watcher import JobWatcher
from app.telegram_sender import TelegramSender


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="VoxMind Control Plane",
    version="2.0.0"
)


class JobRequest(BaseModel):
    video_url: HttpUrl


@app.post("/v1/jobs")
def create_job(request: JobRequest):

    job_id = str(uuid4())

    logger.info(f"Creating job {job_id}")
    logger.info(f"Video URL: {request.video_url}")

    try:
        # 1️⃣ Criar Job no Kubernetes
        creator = JobCreator()
        job_name = creator.create(
            video_url=str(request.video_url),
            job_id=job_id
        )

        logger.info(f"Job {job_name} created")

        # 2️⃣ Aguardar conclusão
        watcher = JobWatcher()
        status = watcher.wait_for_completion(job_name)

        if status == "succeeded":
            logger.info(f"Job {job_name} succeeded")

            # 3️⃣ Enviar vídeos no Telegram
            sender = TelegramSender()
            sender.send_cuts(job_id)

            logger.info(f"Telegram delivery completed for {job_id}")

            return {
                "status": "completed",
                "job_id": job_id
            }

        if status == "failed":
            logger.error(f"Job {job_name} failed")
            raise HTTPException(status_code=500, detail="Job execution failed")

        if status == "timeout":
            logger.error(f"Job {job_name} timeout")
            raise HTTPException(status_code=504, detail="Job execution timeout")

        raise HTTPException(status_code=500, detail="Unknown job status")

    except Exception as e:
        logger.exception(f"Unexpected error while processing job {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")