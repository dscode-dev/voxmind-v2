"""Enforcing the automatic publication budget, rather than observing it.

**The bug this exists to close.** PR-PUBLISH-02 read the day's usage once, computed a
remaining figure, and spent it. Two replicas ticking at the same moment both read `used = 2`
against a cap of 3, both concluded one publication remained, and both took it:

    replica A   count -> 2      remaining 1      creates #3
    replica B   count -> 2      remaining 1      creates #4      cap breached

A read followed by an uncoordinated write is not a budget; it is a suggestion. Once the system
can act with no operator watching, the difference matters.

**The authority is PostgreSQL, and it is the publications themselves.** Not a counter row
beside them: a counter is a second truth that drifts from the first the moment an attempt
creation fails after the counter moved, and reconciling the two would be a job nobody has.
Instead, allocation is serialised with a session-scoped advisory lock and the usage is
*recomputed inside it* before every unit is spent.

**Why session-scoped and not ``pg_advisory_xact_lock``.** The rest of the codebase uses the
transaction-scoped variant, and it would be wrong here: creating a publication commits several
times inside ``PublishingService.publish``, and a transaction-scoped lock is released by the
first of those commits — leaving the remaining allocations unprotected, which is precisely the
window being closed.

**Why the lock lives on its own connection.** Those same internal commits are why it cannot be
taken on the ORM session's connection either: SQLAlchemy returns a connection to the pool when
a transaction ends, so after the first ``publish()`` commit the session may continue on a
*different* connection — and a session-scoped advisory lock stays with the connection that
took it. The unlock would then run somewhere else and the lock would be stranded on a pooled
connection, where a ``ROLLBACK`` on return does not clear it. The next allocation would find
the budget permanently busy. Found by the completion smoke, which runs several allocations in
one process; a dedicated connection is checked out for the lock and closed at the end.

**A unit is one logical external publication** — one media item, on one target, once. Retries,
queue redeliveries, provider calls and manual publications spend nothing.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Callable

from sqlalchemy import func, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from app.models.enums import PublishAttemptStatus
from app.models.publish_attempt import PublishAttempt

logger = logging.getLogger(__name__)

# One namespace for the whole automatic budget. The cap is global today - one target per
# topic, one shared daily allowance - so one lock is the honest scope. A per-target budget
# would take a per-target key, and that is a schema question to answer when a second channel
# exists rather than a shape to guess at now.
BUDGET_LOCK_KEY = 8_812_001

# How long a replica waits for another replica's allocation before giving up. Short: an
# allocation only creates rows and enqueues, so holding the lock is a matter of milliseconds,
# and a wait longer than this means something is wrong rather than busy.
LOCK_WAIT_SEC = 10

# Statuses that mean a publication was really started and therefore really charged.
# CANCELED is excluded: an attempt withdrawn before anything was sent consumed nothing, and
# charging for it would let an operator's correction permanently shrink the day's allowance.
CHARGED_STATUSES = (
    PublishAttemptStatus.PENDING,
    PublishAttemptStatus.IN_PROGRESS,
    PublishAttemptStatus.SUCCEEDED,
    PublishAttemptStatus.FAILED_RETRYABLE,
    PublishAttemptStatus.FAILED_FINAL,
    PublishAttemptStatus.UNKNOWN,
    PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION,
)


class BudgetUnavailableError(RuntimeError):
    """Another replica holds the budget and did not finish in time.

    Not a failure of the run: the correct response is to skip this tick and come back, which
    is strictly safer than proceeding without the lock.
    """


def utc_today(now: Callable[[], datetime] | None = None) -> date:
    """The day the budget is charged to. UTC, always.

    Never the container's timezone: two replicas in different zones would roll over at
    different moments and the cap would mean different things to each of them.
    """
    moment = now() if now else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date()


class AutopublishBudget:
    """The day's automatic publication allowance, enforced under a lock."""

    def __init__(
        self,
        db: Session,
        *,
        limit: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.limit = max(0, int(limit))
        self._clock = clock
        self.date = utc_today(clock)
        self._lock_connection = None
        self._locked = False
        # Only meaningful while locked; recomputed rather than trusted between allocations.
        self._used = 0

    # ------------------------------------------------------------------- usage

    def used(self) -> int:
        """Automatic publications charged to this UTC day.

        Counted from the publication rows, which is the only place the truth lives. One row
        per job/target/media, created once and retried in place — so a retry, a redelivery or
        a provider call cannot spend the budget a second time, and neither can a manual
        publication, which carries no ``budget_date`` at all.
        """
        return int(
            self.db.query(func.count(PublishAttempt.id))
            .filter(
                PublishAttempt.initiator == "automatic",
                PublishAttempt.budget_date == self.date,
                PublishAttempt.status.in_(CHARGED_STATUSES),
            )
            .scalar()
            or 0
        )

    def remaining(self) -> int:
        return max(0, self.limit - self.used())

    # -------------------------------------------------------------- allocation

    @contextmanager
    def hold(self):
        """Serialise allocation across replicas for the duration of the block.

        Blocking rather than try-and-skip, because the two replicas in the race are both
        *entitled* to allocate — the loser should wait a moment and then find the truth, not
        walk away thinking it was busy. Bounded by ``lock_timeout`` so a pathological holder
        cannot stall a scheduler tick indefinitely.
        """
        if not self._supported():
            # SQLite (the test harness) serialises writers anyway, so the lock is a no-op
            # there. The allocation logic below is identical on both backends.
            self._locked = True
            try:
                self._used = self.used()
                yield self
            finally:
                self._locked = False
            return

        # A connection of our own, checked out from the same engine. It is never committed
        # on and never handed back mid-allocation, so the lock stays where it was taken.
        connection = self.db.get_bind().connect()
        try:
            connection.exec_driver_sql(f"SET lock_timeout = '{LOCK_WAIT_SEC}s'")
            connection.exec_driver_sql(
                f"SELECT pg_advisory_lock({BUDGET_LOCK_KEY})"
            )
        except (OperationalError, DBAPIError) as exc:
            connection.close()
            raise BudgetUnavailableError(
                f"the autopublish budget was held by another replica ({type(exc).__name__})"
            ) from exc

        self._lock_connection = connection
        self._locked = True
        try:
            self._used = self.used()
            yield self
        finally:
            self._locked = False
            try:
                # Belt and braces: unlock the key, then drop anything this connection still
                # holds, so a bug above can never strand a lock on a pooled connection.
                connection.exec_driver_sql(
                    f"SELECT pg_advisory_unlock({BUDGET_LOCK_KEY})"
                )
                connection.exec_driver_sql("SELECT pg_advisory_unlock_all()")
            except DBAPIError:
                logger.warning("autopublish_budget_unlock_failed")
            finally:
                connection.close()
                self._lock_connection = None

    def allocatable(self, wanted: int) -> int:
        """How many of ``wanted`` units may be spent right now.

        Recomputed from the database on every call rather than decremented in memory: inside
        the lock nobody else can have written, but between one job and the next this process
        has itself created rows, and the point of the exercise is that the number always
        comes from the publications.
        """
        if not self._locked:
            raise RuntimeError("budget allocation attempted without holding the lock")
        self._used = self.used()
        return max(0, min(int(wanted), self.limit - self._used))

    # ------------------------------------------------------------------ helpers

    def _supported(self) -> bool:
        bind = self.db.bind
        return bind is not None and bind.dialect.name == "postgresql"

    def snapshot(self) -> dict[str, object]:
        """The authoritative read model — the same query the enforcement path uses."""
        used = self.used()
        return {
            "budget_date": self.date.isoformat(),
            "daily_limit": self.limit,
            "daily_used": used,
            "daily_remaining": max(0, self.limit - used),
        }
