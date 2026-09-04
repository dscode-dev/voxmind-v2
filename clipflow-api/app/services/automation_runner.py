"""The background loop that calls the scheduler on a timer.

Deliberately the thinnest part of this PR: a task that wakes up, opens a session, calls
``tick()``, closes the session, and sleeps. Every decision lives in the scheduler and every
rule in the services below it.

**Why in-process and not a separate container.** The scheduler is a few database queries on a
timer, and it already holds a PostgreSQL advisory lock per topic — which is what makes replicas
safe, not process isolation. A dedicated container would add an image, a deployment and a
failure mode to run a `sleep` loop that the API can run for free. If a separate process is ever
justified — a scheduler that must survive API restarts, or one with a very different resource
profile — this module is the only thing that would move.

**Why not APScheduler, Celery beat or a cron sidecar.** All three solve scheduling problems
this does not have: cron expressions, persistent job stores, distributed workers. What is
needed here is "call a function every N seconds and do not run it twice", and the not-twice
part is handled by a lock the library would not know about.

**Not a short sleep loop with drift.** The interval is per topic and persisted as
``next_due_at``, so a slow tick pushes nothing out of alignment: the loop polls on a fixed
cadence and the scheduler decides what is actually due. A run that takes ten minutes does not
delay the next one by ten minutes.
"""
from __future__ import annotations

import asyncio
import logging

from app.core.settings import settings
from app.db.session import SessionLocal
from app.publishing.identity import AutomationHeartbeat, resolve_runner_id
from app.services.automation_scheduler import AutomationScheduler

logger = logging.getLogger(__name__)


class AutomationRunner:
    """Owns the asyncio task. One per process."""

    def __init__(
        self,
        scheduler: AutomationScheduler | None = None,
        heartbeat: AutomationHeartbeat | None = None,
    ) -> None:
        self._scheduler = scheduler or AutomationScheduler()
        self.runner_id = resolve_runner_id()
        # PR-AUTONOMY-HARDEN-01: without this, `runner_enabled` was the only observable fact
        # about the loop, and it is a configuration flag - it says the process was told to
        # run one, not that one is running. A TTL key says the second thing.
        self._heartbeat = heartbeat or AutomationHeartbeat(self.runner_id)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the loop, unless one is already running.

        Guarded because FastAPI's lifespan can run more than once in a reloading dev server,
        and two loops in one process would double every tick.
        """
        if self.running:
            logger.warning("automation_runner_already_running")
            return
        self._stopping.clear()
        self._heartbeat.beat(state="starting")
        self._task = asyncio.create_task(self._loop(), name="automation-scheduler")
        logger.info(
            "automation_runner_started",
            extra={
                "poll_interval_sec": settings.automation_poll_interval_sec,
                "enabled": settings.autonomous_pipeline_enabled,
            },
        )

    async def stop(self, *, timeout_sec: float = 10.0) -> None:
        """Ask the loop to finish, and give an in-flight tick a bounded chance to end.

        Bounded on purpose: a tick blocked on a slow provider must not hold shutdown open
        indefinitely. After the timeout the task is cancelled — the services are transactional,
        so an interrupted run leaves committed work committed and the rest rolled back.
        """
        self._stopping.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout_sec)
        except asyncio.TimeoutError:
            logger.warning("automation_runner_stop_timeout", extra={"timeout_sec": timeout_sec})
            self._task.cancel()
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            # Dropped on a clean stop so the runner disappears at once; on an unclean one the
            # TTL does the same job a minute later, which is why the TTL is the mechanism.
            self._heartbeat.stop()
            logger.info("automation_runner_stopped")

    async def _loop(self) -> None:
        # A small delay before the first tick so a restart does not fire while the process is
        # still opening connections and running its bootstrap.
        await self._sleep(settings.automation_startup_delay_sec)

        while not self._stopping.is_set():
            await self._run_one_tick()
            # After the tick, not before: a heartbeat that only proved the loop was awake
            # would keep beating while every tick raised.
            self._heartbeat.beat(state="idle", last_tick_at=_now_iso())
            await self._sleep(settings.automation_poll_interval_sec)

    async def _run_one_tick(self) -> None:
        try:
            # The tick is synchronous SQLAlchemy, so it runs in a worker thread rather than
            # blocking the event loop and stalling every HTTP request in the process.
            report = await asyncio.to_thread(self._tick)
        except Exception:  # noqa: BLE001
            # Never `except: pass`. A bug here would otherwise stop automation silently and
            # look exactly like "there was nothing to do". It is logged with its traceback and
            # the loop continues, because one bad tick should not end the scheduler.
            logger.exception("automation_tick_failed")
            return

        if report.runs or report.pending_enqueue_recovered:
            logger.info("automation_tick", extra=_tick_fields(report))
        else:
            logger.debug("automation_tick_idle", extra=_tick_fields(report))

    def _tick(self):
        db = SessionLocal()
        try:
            return self._scheduler.tick(db)
        finally:
            db.close()

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately when shutdown starts."""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _tick_fields(report) -> dict:
    return {
        "tick_id": report.tick_id,
        "enabled": report.enabled,
        "topics_considered": report.topics_considered,
        "ran": len(report.runs),
        "skipped": len(report.skipped),
        "pending_enqueue_recovered": report.pending_enqueue_recovered,
        "duration_ms": report.duration_ms,
    }
