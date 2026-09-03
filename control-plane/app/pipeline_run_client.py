"""Creates the authoritative run for a Telegram-originated job.

The bot has no database. It mints a job id, registers the URL in Redis and pushes a payload —
so before PR-STATE-01 a Telegram job existed only as a Redis key and a MinIO prefix, with no
row anywhere and no lifecycle. That is also why ``PipelineJob`` cannot hang off ``ClipJob``:
for these jobs there is no ``ClipJob`` at all.

The bot therefore asks the API to create the run over the existing internal-token channel,
the same way it already resolves a source URL. The API remains the only writer.

Failing to create a run does **not** stop the job from being enqueued. Telegram is the
operator's control surface; degrading it into "no run, no clip" because the API blipped would
trade a missing timeline for a missing video. The absence is logged and the payload carries
``pipeline_job_id: null``, which the worker reports as a legacy payload.
"""
from __future__ import annotations

import logging

import httpx

from .settings import settings

logger = logging.getLogger(__name__)


class PipelineRunClient:
    def __init__(self) -> None:
        self.base_url = str(settings.clipflow_api_base_url or "").strip().rstrip("/")
        self.token = settings.clipflow_api_internal_token
        self.enabled = bool(self.base_url)

    def create_run(
        self,
        *,
        worker_job_id: str,
        source_url: str | None,
        pipeline_stage: str,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
        preset_id: str | None = None,
    ) -> str | None:
        """Return the new run's id, or None when it could not be created."""
        if not self.enabled:
            return None

        headers = {}
        if self.token:
            headers["X-Internal-Token"] = self.token

        try:
            response = httpx.post(
                f"{self.base_url}/internal/pipeline-runs",
                headers=headers,
                json={
                    "worker_job_id": worker_job_id,
                    "source_url": source_url,
                    "pipeline_stage": pipeline_stage,
                    "clip_mode": clip_mode,
                    "video_ratio": video_ratio,
                    "preset_id": preset_id,
                    "origin": "telegram",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.warning(
                "Could not create a pipeline run for job %s; enqueueing without one",
                worker_job_id,
                exc_info=True,
            )
            return None

        run_id = payload.get("pipeline_job_id")
        return str(run_id) if run_id else None
