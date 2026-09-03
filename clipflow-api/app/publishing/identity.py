"""Publisher process identity and liveness.

**Why liveness is in this PR at all.** PR-SCHEDULER-01 shipped a background loop with no
observable heartbeat, and the result was that "the scheduler is running" could only be
inferred from the absence of evidence. A publication worker with the same gap would be worse:
a dead publisher looks exactly like an empty queue, and every manual publish would sit
accepted-and-never-executed with nothing to point at.

The heartbeat is a Redis key with a TTL rather than a database table. It answers one question
— *is a publisher alive right now* — which is a fact about this instant, not history worth
keeping. A TTL expiring is the whole liveness mechanism: nothing has to notice a death and
write it down.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

import redis

from app.core.settings import settings

logger = logging.getLogger(__name__)

WORKER_KEY_PREFIX = "clipflow_publish:workers"


def resolve_worker_id() -> str:
    """A stable id for the life of this process.

    Same resolution order as the media worker's, so operators reading two sets of logs are
    reading the same shape of name: an explicit override, else the container id, else random.
    """
    configured = str(os.environ.get("PUBLISHER_WORKER_ID") or "").strip()
    if configured:
        return configured
    hostname = (socket.gethostname() or "").strip()
    suffix = uuid.uuid4().hex[:8]
    return f"publisher-{hostname}-{suffix}" if hostname else f"publisher-{suffix}"


class PublisherHeartbeat:
    """Announces that this process is alive, and reads who else is."""

    def __init__(
        self,
        worker_id: str,
        redis_client: redis.Redis | None = None,
        *,
        ttl_sec: int | None = None,
    ) -> None:
        self.worker_id = worker_id
        self._redis = redis_client
        self.ttl_sec = ttl_sec or settings.publish_heartbeat_ttl_sec

    @property
    def key(self) -> str:
        return f"{WORKER_KEY_PREFIX}:{self.worker_id}"

    def beat(self, **fields: Any) -> None:
        """Refresh the key. Never raises: a publisher must not die because Redis blinked."""
        payload = {
            "worker_id": self.worker_id,
            "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        try:
            self.redis.set(self.key, json.dumps(payload, ensure_ascii=False), ex=self.ttl_sec)
        except redis.RedisError as exc:
            logger.warning(
                "publisher_heartbeat_failed",
                extra={"publisher_worker_id": self.worker_id,
                       "error_type": type(exc).__name__},
            )

    def stop(self) -> None:
        """Drop the key on a clean shutdown, so the worker disappears immediately.

        On an unclean one the TTL does the same job a minute later - which is why the TTL is
        the mechanism and this is only a courtesy.
        """
        try:
            self.redis.delete(self.key)
        except redis.RedisError:
            pass

    @classmethod
    def alive(cls, redis_client: redis.Redis | None = None) -> list[dict[str, Any]]:
        """Every publisher whose heartbeat has not expired.

        Read from Redis, never from configuration: "a publisher is configured" and "a
        publisher is running" are different claims, and reporting the first as the second is
        how a dead runtime stays invisible.
        """
        client = redis_client or _default_redis()
        workers: list[dict[str, Any]] = []
        try:
            for key in client.scan_iter(match=f"{WORKER_KEY_PREFIX}:*", count=100):
                raw = client.get(key)
                if raw is None:
                    continue
                try:
                    workers.append(json.loads(raw.decode() if isinstance(raw, bytes) else raw))
                except (ValueError, TypeError):
                    continue
        except redis.RedisError as exc:
            logger.warning("publisher_liveness_unavailable",
                           extra={"error_type": type(exc).__name__})
            return []
        return sorted(workers, key=lambda w: w.get("worker_id", ""))

    @property
    def redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = _default_redis()
        return self._redis


def _default_redis() -> redis.Redis:
    return redis.Redis(
        host=settings.voxmind_redis_host,
        port=settings.voxmind_redis_port,
        decode_responses=True,
    )
