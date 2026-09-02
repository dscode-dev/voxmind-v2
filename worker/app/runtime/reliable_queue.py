"""Reliable Redis job queue.

Replaces the previous `BRPOP`, which deleted the payload before any work happened: a worker
that crashed mid-job destroyed the job with no trace.

Keys (derived from the base queue name, default ``voxmind_jobs``)::

    voxmind_jobs              LIST   pending work, producers LPUSH, workers claim from the tail
    voxmind_jobs:processing   LIST   in-flight payloads, one entry per claimed job
    voxmind_jobs:delayed      ZSET   retry backlog, score = unix timestamp when it becomes due
    voxmind_jobs:dead         LIST   dead-letter queue
    voxmind_jobs:lease:<tok>  STRING lease for one in-flight payload, TTL = visibility timeout

Invariants
----------
* **Claimed**: the payload has been atomically moved from ``pending`` to ``processing`` by
  ``BLMOVE``, and a lease key exists. It is in exactly one list at any instant.
* **Acknowledged**: removed from ``processing`` and its lease deleted. This happens only
  after the caller declares definitive success.
* **Retryable**: removed from ``processing``, attempt incremented, re-published to
  ``delayed`` with an exponential backoff score. Promoted back to ``pending`` when due.
* **Dead**: removed from ``processing`` and pushed to ``dead`` with failure metadata, when
  attempts are exhausted or the failure is classified non-retryable.
* **Crash recovery**: the lease expires because nothing renews it. ``recover_stale`` finds
  ``processing`` entries with no live lease and requeues (or dead-letters) them.

No job can disappear because a worker died after claiming it: the payload stays in
``processing`` until it is explicitly acknowledged, retried, or dead-lettered.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.observability import get_logger
from app.settings import settings

logger = get_logger(__name__)


DEFAULT_ATTEMPT = 1


@dataclass
class ClaimedJob:
    """A payload claimed from the queue.

    ``token`` is the exact string stored in the processing list. Every queue operation
    addresses the job by that string, so the payload is never mutated in place.
    """

    token: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    lease_key: str
    claimed_at: float = field(default_factory=time.time)

    @property
    def job_id(self) -> str | None:
        value = self.payload.get("job_id")
        return str(value) if value else None

    @property
    def is_last_attempt(self) -> bool:
        return self.attempt >= self.max_attempts


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReliableQueue:
    def __init__(
        self,
        redis_client,
        queue_name: str | None = None,
        *,
        worker_id: str = "unknown",
        max_attempts: int | None = None,
        visibility_timeout_sec: int | None = None,
        backoff_base_sec: int | None = None,
        backoff_max_sec: int | None = None,
    ) -> None:
        self.redis = redis_client
        self.queue = queue_name or settings.redis_queue_name
        self.worker_id = worker_id
        self.max_attempts = max_attempts or settings.worker_max_attempts
        self.visibility_timeout_sec = (
            visibility_timeout_sec or settings.worker_visibility_timeout_sec
        )
        self.backoff_base_sec = backoff_base_sec or settings.worker_retry_backoff_base_sec
        self.backoff_max_sec = backoff_max_sec or settings.worker_retry_backoff_max_sec

    # ------------------------------------------------------------------ keys

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

    # ------------------------------------------------------------- producing

    def enqueue(self, payload: dict[str, Any]) -> str:
        """Publish a job for immediate processing."""
        token = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.redis.lpush(self.pending_key, token)
        return token

    # -------------------------------------------------------------- claiming

    def promote_due_delayed(self, now: float | None = None) -> int:
        """Move retry-backlog entries that are due into the pending list."""
        now = time.time() if now is None else now
        due = self.redis.zrangebyscore(self.delayed_key, "-inf", now)
        promoted = 0
        for token in due:
            # ZREM returns 1 only for the client that actually removed it, which makes the
            # promotion safe when several workers poll concurrently.
            if self.redis.zrem(self.delayed_key, token):
                self.redis.lpush(self.pending_key, token)
                promoted += 1
        return promoted

    def claim(self, block_sec: int | None = None) -> ClaimedJob | None:
        """Atomically move one payload from pending to processing and lease it.

        Returns None when nothing became available within ``block_sec``.
        """
        self.promote_due_delayed()

        timeout = settings.worker_claim_block_sec if block_sec is None else block_sec
        token = self.redis.blmove(
            self.pending_key,
            self.processing_key,
            timeout,
            "RIGHT",
            "LEFT",
        )
        if token is None:
            return None
        if isinstance(token, bytes):
            token = token.decode("utf-8")

        try:
            payload = json.loads(token)
        except (ValueError, TypeError):
            # Not JSON: it can never be processed, so it goes straight to the DLQ instead of
            # cycling forever through the retry path.
            logger.error(
                "Discarding unparseable queue payload",
                extra={"step": "queue_claim", "status": "failed", "worker_id": self.worker_id},
            )
            self.redis.lrem(self.processing_key, 1, token)
            self.redis.lpush(
                self.dead_key,
                json.dumps(
                    {
                        "_raw_payload": token,
                        "_failure": {
                            "failed_at": _utc_now_iso(),
                            "error_type": "InvalidPayload",
                            "error_message": "payload is not valid JSON",
                            "worker_id": self.worker_id,
                            "attempt": DEFAULT_ATTEMPT,
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            return None

        if not isinstance(payload, dict):
            payload = {"_raw_payload": payload}

        attempt = _coerce_int(payload.get("attempt"), DEFAULT_ATTEMPT)
        max_attempts = _coerce_int(payload.get("max_attempts"), self.max_attempts)

        lease_key = self.lease_key_for(token)
        self.redis.set(
            lease_key,
            json.dumps(
                {
                    "worker_id": self.worker_id,
                    "job_id": payload.get("job_id"),
                    "attempt": attempt,
                    "claimed_at": _utc_now_iso(),
                },
                ensure_ascii=False,
            ),
            ex=self.visibility_timeout_sec,
        )

        return ClaimedJob(
            token=token,
            payload=payload,
            attempt=attempt,
            max_attempts=max_attempts,
            lease_key=lease_key,
        )

    def renew_lease(self, job: ClaimedJob) -> bool:
        """Extend the visibility timeout. Called periodically while the job runs."""
        return bool(self.redis.expire(job.lease_key, self.visibility_timeout_sec))

    # ------------------------------------------------------------ completing

    def acknowledge(self, job: ClaimedJob) -> None:
        """Definitive success: drop the payload and its lease."""
        self.redis.lrem(self.processing_key, 1, job.token)
        self.redis.delete(job.lease_key)

    def retry(self, job: ClaimedJob, error: BaseException | None = None) -> float:
        """Requeue with the attempt counter incremented. Returns the backoff delay."""
        self.redis.lrem(self.processing_key, 1, job.token)
        self.redis.delete(job.lease_key)

        next_payload = dict(job.payload)
        next_payload["attempt"] = job.attempt + 1
        next_payload["max_attempts"] = job.max_attempts

        delay = self.backoff_delay(job.attempt)
        self.redis.zadd(
            self.delayed_key,
            {json.dumps(next_payload, ensure_ascii=False, sort_keys=True): time.time() + delay},
        )
        return delay

    def dead_letter(
        self,
        job: ClaimedJob,
        error: BaseException | None = None,
        reason: str | None = None,
    ) -> None:
        """Remove from processing and record in the DLQ with failure metadata."""
        self.redis.lrem(self.processing_key, 1, job.token)
        self.redis.delete(job.lease_key)

        entry = dict(job.payload)
        entry["_failure"] = {
            "failed_at": _utc_now_iso(),
            "error_type": type(error).__name__ if error is not None else "Unknown",
            "error_message": _truncate(str(error) if error is not None else (reason or "")),
            "reason": reason,
            "worker_id": self.worker_id,
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
        }
        self.redis.lpush(self.dead_key, json.dumps(entry, ensure_ascii=False))

    def backoff_delay(self, attempt: int) -> float:
        return float(min(self.backoff_base_sec * (2 ** max(0, attempt - 1)), self.backoff_max_sec))

    # -------------------------------------------------------------- recovery

    def recover_stale(self) -> dict[str, int]:
        """Requeue in-flight payloads whose lease expired (the worker died).

        A live worker renews its lease from the heartbeat thread, so a missing lease means
        nobody is working on that payload any more.
        """
        entries = self.redis.lrange(self.processing_key, 0, -1)
        recovered = 0
        dead = 0

        for token in entries:
            if isinstance(token, bytes):
                token = token.decode("utf-8")

            lease_key = self.lease_key_for(token)
            if self.redis.exists(lease_key):
                continue

            # Claim the recovery: only the client that actually removes the entry acts on it.
            if not self.redis.lrem(self.processing_key, 1, token):
                continue

            try:
                payload = json.loads(token)
                if not isinstance(payload, dict):
                    payload = {"_raw_payload": payload}
            except (ValueError, TypeError):
                payload = {"_raw_payload": token}

            attempt = _coerce_int(payload.get("attempt"), DEFAULT_ATTEMPT)
            max_attempts = _coerce_int(payload.get("max_attempts"), self.max_attempts)

            stale_job = ClaimedJob(
                token=token,
                payload=payload,
                attempt=attempt,
                max_attempts=max_attempts,
                lease_key=lease_key,
            )

            if attempt >= max_attempts:
                self.dead_letter(stale_job, reason="lease_expired_attempts_exhausted")
                dead += 1
                logger.error(
                    "Stale job exhausted its attempts; moved to the dead-letter queue",
                    extra={
                        "job_id": payload.get("job_id"),
                        "step": "queue_recover",
                        "status": "dead_lettered",
                        "attempt": attempt,
                        "worker_id": self.worker_id,
                    },
                )
                continue

            next_payload = dict(payload)
            next_payload["attempt"] = attempt + 1
            next_payload["max_attempts"] = max_attempts
            self.redis.lpush(
                self.pending_key,
                json.dumps(next_payload, ensure_ascii=False, sort_keys=True),
            )
            recovered += 1
            logger.warning(
                "Recovered a stale in-flight job whose lease expired",
                extra={
                    "job_id": payload.get("job_id"),
                    "step": "queue_recover",
                    "status": "requeued",
                    "attempt": attempt + 1,
                    "worker_id": self.worker_id,
                },
            )

        return {"recovered": recovered, "dead_lettered": dead}

    # ----------------------------------------------------------------- stats

    def depths(self) -> dict[str, int]:
        return {
            "pending": int(self.redis.llen(self.pending_key) or 0),
            "processing": int(self.redis.llen(self.processing_key) or 0),
            "delayed": int(self.redis.zcard(self.delayed_key) or 0),
            "dead": int(self.redis.llen(self.dead_key) or 0),
        }


def _coerce_int(value: Any, default: int) -> int:
    """Backward compatibility: payloads written before this PR carry no attempt fields."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _truncate(text: str, limit: int | None = None) -> str:
    limit = limit or settings.subprocess_stderr_capture_chars
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated, {len(text) - limit} more chars]"
