"""PR-AUTONOMY-HARDEN-01 — exact budgets, operational signals, crash hygiene.

Three unrelated-looking areas with one thing in common: each was a place where the system
*observed* something it should have been *enforcing* or *proving*. A cap that was read and
then spent, a runner whose liveness was a configuration flag, and a temp file whose cleanup
depended on the process being allowed to finish.

The cross-replica budget test that matters most runs against real PostgreSQL — SQLite
serialises writers, so it cannot exhibit the race and cannot prove it is closed. It lives in
the smoke rather than here, and is named in the report.
"""
from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pytest

from app.core.settings import settings
from app.models.automation_state import AutomationState
from app.models.enums import (
    PipelineState,
    PublishAttemptStatus,
    PublishTargetConnectionStatus,
)
from app.models.publish_attempt import PublishAttempt
from app.publishing.publish_queue import PublishQueue, command_payload
from app.publishing.temp_cleanup import SPOOL_PREFIX, SPOOL_SUFFIX, sweep_stale_spools
from app.services.autopublish_budget import (
    CHARGED_STATUSES,
    AutopublishBudget,
    utc_today,
)
from app.services.autopublish_service import DAILY_LIMIT, PER_RUN_LIMIT
from app.services.operations_service import (
    AUTOMATION_RUNNER_STALE,
    CRITICAL,
    DEGRADED,
    HEALTHY,
    HIGH,
    PUBLISH_DEAD_LETTERS,
    PUBLISHER_DOWN,
    REPEATED_AUTOMATION_FAILURE,
    SEVERITIES,
    STALLED_PUBLISH_QUEUE,
    TARGET_RECONNECT_REQUIRED,
    UNRESOLVED_PUBLICATIONS,
    OperationsService,
)
from tests.test_autopublish import (  # noqa: F401 - fixtures are used by pytest
    ALIVE,
    _attempt,
    attempts_count,
    autopublish_config,
    autopublish_target,
    make_ready_job,
    make_topic,
    policy,
    queue,
)
from tests.test_publishing import (  # noqa: F401 - publishing_config is autouse
    StubArtifacts,
    StubPublisher,
    make_target,
    publishing_config,
)


# ===========================================================================
# Budget semantics
# ===========================================================================


def test_the_budget_day_is_utc_and_not_the_container_timezone():
    """Two replicas in different zones must roll over at the same instant."""
    at_2330_utc = datetime(2026, 3, 1, 23, 30, tzinfo=timezone.utc)
    assert utc_today(lambda: at_2330_utc) == date(2026, 3, 1)

    # The same instant expressed in another zone is still the same UTC day.
    in_tokyo = at_2330_utc.astimezone(timezone(timedelta(hours=9)))
    assert utc_today(lambda: in_tokyo) == date(2026, 3, 1)

    just_after = datetime(2026, 3, 2, 0, 1, tzinfo=timezone.utc)
    assert utc_today(lambda: just_after) == date(2026, 3, 2)


def test_a_naive_clock_is_read_as_utc():
    naive = datetime(2026, 3, 1, 23, 30)
    assert utc_today(lambda: naive) == date(2026, 3, 1)


def test_the_budget_counts_only_automatic_publications_of_its_own_day(db, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))

    today = utc_today()
    _attempt(db, job, target, PublishAttemptStatus.SUCCEEDED, media="a.mp4")
    _attempt(db, job, target, PublishAttemptStatus.SUCCEEDED, media="b.mp4",
             budget_date=today - timedelta(days=1))
    _attempt(db, job, target, PublishAttemptStatus.SUCCEEDED, media="c.mp4",
             initiator="manual")

    assert AutopublishBudget(db, limit=10).used() == 1


def test_a_canceled_publication_gives_its_unit_back(db, no_event_fanout):
    """An operator's correction must not permanently shrink the day's allowance."""
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    attempt = _attempt(db, job, target, PublishAttemptStatus.PENDING)

    assert AutopublishBudget(db, limit=3).used() == 1
    attempt.status = PublishAttemptStatus.CANCELED
    db.flush()
    assert AutopublishBudget(db, limit=3).used() == 0
    assert PublishAttemptStatus.CANCELED not in CHARGED_STATUSES


@pytest.mark.parametrize("status", list(CHARGED_STATUSES))
def test_every_started_publication_is_charged(db, status, no_event_fanout):
    """Including the ones that failed: the attempt was made, and it reached the provider."""
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    _attempt(db, job, target, status)

    assert AutopublishBudget(db, limit=3).used() == 1


def test_allocation_outside_the_lock_is_a_programming_error(db, no_event_fanout):
    budget = AutopublishBudget(db, limit=3)
    with pytest.raises(RuntimeError, match="without holding the lock"):
        budget.allocatable(1)


def test_the_lock_is_taken_on_its_own_connection(db, no_event_fanout):
    """Regression: the lock used to be stranded on a pooled connection.

    ``publish()`` commits several times, and SQLAlchemy returns a connection to the pool when
    a transaction ends - so a session-scoped advisory lock taken on the ORM session's
    connection could be released somewhere else and left held on a connection nobody was
    looking at. The next allocation then found the budget permanently busy. The completion
    smoke hit exactly that after three allocations in one process.
    """
    budget = AutopublishBudget(db, limit=3)
    assert budget._lock_connection is None

    with budget.hold():
        # On PostgreSQL a dedicated connection is checked out; on the SQLite test harness
        # the lock is a no-op and there is nothing to hold.
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            assert budget._lock_connection is not None
            assert budget._lock_connection is not db.connection()

    assert budget._lock_connection is None, "the connection is released, not leaked"


def test_repeated_allocations_do_not_wedge_the_budget(db, queue, monkeypatch,
                                                       no_event_fanout):
    """Three allocations in one process, each of which commits inside publish()."""
    monkeypatch.setattr(settings, "autopublish_max_per_day", 10, raising=False)
    target = autopublish_target(db)
    topic = make_topic(db, target=target)
    service = policy(queue)

    for _ in range(3):
        make_ready_job(db, topic)
        report = service.run(db, dry_run=False)
        assert "budget_locked" not in report.blocked_reasons, (
            "a previous allocation left the lock held"
        )

    assert attempts_count(db) == 3


def test_the_budget_recomputes_between_allocations(db, no_event_fanout):
    """Not decremented in memory: the number always comes from the publications."""
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    budget = AutopublishBudget(db, limit=2)

    with budget.hold():
        assert budget.allocatable(5) == 2
        _attempt(db, job, target, PublishAttemptStatus.PENDING, media="a.mp4")
        assert budget.allocatable(5) == 1
        _attempt(db, job, target, PublishAttemptStatus.PENDING, media="b.mp4")
        assert budget.allocatable(5) == 0


# ===========================================================================
# The caps count media items
# ===========================================================================


def test_a_run_larger_than_the_budget_publishes_what_it_can(
    db, queue, monkeypatch, no_event_fanout
):
    """Partial allocation, restored by PR-PUBLISH-COMPLETE-01.

    PR-AUTONOMY-HARDEN-01 had to refuse this outright, because a partly published run would
    have been settled to PUBLISHED and the remaining clips silently lost. Completion is now
    outstanding-aware, so the run takes what the budget allows and keeps the rest.
    """
    monkeypatch.setattr(settings, "autopublish_max_per_day", 1, raising=False)
    monkeypatch.setattr(settings, "autopublish_max_per_tick", 5, raising=False)
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))

    report = policy(queue, artifacts=StubArtifacts(videos=3)).run(db, dry_run=False)

    assert attempts_count(db) == 1, "one unit buys one clip"
    assert report.candidates[0].outstanding == 3
    assert report.candidates[0].deferred == 2
    assert job.state != PipelineState.PUBLISHED


def test_a_multi_clip_run_spends_one_unit_per_clip(db, queue, monkeypatch,
                                                    no_event_fanout):
    """Three clips cost three units, not one - the cap is about videos, not jobs."""
    monkeypatch.setattr(settings, "autopublish_max_per_day", 3, raising=False)
    monkeypatch.setattr(settings, "autopublish_max_per_tick", 5, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue, artifacts=StubArtifacts(videos=3)).run(db, dry_run=False)

    assert attempts_count(db) == 3
    assert report.queued == 3
    assert AutopublishBudget(db, limit=3).remaining() == 0


def test_the_per_tick_cap_also_counts_media_items(db, queue, monkeypatch,
                                                   no_event_fanout):
    """Two single-clip runs fit a per-tick cap of two; a third does not."""
    monkeypatch.setattr(settings, "autopublish_max_per_tick", 2, raising=False)
    monkeypatch.setattr(settings, "autopublish_max_per_day", 50, raising=False)
    target = autopublish_target(db)
    topic = make_topic(db, target=target)
    for _ in range(3):
        make_ready_job(db, topic)

    report = policy(queue).run(db, dry_run=False)

    assert attempts_count(db) == 2
    assert report.queued == 2
    assert report.blocked_reasons.get(PER_RUN_LIMIT) == 1


def test_a_four_clip_run_takes_two_units_of_a_tick_cap_of_two(db, queue, monkeypatch,
                                                                no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_max_per_tick", 2, raising=False)
    monkeypatch.setattr(settings, "autopublish_max_per_day", 50, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue, artifacts=StubArtifacts(videos=4)).run(db, dry_run=False)

    assert attempts_count(db) == 2, "the tick takes what it may and defers the rest"
    assert report.queued == 2
    assert report.candidates[0].deferred == 2


def test_a_run_publishes_every_clip_in_declared_order(db, queue, monkeypatch,
                                                       no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_max_per_day", 4, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    policy(queue, artifacts=StubArtifacts(videos=4)).run(db, dry_run=False)

    identities = sorted(a.media_identity for a in db.query(PublishAttempt).all())
    assert identities == [
        "final_clips/final_clip_01.mp4", "final_clips/final_clip_02.mp4",
        "final_clips/final_clip_03.mp4", "final_clips/final_clip_04.mp4",
    ]


def test_a_large_run_makes_progress_across_budgets(db, queue, monkeypatch,
                                                    no_event_fanout):
    """The P1 this PR closes: a run larger than the cap used to stall for ever."""
    monkeypatch.setattr(settings, "autopublish_max_per_day", 1, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))
    service = policy(queue, artifacts=StubArtifacts(videos=2))

    service.run(db, dry_run=False)
    assert attempts_count(db) == 1, "one clip today"

    # The publisher has to actually run it: while a publication is in flight the run is
    # PUBLISHING, and a run that is publishing is not a candidate for more allocation.
    _drain(db, queue, service)

    monkeypatch.setattr(settings, "autopublish_max_per_day", 2, raising=False)
    service.run(db, dry_run=False)
    assert attempts_count(db) == 2, "the outstanding clip once the budget allows"

    identities = sorted(a.media_identity for a in db.query(PublishAttempt).all())
    assert identities == [
        "final_clips/final_clip_01.mp4", "final_clips/final_clip_02.mp4"
    ], "no clip was published twice"


def test_a_run_with_nothing_outstanding_is_not_a_candidate(db, queue, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    _attempt(db, job, target, PublishAttemptStatus.SUCCEEDED,
             media="final_clips/final_clip_01.mp4")

    report = policy(queue).run(db, dry_run=False)

    assert report.queued == 0
    assert attempts_count(db) == 1


# ===========================================================================
# Budget is not leaked
# ===========================================================================


def test_a_dry_run_spends_nothing(db, queue, no_event_fanout):
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    before = AutopublishBudget(db, limit=10).used()
    policy(queue).run(db, dry_run=True)
    assert AutopublishBudget(db, limit=10).used() == before


def test_an_idempotent_rerun_does_not_spend_twice(db, queue, monkeypatch,
                                                   no_event_fanout):
    """The ordering hazard: reserve, then find the row already exists, and leak a unit."""
    monkeypatch.setattr(settings, "autopublish_max_per_day", 5, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))
    service = policy(queue)

    service.run(db, dry_run=False)
    used_after_first = AutopublishBudget(db, limit=5).used()

    service.run(db, dry_run=False)
    service.run(db, dry_run=False)

    assert AutopublishBudget(db, limit=5).used() == used_after_first == 1
    assert attempts_count(db) == 1


def test_a_retry_does_not_spend_a_second_unit(db, queue, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    attempt = _attempt(db, job, target, PublishAttemptStatus.FAILED_RETRYABLE)

    assert AutopublishBudget(db, limit=5).used() == 1
    attempt.attempt_no = 3
    attempt.status = PublishAttemptStatus.SUCCEEDED
    db.flush()
    assert AutopublishBudget(db, limit=5).used() == 1, "one row, one unit"


def test_a_manual_publication_spends_no_automatic_budget(db, queue, no_event_fanout):
    from tests.test_publishing import make_publishable_run
    from app.services.publishing_service import PublishingService
    from tests.test_publishing import StubMediaSource

    target = make_target(db)
    job = make_publishable_run(db)
    PublishingService(
        publisher=StubPublisher(), artifacts=StubArtifacts(),
        media_source=StubMediaSource(), queue=queue,
    ).publish(db, job=job, target=target, dry_run=False)

    attempt = db.query(PublishAttempt).one()
    assert attempt.initiator == "manual"
    assert attempt.budget_date is None, "no day to be charged to"
    assert AutopublishBudget(db, limit=5).used() == 0


def test_a_new_utc_day_restores_the_budget(db, queue, no_event_fanout):
    """Injected clock, so the boundary is tested without waiting for midnight."""
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    yesterday = datetime(2026, 3, 1, 23, 59, tzinfo=timezone.utc)
    today = datetime(2026, 3, 2, 0, 1, tzinfo=timezone.utc)

    _attempt(db, job, target, PublishAttemptStatus.SUCCEEDED,
             budget_date=utc_today(lambda: yesterday))

    assert AutopublishBudget(db, limit=1, clock=lambda: yesterday).remaining() == 0
    assert AutopublishBudget(db, limit=1, clock=lambda: today).remaining() == 1


def test_the_report_states_the_budget_day(db, queue, no_event_fanout):
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue).run(db, dry_run=True)
    assert report.budget_date == utc_today().isoformat()


def test_the_status_read_model_uses_the_enforcement_query(db, queue, no_event_fanout):
    """A status page that computes its own number starts disagreeing with the gate."""
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    _attempt(db, job, target, PublishAttemptStatus.PENDING)

    status = policy(queue).status(db)
    budget = AutopublishBudget(db, limit=status["daily_cap"])

    assert status["daily_used"] == budget.used()
    assert status["budget_date"] == budget.date.isoformat()
    assert status["daily_remaining"] == budget.remaining()


# ===========================================================================
# Operational signals
# ===========================================================================


def _drain(db, queue, service, limit: int = 10) -> None:
    """Execute whatever the policy queued, as the publisher container would.

    The policy only creates commands; a run stays PUBLISHING until they are executed, which
    is correct and is why a test about progress across budgets has to run them.
    """
    from app.publishing.identity import PublisherHeartbeat
    from app.services.publish_runtime import PublisherRuntime

    # Accepts either the policy service or the publishing service it wraps, so callers do
    # not have to know which layer they are holding.
    publishing = getattr(service, "publishing", service)
    worker = PublisherRuntime(
        worker_id="test-publisher",
        queue=queue,
        publishing=publishing,
        heartbeat=PublisherHeartbeat("test-publisher", queue.redis),
        session_factory=lambda: _KeepOpen(db),
    )
    for _ in range(limit):
        if not queue.depths()["ready"]:
            break
        worker.tick()
    db.expire_all()


class _KeepOpen:
    """The test session with ``close`` suppressed. Test-only scaffolding."""

    def __init__(self, session):
        self._session = session

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self._session, name)


def operations(queue, publishers=None, runners=None, clock=None) -> OperationsService:
    return OperationsService(
        queue=queue,
        publisher_reader=lambda: list(publishers or []),
        runner_reader=lambda: list(runners or []),
        clock=clock,
    )


def _signal(health: dict, code: str) -> dict:
    return next(s for s in health["signals"] if s["code"] == code)


def test_a_quiet_system_is_healthy(db, queue, monkeypatch, no_event_fanout):
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    health = operations(queue, publishers=ALIVE).health(db)

    assert health["status"] == HEALTHY
    assert health["active_signals"] == []


def test_publisher_down_when_work_waits_and_nobody_is_alive(db, queue, monkeypatch,
                                                             no_event_fanout):
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))

    health = operations(queue, publishers=[]).health(db)

    assert _signal(health, PUBLISHER_DOWN)["active"] is True
    assert health["status"] == CRITICAL
    assert SEVERITIES[PUBLISHER_DOWN] == CRITICAL


def test_publisher_down_clears_when_a_worker_appears(db, queue, monkeypatch,
                                                      no_event_fanout):
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))

    assert _signal(operations(queue, publishers=[]).health(db), PUBLISHER_DOWN)["active"]
    health = operations(queue, publishers=ALIVE).health(db)
    assert _signal(health, PUBLISHER_DOWN)["active"] is False
    assert health["status"] == HEALTHY


def test_no_publisher_alarm_when_publishing_is_switched_off(db, queue, monkeypatch,
                                                             no_event_fanout):
    """An operator who disabled publishing does not need telling nothing is publishing."""
    monkeypatch.setattr(settings, "publishing_enabled", False, raising=False)
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))

    assert _signal(operations(queue, publishers=[]).health(db), PUBLISHER_DOWN)["active"] is False


def test_no_publisher_alarm_when_nothing_is_waiting(db, queue, monkeypatch,
                                                     no_event_fanout):
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    health = operations(queue, publishers=[]).health(db)
    assert _signal(health, PUBLISHER_DOWN)["active"] is False
    assert health["status"] == HEALTHY


def test_unresolved_publications_surface_with_count_and_age(db, queue, monkeypatch,
                                                             no_event_fanout):
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    attempt = _attempt(db, job, target, PublishAttemptStatus.UNKNOWN)
    attempt.created_at = datetime.utcnow() - timedelta(hours=2)
    db.flush()

    health = operations(queue, publishers=ALIVE).health(db)
    signal = _signal(health, UNRESOLVED_PUBLICATIONS)

    assert signal["active"] is True
    assert signal["metadata"]["count"] == 1
    assert signal["metadata"]["oldest_age_sec"] > 3600
    assert health["status"] == DEGRADED


def test_the_unresolved_signal_carries_no_session_uri(db, queue, no_event_fanout):
    import json

    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    _attempt(db, job, target, PublishAttemptStatus.UNKNOWN,
             upload_session_uri_encrypted="ciphertext-looking-value")

    flat = json.dumps(operations(queue, publishers=ALIVE).health(db), default=str)
    assert "ciphertext-looking-value" not in flat
    assert "upload_session_uri" not in flat


def test_the_unresolved_signal_clears_when_resolved(db, queue, monkeypatch,
                                                     no_event_fanout):
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    attempt = _attempt(db, job, target, PublishAttemptStatus.UNKNOWN)

    assert _signal(operations(queue, publishers=ALIVE).health(db),
                   UNRESOLVED_PUBLICATIONS)["active"] is True

    attempt.status = PublishAttemptStatus.SUCCEEDED
    attempt.external_id = "vid_resolved"
    db.flush()

    health = operations(queue, publishers=ALIVE).health(db)
    assert _signal(health, UNRESOLVED_PUBLICATIONS)["active"] is False
    assert health["status"] == HEALTHY


def test_dead_letters_signal_above_the_threshold(db, queue, monkeypatch,
                                                  no_event_fanout):
    monkeypatch.setattr(settings, "operations_dead_letter_threshold", 2, raising=False)
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)

    queue.redis.lpush(queue.dead_key, "{}")
    assert _signal(operations(queue, publishers=ALIVE).health(db),
                   PUBLISH_DEAD_LETTERS)["active"] is False

    queue.redis.lpush(queue.dead_key, "{}")
    assert _signal(operations(queue, publishers=ALIVE).health(db),
                   PUBLISH_DEAD_LETTERS)["active"] is True


def test_a_deep_queue_that_is_draining_is_not_a_stall(db, queue, monkeypatch,
                                                       no_event_fanout):
    """Backlog is not stall: a healthy system working through one looks exactly like this."""
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    for index in range(5):
        queue.enqueue(command_payload(publish_attempt_id=str(uuid.uuid4()),
                                      pipeline_job_id="j", target_id="t",
                                      media_identity=f"m{index}"))
    settled = _attempt(db, job, target, PublishAttemptStatus.SUCCEEDED)
    settled.finished_at = datetime.utcnow()
    db.flush()

    assert _signal(operations(queue, publishers=ALIVE).health(db),
                   STALLED_PUBLISH_QUEUE)["active"] is False


def test_a_queue_with_a_live_publisher_and_no_progress_is_a_stall(db, queue, monkeypatch,
                                                                   no_event_fanout):
    monkeypatch.setattr(settings, "operations_stall_window_sec", 60, raising=False)
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))

    # A publication that has been waiting longer than the window is the evidence. Without
    # one, a queue that only just received work would look stalled the instant it filled.
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    waiting = _attempt(db, job, target, PublishAttemptStatus.PENDING)
    waiting.created_at = datetime.utcnow() - timedelta(hours=1)
    db.flush()

    signal = _signal(operations(queue, publishers=ALIVE).health(db), STALLED_PUBLISH_QUEUE)
    assert signal["active"] is True
    assert signal["metadata"]["settled_in_window"] == 0
    assert signal["metadata"]["waiting_over_window"] == 1


def test_a_reconnect_required_target_raises_a_signal(db, queue, monkeypatch,
                                                      no_event_fanout):
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    target = autopublish_target(
        db, connection_status=PublishTargetConnectionStatus.RECONNECT_REQUIRED,
        last_error_code="invalid_grant",
    )

    signal = _signal(operations(queue, publishers=ALIVE).health(db),
                     TARGET_RECONNECT_REQUIRED)

    assert signal["active"] is True
    assert signal["metadata"]["targets"][0]["error_code"] == "invalid_grant"

    target.connection_status = PublishTargetConnectionStatus.CONNECTED
    db.flush()
    assert _signal(operations(queue, publishers=ALIVE).health(db),
                   TARGET_RECONNECT_REQUIRED)["active"] is False


def test_an_inactive_target_needing_reconnection_is_not_alarming(db, queue, monkeypatch,
                                                                  no_event_fanout):
    """Nobody is trying to publish to it."""
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    autopublish_target(
        db, is_active=False,
        connection_status=PublishTargetConnectionStatus.RECONNECT_REQUIRED,
    )
    assert _signal(operations(queue, publishers=ALIVE).health(db),
                   TARGET_RECONNECT_REQUIRED)["active"] is False


def test_repeated_automation_failures_raise_a_signal(db, queue, monkeypatch,
                                                      no_event_fanout):
    monkeypatch.setattr(settings, "operations_failure_threshold", 3, raising=False)
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    topic = make_topic(db)
    db.add(AutomationState(topic_id=topic.id, consecutive_failures=1))
    db.flush()

    assert _signal(operations(queue, publishers=ALIVE).health(db),
                   REPEATED_AUTOMATION_FAILURE)["active"] is False

    state = db.query(AutomationState).one()
    state.consecutive_failures = 4
    db.flush()

    signal = _signal(operations(queue, publishers=ALIVE).health(db),
                     REPEATED_AUTOMATION_FAILURE)
    assert signal["active"] is True
    assert signal["metadata"]["worst"] == 4


# ===========================================================================
# Automation liveness
# ===========================================================================


def test_runner_enabled_alone_no_longer_reads_as_alive(db, queue, monkeypatch,
                                                        no_event_fanout):
    """The PR-SCHEDULER-01 gap: a configuration flag is not evidence of a running loop."""
    monkeypatch.setattr(settings, "automation_runner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "autonomous_pipeline_enabled", True, raising=False)

    health = operations(queue, publishers=ALIVE, runners=[]).health(db)
    signal = _signal(health, AUTOMATION_RUNNER_STALE)

    assert signal["active"] is True
    assert signal["metadata"]["configured"] is True
    assert signal["metadata"]["runners_alive"] == 0
    assert SEVERITIES[AUTOMATION_RUNNER_STALE] == HIGH


def test_a_ticking_runner_clears_the_signal(db, queue, monkeypatch, no_event_fanout):
    monkeypatch.setattr(settings, "automation_runner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "autonomous_pipeline_enabled", True, raising=False)
    runners = [{"worker_id": "runner-1",
                "last_tick_at": datetime.now(timezone.utc).isoformat()}]

    health = operations(queue, publishers=ALIVE, runners=runners).health(db)
    signal = _signal(health, AUTOMATION_RUNNER_STALE)

    assert signal["active"] is False
    assert signal["metadata"]["last_tick_at"] == runners[0]["last_tick_at"]


def test_no_runner_alarm_when_the_loop_is_deliberately_off(db, queue, monkeypatch,
                                                            no_event_fanout):
    monkeypatch.setattr(settings, "automation_runner_enabled", False, raising=False)
    assert _signal(operations(queue, publishers=ALIVE, runners=[]).health(db),
                   AUTOMATION_RUNNER_STALE)["active"] is False


def test_the_heartbeat_expires_rather_than_being_declared():
    from app.publishing.identity import AutomationHeartbeat

    fake = fakeredis.FakeRedis(decode_responses=True)
    heartbeat = AutomationHeartbeat("runner-1", fake, ttl_sec=60)
    heartbeat.beat(state="idle", last_tick_at="2026-01-01T00:00:00+00:00")

    alive = AutomationHeartbeat.alive(fake)
    assert [r["worker_id"] for r in alive] == ["runner-1"]
    assert alive[0]["last_tick_at"] == "2026-01-01T00:00:00+00:00"

    fake.delete(heartbeat.key)  # what the TTL does on its own
    assert AutomationHeartbeat.alive(fake) == []


def test_runner_and_publisher_heartbeats_do_not_collide():
    from app.publishing.identity import AutomationHeartbeat, PublisherHeartbeat

    fake = fakeredis.FakeRedis(decode_responses=True)
    PublisherHeartbeat("publisher-1", fake).beat()
    AutomationHeartbeat("runner-1", fake).beat()

    assert [w["worker_id"] for w in PublisherHeartbeat.alive(fake)] == ["publisher-1"]
    assert [r["worker_id"] for r in AutomationHeartbeat.alive(fake)] == ["runner-1"]


def test_unreadable_liveness_reports_nothing_alive(db, queue, monkeypatch,
                                                    no_event_fanout):
    """Unprovable is not fine: it raises the signal rather than hiding it."""
    monkeypatch.setattr(settings, "automation_runner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "autonomous_pipeline_enabled", True, raising=False)

    def explode():
        raise RuntimeError("redis is down")

    service = OperationsService(queue=queue, publisher_reader=explode,
                                runner_reader=explode)
    health = service.health(db)
    assert health["automation_runners_alive"] == 0
    assert _signal(health, AUTOMATION_RUNNER_STALE)["active"] is True


# ===========================================================================
# Process health stays separate
# ===========================================================================


def test_operational_degradation_does_not_break_process_health(db, no_event_fanout):
    """A YouTube token expiring must not make an orchestrator restart the API."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.router import api_router
    from app.db.session import get_db
    from app.main import app as real_app

    autopublish_target(
        db, connection_status=PublishTargetConnectionStatus.RECONNECT_REQUIRED
    )

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(real_app) as client:
        assert client.get("/health").status_code == 200

    # And the operations endpoint reports the degradation with a 200, not a 5xx: the request
    # succeeded, and the answer is that the product is degraded.
    assert "operations" in {route.path.split("/")[2] for route in api_router.routes
                            if hasattr(route, "path") and route.path.count("/") > 2}


# ===========================================================================
# Temp-file hygiene
# ===========================================================================


def _spool(directory: Path, age_sec: int, size: int = 1024) -> Path:
    path = directory / f"{SPOOL_PREFIX}{uuid.uuid4().hex[:8]}{SPOOL_SUFFIX}"
    path.write_bytes(b"x" * size)
    when = time.time() - age_sec
    import os

    os.utime(path, (when, when))
    return path


def test_only_old_spools_of_ours_are_removed(tmp_path):
    old = _spool(tmp_path, age_sec=10_000, size=2048)
    fresh = _spool(tmp_path, age_sec=5)
    unrelated = tmp_path / "someone-elses-video.mp4"
    unrelated.write_bytes(b"y" * 100)
    also_unrelated = tmp_path / "clipflow-publish-notours.txt"
    also_unrelated.write_bytes(b"z" * 100)

    result = sweep_stale_spools(tmp_path, stale_after_sec=3600)

    assert not old.exists()
    assert fresh.exists(), "a live upload could still be streaming from it"
    assert unrelated.exists()
    assert also_unrelated.exists(), "the suffix is part of the ownership check"
    assert result.removed == 1
    assert result.bytes_reclaimed == 2048
    assert result.skipped_recent == 1


def test_the_sweep_reports_what_it_reclaimed(tmp_path):
    _spool(tmp_path, age_sec=10_000, size=4096)
    _spool(tmp_path, age_sec=20_000, size=1024)

    result = sweep_stale_spools(tmp_path, stale_after_sec=3600).as_dict()

    assert result["files_removed"] == 2
    assert result["bytes_reclaimed"] == 5120
    assert result["oldest_age_sec"] >= 20_000


def test_the_sweep_is_bounded(tmp_path):
    for _ in range(50):
        _spool(tmp_path, age_sec=10_000, size=1)

    result = sweep_stale_spools(tmp_path, stale_after_sec=3600, max_scan=10)

    assert result.scanned <= 10
    assert result.removed <= 10
    assert len(list(tmp_path.glob(f"{SPOOL_PREFIX}*"))) >= 40, "the rest waits for next time"


def test_an_empty_directory_is_fine(tmp_path):
    result = sweep_stale_spools(tmp_path, stale_after_sec=3600)
    assert result.as_dict() == {
        "scanned": 0, "files_removed": 0, "bytes_reclaimed": 0,
        "oldest_age_sec": 0, "skipped_recent": 0, "errors": 0,
    }


def test_a_missing_directory_does_not_raise(tmp_path):
    result = sweep_stale_spools(tmp_path / "nope", stale_after_sec=3600)
    assert result.removed == 0


def test_the_sweep_prefix_matches_what_the_media_source_writes():
    """A rename on one side and not the other would silently stop cleaning anything."""
    import inspect

    from app.publishing import media_source

    source = inspect.getsource(media_source.MinioMediaSource.download)
    assert f'prefix="{SPOOL_PREFIX}"' in source
    assert f'suffix="{SPOOL_SUFFIX}"' in source
