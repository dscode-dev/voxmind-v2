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
        manual_response: dict | None = None
    ):

        payload = {
            "video_url": video_url,
            "job_id": job_id,
            "pipeline_stage": pipeline_stage,
            "manual_response": manual_response,
        }

        self.redis.lpush(
            settings.redis_queue_name,
            json.dumps(payload)
        )