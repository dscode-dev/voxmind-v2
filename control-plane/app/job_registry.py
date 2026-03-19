import json
from pathlib import Path
import redis

from .settings import settings

class JobRegistry:

    def __init__(self):
        self.file_path = Path("/tmp/voxmind_job_registry.json")
        self.redis = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )

        if not self.file_path.exists():
            self._write({})

    def register(self, job_id: str, video_url: str):
        self._write_redis(job_id, video_url)
        self._write_file(job_id, video_url)

    def get_video_url(self, job_id: str):
        value = self._read_redis(job_id)
        if value:
            return value

        data = self._read_file()
        return data.get(job_id)

    def _redis_key(self, job_id: str) -> str:
        return f"{settings.redis_job_registry_prefix}:{job_id}"

    def _read_redis(self, job_id: str) -> str | None:
        try:
            return self.redis.get(self._redis_key(job_id))
        except Exception:
            return None

    def _write_redis(self, job_id: str, video_url: str) -> None:
        try:
            self.redis.setex(
                self._redis_key(job_id),
                settings.job_registry_ttl_sec,
                video_url,
            )
        except Exception:
            pass

    def _read_file(self):
        with open(self.file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_file(self, job_id: str, video_url: str):
        data = self._read_file()
        data[job_id] = video_url
        self._write(data)

    def _write(self, data):
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
