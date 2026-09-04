"""Operational conditions that need someone's attention.

**A read model, not an incident system.** Every signal is derived from current state each time
it is asked for. Nothing is materialised, nothing needs acknowledging, and nothing has to be
resolved by pressing a button: when the condition stops holding, the signal stops being
active. A stored alert that outlives its cause is a thing operators learn to ignore, and an
ignored alert is worse than none.

**Detection, not delivery.** This decides *that* something is wrong. Email, Slack, paging and
the rest are transports, and none of them are here — a transport can be added later against a
stable set of codes, which is the useful order to build them in.

**Separate from process health.** ``/health`` and ``/ready`` answer "is this container
working". These answer "is the product working". A YouTube token expiring must not make
Docker restart the API, so the two never share an endpoint or a status.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.automation_state import AutomationState
from app.models.enums import PublishAttemptStatus, PublishTargetConnectionStatus
from app.models.publish_attempt import PublishAttempt
from app.models.publish_target import PublishTarget
from app.publishing.identity import AutomationHeartbeat, PublisherHeartbeat
from app.publishing.publish_queue import PublishQueue

logger = logging.getLogger(__name__)

# Severities, defined once. Spreading these strings through the codebase is how "high" and
# "HIGH" and "warning" end up meaning the same thing in three places.
CRITICAL = "critical"
HIGH = "high"
MEDIUM = "medium"

SEVERITY_ORDER = {MEDIUM: 1, HIGH: 2, CRITICAL: 3}

# The signal registry. Code -> severity, in one table so a reader can see the whole set and
# its relative weight without opening five files.
PUBLISHER_DOWN = "publisher_down"
UNRESOLVED_PUBLICATIONS = "unresolved_publications"
PUBLISH_DEAD_LETTERS = "publish_dead_letters"
STALLED_PUBLISH_QUEUE = "stalled_publish_queue"
AUTOMATION_RUNNER_STALE = "automation_runner_stale"
REPEATED_AUTOMATION_FAILURE = "repeated_automation_failure"
TARGET_RECONNECT_REQUIRED = "target_reconnect_required"

SEVERITIES: dict[str, str] = {
    # Work is waiting and nothing will ever pick it up.
    PUBLISHER_DOWN: CRITICAL,
    # A video may exist that this system cannot account for. Only a human closes it.
    UNRESOLVED_PUBLICATIONS: HIGH,
    PUBLISH_DEAD_LETTERS: HIGH,
    STALLED_PUBLISH_QUEUE: HIGH,
    # The autonomous loop is not running; production has silently stopped.
    AUTOMATION_RUNNER_STALE: HIGH,
    TARGET_RECONNECT_REQUIRED: HIGH,
    # Failing repeatedly is worth knowing about, but the system is retrying correctly.
    REPEATED_AUTOMATION_FAILURE: MEDIUM,
}

HEALTHY = "healthy"
DEGRADED = "degraded"
CRITICAL_STATUS = "critical"


@dataclass
class Signal:
    code: str
    severity: str
    active: bool
    message: str
    observed_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "active": self.active,
            "message": self.message,
            "observed_at": self.observed_at,
            "metadata": self.metadata,
        }


class OperationsService:
    """Evaluates every operational signal against current state."""

    def __init__(
        self,
        queue: PublishQueue | None = None,
        *,
        publisher_reader=None,
        runner_reader=None,
        clock=None,
    ) -> None:
        self.queue = queue or PublishQueue()
        self._publisher_reader = publisher_reader or (
            lambda: PublisherHeartbeat.alive(self.queue.redis)
        )
        self._runner_reader = runner_reader or (
            lambda: AutomationHeartbeat.alive(self.queue.redis)
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------- health

    def health(self, db: Session) -> dict[str, Any]:
        now = self._clock()
        depths = self._depths()
        publishers = self._read(self._publisher_reader)
        runners = self._read(self._runner_reader)

        signals = [
            self._publisher_down(depths, publishers, now),
            self._unresolved(db, now),
            self._dead_letters(depths, now),
            self._stalled_queue(db, depths, publishers, now),
            self._runner_stale(runners, now),
            self._repeated_failures(db, now),
            self._reconnect_required(db, now),
        ]
        active = [signal for signal in signals if signal.active]

        return {
            "status": self._status(active),
            "checked_at": now.isoformat(),
            "publishing_enabled": settings.publishing_enabled,
            "autopublish_enabled": settings.autopublish_enabled,
            "publisher_workers_alive": len(publishers),
            "automation_runners_alive": len(runners),
            "queue": depths,
            "signals": [signal.as_dict() for signal in signals],
            "active_signals": [signal.code for signal in active],
        }

    @staticmethod
    def _status(active: list[Signal]) -> str:
        """Derived from the active signals, never from an HTTP status code.

        The API can be perfectly healthy while the product is not: this says which.
        """
        if not active:
            return HEALTHY
        worst = max(SEVERITY_ORDER.get(signal.severity, 0) for signal in active)
        return CRITICAL_STATUS if worst >= SEVERITY_ORDER[CRITICAL] else DEGRADED

    # ------------------------------------------------------------------ signals

    def _publisher_down(
        self, depths: dict[str, int], publishers: list[dict], now: datetime
    ) -> Signal:
        """Work is waiting and no publisher exists to do it.

        Not raised when publishing is switched off: an operator who disabled it does not need
        telling that nothing is publishing. Nor when the queue is empty - no publisher and no
        work is a quiet system, not a broken one.
        """
        waiting = depths["ready"] + depths["processing"] + depths["delayed"]
        active = bool(
            settings.publishing_enabled and waiting > 0 and not publishers
        )
        return Signal(
            code=PUBLISHER_DOWN,
            severity=SEVERITIES[PUBLISHER_DOWN],
            active=active,
            message=(
                f"{waiting} publication command(s) waiting and no publisher is alive"
                if active else "a publisher is alive, or nothing is waiting"
            ),
            observed_at=now.isoformat(),
            metadata={"waiting": waiting, "workers_alive": len(publishers)},
        )

    def _unresolved(self, db: Session, now: datetime) -> Signal:
        """Publications whose outcome nobody can account for."""
        rows = (
            db.query(PublishAttempt.id, PublishAttempt.created_at)
            .filter(
                PublishAttempt.status.in_(
                    [PublishAttemptStatus.UNKNOWN,
                     PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION]
                )
            )
            .all()
        )
        oldest_age = 0
        if rows:
            oldest = min(_as_utc(created) for _, created in rows if created)
            oldest_age = int((now - oldest).total_seconds())

        return Signal(
            code=UNRESOLVED_PUBLICATIONS,
            severity=SEVERITIES[UNRESOLVED_PUBLICATIONS],
            active=bool(rows),
            message=(
                f"{len(rows)} publication(s) need a human decision"
                if rows else "no unresolved publications"
            ),
            observed_at=now.isoformat(),
            # Counts and ages only. Never a session URI, which is a bearer credential that
            # merely looks like a URL.
            metadata={"count": len(rows), "oldest_age_sec": oldest_age},
        )

    def _dead_letters(self, depths: dict[str, int], now: datetime) -> Signal:
        threshold = max(1, settings.operations_dead_letter_threshold)
        dead = depths["dead"]
        return Signal(
            code=PUBLISH_DEAD_LETTERS,
            severity=SEVERITIES[PUBLISH_DEAD_LETTERS],
            active=dead >= threshold,
            message=(
                f"{dead} publication command(s) in the dead letter"
                if dead >= threshold else "dead letter within threshold"
            ),
            observed_at=now.isoformat(),
            metadata={"count": dead, "threshold": threshold},
        )

    def _stalled_queue(
        self,
        db: Session,
        depths: dict[str, int],
        publishers: list[dict],
        now: datetime,
    ) -> Signal:
        """Publishers are alive, work is waiting, and nothing has finished for a while.

        Deliberately not "the queue is deep": a healthy system draining a backlog looks
        exactly like that. The evidence for a stall is that commands are ready, a worker
        exists to claim them, and yet no publication has reached a terminal state within the
        window. That is a claim this data can actually support.
        """
        window = max(60, settings.operations_stall_window_sec)
        since = now - timedelta(seconds=window)

        waiting = depths["ready"] + depths["processing"]
        if not (waiting > 0 and publishers):
            return Signal(
                code=STALLED_PUBLISH_QUEUE,
                severity=SEVERITIES[STALLED_PUBLISH_QUEUE],
                active=False,
                message="no work waiting, or no publisher to judge",
                observed_at=now.isoformat(),
                metadata={"waiting": waiting, "workers_alive": len(publishers)},
            )

        recent = (
            db.query(func.count(PublishAttempt.id))
            .filter(
                PublishAttempt.finished_at.isnot(None),
                PublishAttempt.finished_at >= since.replace(tzinfo=None),
            )
            .scalar()
            or 0
        )

        # A queue that only just received work has not stalled, however little has settled -
        # nothing has had time to. The evidence for a stall is a publication that has been
        # waiting longer than the window while a publisher was there to take it.
        aging = (
            db.query(func.count(PublishAttempt.id))
            .filter(
                PublishAttempt.status.in_(
                    [PublishAttemptStatus.PENDING, PublishAttemptStatus.IN_PROGRESS]
                ),
                PublishAttempt.created_at <= since.replace(tzinfo=None),
            )
            .scalar()
            or 0
        )

        active = recent == 0 and aging > 0
        return Signal(
            code=STALLED_PUBLISH_QUEUE,
            severity=SEVERITIES[STALLED_PUBLISH_QUEUE],
            active=active,
            message=(
                f"{aging} publication(s) waiting over {window}s with a live publisher and "
                "none settling"
                if active else "publications are settling, or none has waited long enough"
            ),
            observed_at=now.isoformat(),
            metadata={"waiting": waiting, "settled_in_window": int(recent),
                      "waiting_over_window": int(aging), "window_sec": window},
        )

    def _runner_stale(self, runners: list[dict], now: datetime) -> Signal:
        """The autonomous loop is configured but nothing is ticking.

        The gap PR-SCHEDULER-01 left: ``runner_enabled`` says the process was told to run a
        loop, which is not the same as one running. Only a heartbeat can say the second thing,
        and its absence is what this reports.
        """
        expected = settings.automation_runner_enabled and settings.autonomous_pipeline_enabled
        active = bool(expected and not runners)
        return Signal(
            code=AUTOMATION_RUNNER_STALE,
            severity=SEVERITIES[AUTOMATION_RUNNER_STALE],
            active=active,
            message=(
                "automation is enabled but no runner has reported a tick"
                if active else "automation liveness as configured"
            ),
            observed_at=now.isoformat(),
            metadata={
                "configured": settings.automation_runner_enabled,
                "automation_enabled": settings.autonomous_pipeline_enabled,
                "runners_alive": len(runners),
                "last_tick_at": _latest_tick(runners),
            },
        )

    def _repeated_failures(self, db: Session, now: datetime) -> Signal:
        """Topics whose automation has failed repeatedly.

        Read from state the scheduler already keeps. Not raised on a single failure - a
        provider having a bad minute is normal, and alerting on it teaches people to ignore
        the alert.
        """
        threshold = max(2, settings.operations_failure_threshold)
        rows = (
            db.query(AutomationState.topic_id, AutomationState.consecutive_failures)
            .filter(AutomationState.consecutive_failures >= threshold)
            .all()
        )
        return Signal(
            code=REPEATED_AUTOMATION_FAILURE,
            severity=SEVERITIES[REPEATED_AUTOMATION_FAILURE],
            active=bool(rows),
            message=(
                f"{len(rows)} topic(s) failing repeatedly"
                if rows else "no topic is failing repeatedly"
            ),
            observed_at=now.isoformat(),
            metadata={
                "topics": len(rows),
                "threshold": threshold,
                "worst": max((int(f or 0) for _, f in rows), default=0),
            },
        )

    def _reconnect_required(self, db: Session, now: datetime) -> Signal:
        """A channel an operator still wants to publish to has lost its credential."""
        rows = (
            db.query(PublishTarget.id, PublishTarget.name, PublishTarget.last_error_code)
            .filter(
                PublishTarget.is_active.is_(True),
                PublishTarget.connection_status
                == PublishTargetConnectionStatus.RECONNECT_REQUIRED,
            )
            .all()
        )
        return Signal(
            code=TARGET_RECONNECT_REQUIRED,
            severity=SEVERITIES[TARGET_RECONNECT_REQUIRED],
            active=bool(rows),
            message=(
                f"{len(rows)} active target(s) need reconnecting"
                if rows else "every active target is connected"
            ),
            observed_at=now.isoformat(),
            metadata={
                "count": len(rows),
                # Names and provider error CODES only - never a token, and never a raw
                # provider message, which can echo request parameters.
                "targets": [
                    {"id": str(row[0]), "name": row[1], "error_code": row[2]}
                    for row in rows[:10]
                ],
            },
        )

    # ------------------------------------------------------------------ helpers

    def _depths(self) -> dict[str, int]:
        try:
            return self.queue.depths()
        except Exception:  # noqa: BLE001
            logger.warning("operations_queue_unreadable")
            return {"ready": 0, "processing": 0, "delayed": 0, "dead": 0}

    @staticmethod
    def _read(reader) -> list[dict]:
        try:
            return reader() or []
        except Exception:  # noqa: BLE001
            # An unreadable heartbeat store means we cannot prove anything is alive. Reported
            # as nothing alive, which raises the signal rather than hiding it.
            logger.warning("operations_liveness_unreadable")
            return []


def _latest_tick(runners: list[dict]) -> str | None:
    ticks = [r.get("last_tick_at") for r in runners if r.get("last_tick_at")]
    return max(ticks) if ticks else None


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
