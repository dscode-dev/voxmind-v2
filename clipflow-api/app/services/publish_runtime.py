"""The publication worker: claim a command, run it, settle it, acknowledge it.

**Why a process and not a thread in the API.** An upload is minutes of network I/O. Keeping it
inside the HTTP request made a large publication hostage to every proxy timeout between the
operator and the API — the P1 debt this PR exists to pay. Keeping it inside the API process at
all would still couple publishing restarts to API restarts and put long transfers next to
request handling. It is also not the GPU worker: publishing needs no GPU, and queueing behind
ffmpeg and ASR would make every publication wait on a render.

It runs from the API image because that is where the publishing code already lives; a third
copy of the codebase to run one loop would be worse than a second container from one image.

**The order of operations, and why.**

    claim the command        the queue decides who owns it
    load the attempt         the database decides what may happen
    execute                  the atomic DB claim decides who uploads
    persist the outcome      committed BEFORE the acknowledgement
    acknowledge              only if we still own the lease

The database is written before the queue is acknowledged, always. A crash in between means
the command is delivered again and finds a terminal attempt, which costs one no-op. The
reverse order would mean a crash loses the only record that an upload happened.
"""
from __future__ import annotations

import logging
import signal
import threading
import uuid
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db.session import SessionLocal
from app.models.enums import PublishAttemptStatus
from app.models.publish_attempt import PublishAttempt
from app.publishing.identity import PublisherHeartbeat, resolve_worker_id
from app.publishing.publish_queue import ClaimedCommand, PublishQueue
from app.services.publish_recovery_service import (
    AMBIGUOUS,
    COMPLETED,
    REQUEUE,
    RESUME,
    PublishRecoveryService,
)
from app.services.publishing_service import PublishingService

logger = logging.getLogger(__name__)

# Outcomes that mean "this command is finished" — the publication has an answer no further
# execution of this command could improve.
TERMINAL_ITEM_STATUSES = frozenset(
    {
        "published",
        "already_published",
        "unknown",
        "requires_manual_resolution",
        "canceled",
        "not_executable",
        # This worker was superseded while it was stalled. The publication has an owner and
        # an answer; this command has nothing left to contribute.
        "superseded",
    }
)


class PublisherRuntime:
    """One publication worker. One per process."""

    def __init__(
        self,
        *,
        worker_id: str | None = None,
        queue: PublishQueue | None = None,
        publishing: PublishingService | None = None,
        recovery: PublishRecoveryService | None = None,
        heartbeat: PublisherHeartbeat | None = None,
        session_factory=SessionLocal,
    ) -> None:
        self.worker_id = worker_id or resolve_worker_id()
        self.queue = queue or PublishQueue(worker_id=self.worker_id)
        self.publishing = publishing or PublishingService(queue=self.queue)
        self.recovery = recovery or PublishRecoveryService()
        self.heartbeat = heartbeat or PublisherHeartbeat(self.worker_id)
        self.session_factory = session_factory

        self._stopping = threading.Event()
        self._last_sweep = 0.0
        self.commands_handled = 0

    # -------------------------------------------------------------------- run

    def run(self) -> None:
        """Claim and execute until asked to stop."""
        self._install_signal_handlers()
        logger.info(
            "publisher_started",
            extra={
                "publisher_worker_id": self.worker_id,
                "queue": self.queue.queue,
                "publishing_enabled": settings.publishing_enabled,
            },
        )
        self.heartbeat.beat(state="idle")

        try:
            while not self._stopping.is_set():
                self.tick()
        finally:
            self.heartbeat.stop()
            logger.info(
                "publisher_stopped",
                extra={"publisher_worker_id": self.worker_id,
                       "commands_handled": self.commands_handled},
            )

    def tick(self) -> ClaimedCommand | None:
        """One pass: maintenance, then at most one command.

        Split out from ``run`` so tests drive it deterministically instead of racing a
        thread.
        """
        self.heartbeat.beat(state="idle")
        self._maybe_sweep()

        if not settings.publishing_enabled:
            # The kill switch stops claiming, not the process: the loop keeps running and
            # heartbeating so turning publishing back on needs no restart, and so an
            # operator can still see that a publisher is alive.
            self._sleep(settings.publish_claim_block_sec)
            return None

        command = self.queue.claim()
        if command is None:
            return None

        self.commands_handled += 1
        self._handle(command)
        return command

    # --------------------------------------------------------------- handling

    def _handle(self, command: ClaimedCommand) -> None:
        started = time.monotonic()
        db: Session = self.session_factory()
        renewer: _LeaseRenewer | None = None
        try:
            attempt = self._load(db, command)
            if attempt is None:
                # The command points at nothing. Not retryable and not a publication
                # problem: it is a malformed command, which is what the dead letter is for.
                self.queue.dead_letter(command, reason="unknown_publish_attempt")
                return

            self.heartbeat.beat(
                state="uploading",
                publish_attempt_id=str(attempt.id),
                pipeline_job_id=str(attempt.pipeline_job_id),
            )

            # Renewed from a thread because the upload is a long blocking call. Without it
            # the lease expires mid-upload and another worker recovers a command that is
            # still being executed.
            renewer = _LeaseRenewer(self.queue, command, self.heartbeat)
            renewer.start()

            outcome = self.publishing.execute_attempt(
                db, attempt=attempt, worker_id=self.worker_id
            )
            db.refresh(attempt)
            self._settle(command, attempt, outcome, duration_ms=_ms(started))

        except Exception:  # noqa: BLE001
            # Never swallowed silently: an unhandled error here would otherwise look exactly
            # like a queue that had nothing to do. The command is left in `processing` with
            # its lease, so it is recovered rather than lost.
            logger.exception(
                "publisher_command_crashed",
                extra={
                    "publisher_worker_id": self.worker_id,
                    "queue_message_id": command.message_id,
                    "publish_attempt_id": command.publish_attempt_id,
                },
            )
        finally:
            if renewer is not None:
                renewer.stop()
            db.close()
            self.heartbeat.beat(state="idle")

    def _settle(
        self,
        command: ClaimedCommand,
        attempt: PublishAttempt,
        outcome: Any,
        *,
        duration_ms: int,
    ) -> None:
        """Acknowledge, retry, or dead-letter — after the outcome is already committed."""
        status = outcome.status
        fields = {
            "publisher_worker_id": self.worker_id,
            "queue_message_id": command.message_id,
            "publish_attempt_id": str(attempt.id),
            "pipeline_job_id": str(attempt.pipeline_job_id),
            "publish_target_id": str(attempt.target_id),
            "attempt_no": attempt.attempt_no,
            "lease_age_ms": command.lease_age_ms,
            "duration_ms": duration_ms,
            "bytes_uploaded": attempt.bytes_uploaded,
            "status": status,
        }

        if status in TERMINAL_ITEM_STATUSES:
            # Includes UNKNOWN. An ambiguous publication is a finished COMMAND: it is
            # recorded in the database where an operator can act on it, and putting it back
            # on the queue would be the blind retry the whole design forbids.
            self.queue.acknowledge(command)
            logger.info("publish_command_done", extra=fields)
            return

        if status == "paused":
            # A switch is off. Come back later without spending an attempt.
            delay = self.queue.retry(command, delay_sec=self.queue.backoff_delay(1))
            logger.info("publish_command_paused", extra={**fields, "retry_in_sec": delay})
            return

        if status == "failed" and attempt.status == PublishAttemptStatus.FAILED_RETRYABLE:
            if attempt.attempt_no >= attempt.max_attempts:
                # The budget is spent. The attempt keeps its FAILED_RETRYABLE status so an
                # operator can still retry deliberately; the command stops cycling.
                self.queue.dead_letter(command, reason="attempts_exhausted")
                logger.warning("publish_command_exhausted", extra=fields)
                return
            delay = self.queue.retry(command, delay_sec=self._retry_delay(attempt, command))
            self._emit_retry(attempt, delay)
            logger.info("publish_command_retry", extra={**fields, "retry_in_sec": delay})
            return

        if status in ("failed", "blocked"):
            # A definitive refusal: bad metadata, revoked credentials, missing media. Not a
            # runtime failure, so it is acknowledged rather than dead-lettered — the reason
            # is on the attempt row.
            self.queue.acknowledge(command)
            logger.info("publish_command_refused", extra=fields)
            return

        if status == "in_progress":
            # Someone else holds the attempt. Our command is redundant.
            self.queue.acknowledge(command)
            logger.info("publish_command_superseded", extra=fields)
            return

        # An outcome the runtime does not recognise. Acknowledged rather than spun, and
        # logged loudly so the gap is visible.
        self.queue.acknowledge(command)
        logger.warning("publish_command_unhandled_outcome", extra=fields)

    def _retry_delay(self, attempt: PublishAttempt, command: ClaimedCommand) -> float:
        """Backoff, with quota treated as the different thing it is.

        A 503 clears in seconds. A daily quota does not, and retrying it every thirty
        seconds spends the rest of the day proving that — so it gets its own, much longer,
        floor.
        """
        if (attempt.error_code or "") in ("quotaExceeded", "userRateLimitExceeded"):
            return max(settings.publish_quota_backoff_sec, self.queue.backoff_delay(command.attempt))
        return self.queue.backoff_delay(command.attempt)

    def _emit_retry(self, attempt: PublishAttempt, delay: float | None) -> None:
        db = self.session_factory()
        try:
            from app.models.enums import PipelineEventType
            from app.services import event_bus

            event_bus.publish_event(
                db,
                service="publisher",
                event_type=PipelineEventType.WARNING,
                pipeline_job_id=attempt.pipeline_job_id,
                stage="publish.retry_scheduled",
                message=f"retry in {delay:.0f}s" if delay else "retry scheduled",
                payload={
                    "pipeline_job_id": str(attempt.pipeline_job_id),
                    "publish_attempt_id": str(attempt.id),
                    "publish_target_id": str(attempt.target_id),
                    "publisher_worker_id": self.worker_id,
                    "provider": "youtube",
                    "attempt_no": attempt.attempt_no,
                    "retry_in_sec": delay,
                    "error_code": attempt.error_code,
                },
                commit=True,
            )
        except Exception:  # noqa: BLE001
            logger.warning("publish_retry_event_failed",
                           extra={"publish_attempt_id": str(attempt.id)})
        finally:
            db.close()

    # ------------------------------------------------------------ maintenance

    def _maybe_sweep(self) -> None:
        if time.time() - self._last_sweep < settings.publish_sweep_interval_sec:
            return
        self._last_sweep = time.time()
        self.sweep()

    def sweep(self) -> dict[str, int]:
        """Two recoveries, run on a timer.

        Neither of them can start a publication that nobody asked for: both operate only on
        PublishAttempt rows, which exist only because an admin explicitly published.
        """
        result = {"enqueue_recovered": 0, "commands_reclaimed": 0, "attempts_recovered": 0}
        db = self.session_factory()
        try:
            result["enqueue_recovered"] = self.publishing.sweep_pending_enqueue(db)
            result.update(self._recover_stale(db))
        except Exception:  # noqa: BLE001
            logger.exception("publisher_sweep_failed",
                             extra={"publisher_worker_id": self.worker_id})
        finally:
            db.close()

        if any(result.values()):
            logger.info("publisher_sweep", extra={**result,
                                                  "publisher_worker_id": self.worker_id})
        return result

    def _recover_stale(self, db: Session) -> dict[str, int]:
        """Commands whose worker died, and the attempts they were executing.

        The queue side and the database side are handled separately on purpose. Reclaiming
        the command only makes it deliverable again; whether the upload may be repeated is
        decided by the recovery service from evidence on the row.
        """
        reclaimed = 0
        recovered = 0

        for token in self.queue.stale_commands():
            import json

            try:
                payload = json.loads(token)
            except (ValueError, TypeError):
                self.queue.reclaim(token)
                continue

            attempt = _attempt_by_id(db, payload.get("publish_attempt_id"))

            if attempt is not None and attempt.status == PublishAttemptStatus.IN_PROGRESS:
                decision = self.recovery.recover(db, attempt, worker_id=self.worker_id)
                recovered += 1
                if decision.action in (COMPLETED, AMBIGUOUS):
                    # Settled by recovery. The command has nothing left to do, so it is
                    # removed rather than made deliverable again.
                    self.queue.redis.lrem(self.queue.processing_key, 1, token)
                    continue
                if decision.action not in (REQUEUE, RESUME):
                    # Undetermined: leave it in processing for the next sweep to re-probe.
                    continue

            if self.queue.reclaim(token):
                reclaimed += 1

        return {"commands_reclaimed": reclaimed, "attempts_recovered": recovered}

    # --------------------------------------------------------------- shutdown

    def stop(self) -> None:
        self._stopping.set()

    def _install_signal_handlers(self) -> None:
        def _handle(signum, _frame):
            logger.info(
                "publisher_shutdown_signal",
                extra={"publisher_worker_id": self.worker_id, "signal": signum},
            )
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handle)
            except ValueError:
                # Not the main thread (a test, or an embedded runtime). The stop flag still
                # works; only the signal wiring is unavailable.
                pass

    def _sleep(self, seconds: float) -> None:
        self._stopping.wait(timeout=seconds)

    # ----------------------------------------------------------------- loading

    @staticmethod
    def _load(db: Session, command: ClaimedCommand) -> PublishAttempt | None:
        return _attempt_by_id(db, command.publish_attempt_id)


class _LeaseRenewer:
    """Keeps the queue lease alive while a long upload runs.

    Stops the moment ownership is lost. That is not merely an optimisation: losing the lease
    means another worker may have recovered this command, and the loudest possible signal is
    the right response.
    """

    def __init__(
        self,
        queue: PublishQueue,
        command: ClaimedCommand,
        heartbeat: PublisherHeartbeat,
    ) -> None:
        self.queue = queue
        self.command = command
        self.heartbeat = heartbeat
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost_ownership = False

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="publish-lease-renewer", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        interval = max(1, settings.publish_heartbeat_interval_sec)
        while not self._stop.wait(timeout=interval):
            if not self.queue.renew_lease(self.command):
                self.lost_ownership = True
                logger.warning(
                    "publish_lease_lost_during_upload",
                    extra={
                        "queue_message_id": self.command.message_id,
                        "publish_attempt_id": self.command.publish_attempt_id,
                    },
                )
                return
            self.heartbeat.beat(
                state="uploading",
                publish_attempt_id=self.command.publish_attempt_id,
            )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _attempt_by_id(db: Session, attempt_id: Any) -> PublishAttempt | None:
    """Look one up from the id a command carries.

    The command holds a string, because a queue payload is JSON. The column is a UUID, and
    some dialects bind it strictly, so the value is converted rather than passed through.
    """
    if not attempt_id:
        return None
    try:
        key = uuid.UUID(str(attempt_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return db.query(PublishAttempt).filter(PublishAttempt.id == key).first()


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def runtime_snapshot(queue: PublishQueue | None = None) -> dict[str, Any]:
    """What an operator needs to answer "is publishing actually running?"."""
    queue = queue or PublishQueue()
    try:
        depths = queue.depths()
    except Exception:  # noqa: BLE001
        depths = {"ready": -1, "processing": -1, "delayed": -1, "dead": -1}

    workers = PublisherHeartbeat.alive()
    return {
        "publishing_enabled": settings.publishing_enabled,
        "queue": queue.queue,
        **depths,
        # From heartbeats, never from configuration: "a publisher is deployed" and "a
        # publisher is running" are different claims.
        "workers": workers,
        "workers_alive": len(workers),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
