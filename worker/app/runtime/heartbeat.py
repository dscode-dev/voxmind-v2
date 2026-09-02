"""Worker heartbeat.

Publishes ``clipflow:workers:{worker_id}`` with a TTL. A live worker refreshes the key;
a dead worker's key simply expires, so "alive" and "dead" are distinguishable without any
extra bookkeeping.

The same background thread renews the lease of the job currently being processed. That is
deliberate: the two facts ("this worker is alive" and "this worker still owns that job")
have exactly the same lifetime, so tying them to one thread means a hung or killed worker
releases its job automatically.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from app.observability import get_logger
from app.settings import settings

logger = get_logger(__name__)

WORKER_KEY_PREFIX = "clipflow:workers"

STATUS_IDLE = "idle"
STATUS_BUSY = "busy"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker_key(worker_id: str) -> str:
    return f"{WORKER_KEY_PREFIX}:{worker_id}"


class WorkerHeartbeat:
    def __init__(
        self,
        redis_client,
        worker_id: str,
        *,
        interval_sec: int | None = None,
        ttl_sec: int | None = None,
        queue=None,
    ) -> None:
        self.redis = redis_client
        self.worker_id = worker_id
        self.interval_sec = interval_sec or settings.worker_heartbeat_interval_sec
        self.ttl_sec = ttl_sec or settings.worker_heartbeat_ttl_sec
        self.queue = queue

        self.key = worker_key(worker_id)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._status = STATUS_IDLE
        self._job_id: str | None = None
        self._attempt: int | None = None
        self._started_at: str | None = None
        self._current_job = None

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.publish()
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"heartbeat-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_sec + 1)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval_sec):
            try:
                self.publish()
                self._renew_lease()
            except Exception:
                # A heartbeat failure must never take the worker down.
                logger.warning(
                    "Heartbeat publish failed",
                    extra={"step": "heartbeat", "status": "failed", "worker_id": self.worker_id},
                )

    def _renew_lease(self) -> None:
        with self._lock:
            job = self._current_job
        if job is not None and self.queue is not None:
            self.queue.renew_lease(job)

    # ------------------------------------------------------------------ state

    def mark_busy(self, job) -> None:
        with self._lock:
            self._status = STATUS_BUSY
            self._job_id = job.job_id
            self._attempt = job.attempt
            self._started_at = _utc_now_iso()
            self._current_job = job
        self.publish()

    def mark_idle(self) -> None:
        with self._lock:
            self._status = STATUS_IDLE
            self._job_id = None
            self._attempt = None
            self._started_at = None
            self._current_job = None
        self.publish()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "worker_id": self.worker_id,
                "status": self._status,
                "job_id": self._job_id,
                "attempt": self._attempt,
                "started_at": self._started_at,
                "last_seen_at": _utc_now_iso(),
            }

    # ------------------------------------------------------------- publishing

    def publish(self) -> dict[str, Any]:
        payload = self.snapshot()
        self.redis.set(
            self.key,
            json.dumps(payload, ensure_ascii=False),
            ex=self.ttl_sec,
        )
        return payload

    def clear(self) -> None:
        self.redis.delete(self.key)


def list_workers(redis_client) -> list[dict[str, Any]]:
    """Every worker whose heartbeat has not expired. Used by ops tooling and tests."""
    workers: list[dict[str, Any]] = []
    for key in redis_client.scan_iter(match=f"{WORKER_KEY_PREFIX}:*"):
        raw = redis_client.get(key)
        if not raw:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            workers.append(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return workers
