"""Publishing a payload onto the worker's queue.

A thin seam over one ``LPUSH``, extracted for two reasons.

**It has to be substitutable.** Admission's most important failure mode is "the row committed
but the message never arrived", and there is no way to test that without being able to make
the enqueue fail on demand. Inline ``redis.Redis(...).lpush(...)`` inside an endpoint cannot
be made to fail without breaking Redis for everything else in the process.

**Failure has to be typed.** The caller needs to distinguish "Redis is down" from "the payload
was rejected", because the recovery for the first is to retry later and for the second is to
never retry at all.

This deliberately does not import the worker's ``ReliableQueue``: the two services do not
share a package, and the producer side is a single LPUSH onto a list whose name both already
agree on. Claiming, leasing, retrying and dead-lettering stay entirely with the worker
(PR-RUNTIME-01), untouched.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import redis

from app.core.settings import settings

logger = logging.getLogger(__name__)


class EnqueueError(RuntimeError):
    """The payload did not reach the queue.

    ``retryable`` says whether trying again could plausibly succeed. A connection refused is
    worth retrying; a payload that cannot be serialised will fail identically forever.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        self.retryable = retryable
        super().__init__(message)


class WorkQueue(Protocol):
    def publish(self, payload: dict[str, Any]) -> str: ...


class RedisWorkQueue:
    """The real queue: one LPUSH onto the list the worker claims from."""

    def __init__(self, queue_name: str | None = None, client: redis.Redis | None = None) -> None:
        self.queue_name = queue_name or settings.voxmind_redis_queue
        self._client = client

    def publish(self, payload: dict[str, Any]) -> str:
        try:
            # sort_keys matches how the worker's ReliableQueue serialises a payload when it
            # re-queues one for a retry. The token IS the identity of an in-flight message in
            # the processing list, so the two producers agreeing on its shape keeps a
            # re-queued job byte-identical to the one that was first published.
            token = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise EnqueueError(f"payload is not serialisable: {exc}", retryable=False) from exc

        try:
            self._redis().lpush(self.queue_name, token)
        except redis.RedisError as exc:
            raise EnqueueError(f"redis unavailable: {type(exc).__name__}", retryable=True) from exc
        return token

    def _redis(self) -> redis.Redis:
        if self._client is not None:
            return self._client
        return redis.Redis(
            host=settings.voxmind_redis_host,
            port=settings.voxmind_redis_port,
            decode_responses=True,
        )
