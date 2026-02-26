from __future__ import annotations

import logging
from redis import Redis

from .base import Cache

log = logging.getLogger(__name__)


class RedisCache(Cache):
    def __init__(self, redis_url: str):
        self._client = Redis.from_url(redis_url, decode_responses=True)

    def get(self, key: str) -> str | None:
        try:
            return self._client.get(key)
        except Exception:
            log.exception("cache.get_failed")
            return None

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        try:
            self._client.setex(key, ttl_seconds, value)
        except Exception:
            log.exception("cache.set_failed")
