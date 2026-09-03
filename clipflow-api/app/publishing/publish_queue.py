"""A reliable Redis queue for publication commands.

**Why not the media queue.** ``voxmind_jobs`` carries render work: minutes of GPU time, a
retry that costs nothing but electricity, and no external side effect. A publication command
carries an upload that may already have happened. The two need different visibility timeouts,
different retry budgets, a different dead-letter meaning, and — critically — different rules
about when a redelivery may repeat the work. Sharing a list would mean one set of settings
serving two jobs that disagree about the most important one.

**Why not import the worker's ReliableQueue.** ``worker/`` and ``clipflow-api/`` are separate
Python packages in separate images with separate dependency sets; there is no shared library
to put a common primitive in, and creating one to save this file would mean a third package,
a third build, and a coupled release for two services that currently deploy independently.
So this reuses that module's *design* — the same key layout, the same claimed/acknowledged/
retryable/dead invariants, the same "payload stays in processing until explicitly settled" —
and deliberately does not reuse its code.

**What it adds: ownership tokens.** The media queue's ``acknowledge`` removes the payload
without checking who is asking. That is survivable there. Here it is not: a worker whose lease
expired mid-upload, whose command was recovered and re-executed by someone else, must not wake
up and acknowledge — or retry, or dead-letter — a command that now belongs to another process.
Every settle operation is a compare-and-set against the lease's owner token, in Lua so the
check and the write cannot be separated.

Keys::

    clipflow_publish_jobs             LIST   ready commands
    clipflow_publish_jobs:processing  LIST   claimed, one entry per in-flight command
    clipflow_publish_jobs:delayed     ZSET   retry backlog, score = when it becomes due
    clipflow_publish_jobs:dead        LIST   dead-letter
    clipflow_publish_jobs:lease:<h>   STRING owner token + metadata, TTL = visibility timeout

**Delivery is at-least-once.** A command can be delivered more than once — that is the price
of never losing one. What makes that safe is not this file: it is the attempt's atomic DB
claim and its idempotency key. This queue guarantees the command is not lost; the database
guarantees the upload is not repeated.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import redis

from app.core.settings import settings

logger = logging.getLogger(__name__)

COMMAND_VERSION = 1

# Settle only if this process still owns the lease. KEYS[1] is the lease, ARGV[1] the owner
# token. Lua because "read the owner, compare, then write" has to be one operation - the gap
# between a GET and an LREM is exactly where a recovered command gets acknowledged twice.
_SETTLE_IF_OWNER = """
local lease = redis.call('GET', KEYS[1])
if not lease then
  return 0
end
local ok, decoded = pcall(cjson.decode, lease)
if not ok or decoded['owner'] ~= ARGV[1] then
  return 0
end
redis.call('LREM', KEYS[2], 1, ARGV[2])
redis.call('DEL', KEYS[1])
return 1
"""

_RENEW_IF_OWNER = """
local lease = redis.call('GET', KEYS[1])
if not lease then
  return 0
end
local ok, decoded = pcall(cjson.decode, lease)
if not ok or decoded['owner'] ~= ARGV[1] then
  return 0
end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""


def command_payload(
    *,
    publish_attempt_id: str,
    pipeline_job_id: str,
    target_id: str,
    media_identity: str,
) -> dict[str, Any]:
    """The whole command. Deliberately four ids and a version.

    No token, no session URI, no title, no description, no media. The worker reloads the
    frozen snapshot from the attempt row, which is the only copy that is authoritative and
    the only one that cannot go stale between enqueue and execution.
    """
    return {
        "version": COMMAND_VERSION,
        "publish_attempt_id": str(publish_attempt_id),
        "pipeline_job_id": str(pipeline_job_id),
        "target_id": str(target_id),
        "media_identity": media_identity,
    }


@dataclass
class ClaimedCommand:
    """A command this process owns for the duration of its lease."""

    token: str
    payload: dict[str, Any]
    attempt: int
    owner: str
    lease_key: str
    claimed_at: float = field(default_factory=time.time)

    @property
    def publish_attempt_id(self) -> str | None:
        value = self.payload.get("publish_attempt_id")
        return str(value) if value else None

    @property
    def message_id(self) -> str:
        """A short stable id for logs. Not an identity - the token is."""
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:12]

    @property
    def lease_age_ms(self) -> int:
        return int((time.time() - self.claimed_at) * 1000)


class PublishQueue:
    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        queue_name: str | None = None,
        *,
        worker_id: str = "unknown",
        max_attempts: int | None = None,
        visibility_timeout_sec: int | None = None,
        backoff_base_sec: float | None = None,
        backoff_max_sec: float | None = None,
    ) -> None:
        self._redis = redis_client
        self.queue = queue_name or settings.publish_queue_name
        self.worker_id = worker_id
        self.max_attempts = max_attempts or settings.publish_max_attempts
        self.visibility_timeout_sec = (
            visibility_timeout_sec or settings.publish_visibility_timeout_sec
        )
        self.backoff_base_sec = backoff_base_sec or settings.publish_retry_backoff_base_sec
        self.backoff_max_sec = backoff_max_sec or settings.publish_retry_backoff_max_sec

    # ------------------------------------------------------------------- keys

    @property
    def pending_key(self) -> str:
        return self.queue

    @property
    def processing_key(self) -> str:
        return f"{self.queue}:processing"

    @property
    def delayed_key(self) -> str:
        return f"{self.queue}:delayed"

    @property
    def dead_key(self) -> str:
        return f"{self.queue}:dead"

    def lease_key_for(self, token: str) -> str:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
        return f"{self.queue}:lease:{digest}"

    # -------------------------------------------------------------- producing

    def enqueue(self, payload: dict[str, Any]) -> str:
        """Publish a command for immediate processing."""
        token = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.redis.lpush(self.pending_key, token)
        return token

    # --------------------------------------------------------------- claiming

    def promote_due_delayed(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        promoted = 0
        for token in self.redis.zrangebyscore(self.delayed_key, "-inf", now):
            token = _text(token)
            # ZREM returns 1 only for the client that actually removed it, so concurrent
            # publishers cannot both promote the same command.
            if self.redis.zrem(self.delayed_key, token):
                self.redis.lpush(self.pending_key, token)
                promoted += 1
        return promoted

    def claim(self, block_sec: int | None = None) -> ClaimedCommand | None:
        """Move one command from ready to processing and lease it.

        ``BLMOVE`` is atomic: the command is in exactly one list at every instant, so a
        process that dies immediately after claiming leaves the command in ``processing``
        where recovery finds it, never nowhere.
        """
        self.promote_due_delayed()

        timeout = settings.publish_claim_block_sec if block_sec is None else block_sec
        token = self.redis.blmove(
            self.pending_key, self.processing_key, timeout, "RIGHT", "LEFT"
        )
        if token is None:
            return None
        token = _text(token)

        try:
            payload = json.loads(token)
        except (ValueError, TypeError):
            # Unparseable: it can never execute, so it goes straight to the dead letter
            # rather than cycling through retries forever.
            self.redis.lrem(self.processing_key, 1, token)
            self.redis.lpush(
                self.dead_key,
                json.dumps(
                    {
                        "_raw_payload": token,
                        "_failure": {
                            "failed_at": _now_iso(),
                            "reason": "invalid_payload",
                            "worker_id": self.worker_id,
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            logger.error("publish_queue_invalid_payload", extra={"worker_id": self.worker_id})
            return None

        if not isinstance(payload, dict):
            self.redis.lrem(self.processing_key, 1, token)
            return None

        # Unique per claim, not per worker: the same worker claiming the same command again
        # after a recovery must not be able to settle the earlier execution's lease.
        owner = f"{self.worker_id}:{uuid.uuid4().hex}"
        lease_key = self.lease_key_for(token)
        self.redis.set(
            lease_key,
            json.dumps(
                {
                    "owner": owner,
                    "worker_id": self.worker_id,
                    "publish_attempt_id": payload.get("publish_attempt_id"),
                    "claimed_at": _now_iso(),
                },
                ensure_ascii=False,
            ),
            ex=self.visibility_timeout_sec,
        )

        return ClaimedCommand(
            token=token,
            payload=payload,
            attempt=_int(payload.get("attempt"), 1),
            owner=owner,
            lease_key=lease_key,
        )

    def renew_lease(self, command: ClaimedCommand) -> bool:
        """Extend the visibility timeout, if this process still owns it.

        Returns False when ownership is gone — which is the signal that another worker has
        recovered this command and that continuing to upload would be a duplicate.
        """
        result = self.redis.eval(
            _RENEW_IF_OWNER, 1, command.lease_key, command.owner,
            str(self.visibility_timeout_sec),
        )
        return bool(result)

    def owns(self, command: ClaimedCommand) -> bool:
        raw = self.redis.get(command.lease_key)
        if raw is None:
            return False
        try:
            return json.loads(_text(raw)).get("owner") == command.owner
        except (ValueError, TypeError):
            return False

    # -------------------------------------------------------------- settling

    def acknowledge(self, command: ClaimedCommand) -> bool:
        """Done. Drop the command and its lease — only if we still own it."""
        return self._settle(command)

    def retry(self, command: ClaimedCommand, *, delay_sec: float | None = None) -> float | None:
        """Schedule another execution later. Returns the delay, or None if ownership is gone.

        The delayed set is the whole point: an immediate re-push would spin a failing
        publication against a provider that just asked us to slow down.
        """
        delay = self.backoff_delay(command.attempt) if delay_sec is None else float(delay_sec)
        next_payload = dict(command.payload)
        next_payload["attempt"] = command.attempt + 1
        next_token = json.dumps(next_payload, ensure_ascii=False, sort_keys=True)

        # Scheduled BEFORE the settle, so a crash between the two leaves the command in
        # `processing` (recoverable) rather than nowhere. A duplicate is safe here; a loss
        # is not.
        self.redis.zadd(self.delayed_key, {next_token: time.time() + delay})
        if not self._settle(command):
            # We no longer own it: someone else recovered it, so remove the copy we just
            # scheduled rather than leaving two futures for one command.
            self.redis.zrem(self.delayed_key, next_token)
            return None
        return delay

    def dead_letter(self, command: ClaimedCommand, *, reason: str) -> bool:
        """A command the runtime could not process. Not a publication verdict.

        UNKNOWN publications never come here: they are recorded in the database, where an
        operator can act on them. The dead letter is for commands that are malformed,
        unroutable, or have exhausted their execution budget.
        """
        entry = dict(command.payload)
        entry["_failure"] = {
            "failed_at": _now_iso(),
            "reason": reason,
            "worker_id": self.worker_id,
            "attempt": command.attempt,
        }
        self.redis.lpush(self.dead_key, json.dumps(entry, ensure_ascii=False))
        return self._settle(command)

    def _settle(self, command: ClaimedCommand) -> bool:
        result = self.redis.eval(
            _SETTLE_IF_OWNER, 2, command.lease_key, self.processing_key,
            command.owner, command.token,
        )
        if not result:
            logger.warning(
                "publish_queue_settle_refused_not_owner",
                extra={
                    "worker_id": self.worker_id,
                    "queue_message_id": command.message_id,
                    "publish_attempt_id": command.publish_attempt_id,
                },
            )
        return bool(result)

    def backoff_delay(self, attempt: int) -> float:
        """Exponential, capped, with bounded jitter.

        Jitter matters more here than in the media queue: several publications of the same
        run fail together when a provider has a bad minute, and without it they would all
        come back at the same instant and fail together again.
        """
        base = min(self.backoff_base_sec * (2 ** max(0, attempt - 1)), self.backoff_max_sec)
        return float(base * random.uniform(0.8, 1.2))

    # --------------------------------------------------------------- recovery

    def stale_commands(self) -> list[str]:
        """Processing entries whose lease has expired: nobody is working on these."""
        stale: list[str] = []
        for token in self.redis.lrange(self.processing_key, 0, -1):
            token = _text(token)
            if not self.redis.exists(self.lease_key_for(token)):
                stale.append(token)
        return stale

    def reclaim(self, token: str) -> bool:
        """Move one stale command back to ready.

        Returns whether *this* caller won it: LREM returning 0 means another recovery got
        there first, and pushing anyway would duplicate the command.

        This says nothing about whether the upload may be repeated. That question belongs to
        the attempt row, and answering it from here — "the lease expired, so retry" — is the
        mistake that produces two videos.
        """
        if not self.redis.lrem(self.processing_key, 1, token):
            return False
        self.redis.lpush(self.pending_key, token)
        return True

    # ------------------------------------------------------------------ stats

    def depths(self) -> dict[str, int]:
        return {
            "ready": int(self.redis.llen(self.pending_key)),
            "processing": int(self.redis.llen(self.processing_key)),
            "delayed": int(self.redis.zcard(self.delayed_key)),
            "dead": int(self.redis.llen(self.dead_key)),
        }

    @property
    def redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.Redis(
                host=settings.voxmind_redis_host,
                port=settings.voxmind_redis_port,
                decode_responses=True,
            )
        return self._redis


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
