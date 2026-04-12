import json
import redis

from .settings import settings


class QueuePublisher:

    def __init__(self):
        self.redis = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True
        )

    def publish(
        self,
        *,
        video_url: str,
        job_id: str,
        pipeline_stage: str,
        manual_response: dict | None = None,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
        job_preset: str | None = None,
        build_ia: bool = False,
    ):

        payload = {
            "video_url": video_url,
            "job_id": job_id,
            "pipeline_stage": pipeline_stage,
            "manual_response": manual_response,
            "clip_mode": clip_mode,
            "video_ratio": video_ratio,
            "job_preset": job_preset,
            "build_ia": build_ia,
        }

        self.redis.lpush(
            settings.redis_queue_name,
            json.dumps(payload)
        )
