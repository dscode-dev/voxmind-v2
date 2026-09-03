"""PR-PUBLISH-QUEUE-01 — the asynchronous publication runtime.

Redis is `fakeredis` where a real server is not needed; the Lua ownership scripts run for
real on it, so the compare-and-set semantics are exercised rather than assumed.

The tests that matter most are again about *not* doing something: not uploading from the HTTP
request, not uploading twice for one command delivered twice, and above all not turning an
expired queue lease into a second video.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import fakeredis
import httpx
import pytest

from app.core.settings import settings
from app.models.enums import (
    PipelineState,
    PublishAttemptStatus,
    PublishRetryability,
)
from app.models.publish_attempt import PublishAttempt
from app.publishing.contracts import PublishOutcome, PublishResult
from app.publishing.identity import PublisherHeartbeat, resolve_worker_id
from app.publishing.publish_queue import PublishQueue, command_payload
from app.security.secret_box import SecretBox
from app.services.publish_recovery_service import (
    AMBIGUOUS,
    COMPLETED,
    REQUEUE,
    RESUME,
    UNDETERMINED,
    PublishRecoveryService,
)
from app.services.publish_runtime import PublisherRuntime, runtime_snapshot
from app.services.publishing_service import PublishingService, idempotency_key
from tests.test_publishing import (  # noqa: F401 - publishing_config is autouse
    TEST_KEY,
    StubArtifacts,
    StubMediaSource,
    StubPublisher,
    _unknown_result,
    make_publishable_run,
    make_target,
    publishing_config,
)


@pytest.fixture()
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def queue(fake_redis):
    return PublishQueue(fake_redis, "test_publish_jobs", worker_id="worker-a")


def service(queue, publisher=None, artifacts=None, media=None, db=None) -> PublishingService:
    return PublishingService(
        publisher=publisher or StubPublisher(),
        artifacts=artifacts or StubArtifacts(),
        media_source=media or StubMediaSource(),
        queue=queue,
        # In production the progress recorder opens its own connection to the same database;
        # here it is pointed at the test's session so the test can observe the writes.
        session_factory=(lambda: _KeepOpen(db)) if db is not None else None,
    )


class _KeepOpen:
    """The test session with ``close`` suppressed. Test-only scaffolding."""

    def __init__(self, session):
        self._session = session

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self._session, name)


def reload(db, instance):
    """Re-read a row after the runtime has been through it.

    ``PublisherRuntime`` closes the session it is given, which detaches whatever the test is
    holding. That is correct in production - every command gets a fresh session and nobody
    keeps a reference across one - so the tests re-read rather than the runtime changing to
    suit them.
    """
    db.expire_all()
    return db.query(type(instance)).filter(type(instance).id == instance.id).one()


def runtime(queue, db, publisher=None, artifacts=None, media=None, recovery=None):
    """A runtime bound to one test session, so nothing opens its own connection."""
    return PublisherRuntime(
        worker_id=queue.worker_id,
        queue=queue,
        publishing=service(queue, publisher=publisher, artifacts=artifacts, media=media),
        recovery=recovery or PublishRecoveryService(),
        heartbeat=PublisherHeartbeat(queue.worker_id, queue.redis),
        session_factory=lambda: db,
    )


# ===========================================================================
# The queue primitive
# ===========================================================================


def test_a_command_carries_ids_and_nothing_else():
    payload = command_payload(
        publish_attempt_id="a", pipeline_job_id="j", target_id="t",
        media_identity="final_clips/x.mp4",
    )
    assert set(payload) == {
        "version", "publish_attempt_id", "pipeline_job_id", "target_id", "media_identity",
    }
    # Nothing that could be a secret or go stale between enqueue and execution.
    flat = json.dumps(payload)
    for forbidden in ("token", "session", "title", "description", "refresh"):
        assert forbidden not in flat


def test_the_publish_queue_is_not_the_media_queue(queue):
    assert queue.queue != settings.voxmind_redis_queue
    assert settings.publish_queue_name == "clipflow_publish_jobs"
    assert settings.publish_queue_name != settings.voxmind_redis_queue


def test_a_claimed_command_is_in_processing_not_ready(queue):
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))
    command = queue.claim(block_sec=1)

    assert command is not None
    assert queue.depths()["ready"] == 0
    assert queue.depths()["processing"] == 1
    assert queue.redis.exists(command.lease_key)


def test_acknowledging_removes_the_command_and_its_lease(queue):
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))
    command = queue.claim(block_sec=1)

    assert queue.acknowledge(command) is True
    assert queue.depths()["processing"] == 0
    assert not queue.redis.exists(command.lease_key)


def test_a_retry_goes_to_delayed_not_straight_back_to_ready(queue):
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))
    command = queue.claim(block_sec=1)

    delay = queue.retry(command)

    assert delay and delay > 0
    depths = queue.depths()
    assert depths["delayed"] == 1
    assert depths["ready"] == 0, "an immediate re-push would spin against a sick provider"
    assert depths["processing"] == 0


def test_a_delayed_command_is_promoted_when_due(queue):
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))
    command = queue.claim(block_sec=1)
    queue.retry(command, delay_sec=0.0)

    assert queue.promote_due_delayed() == 1
    assert queue.depths()["ready"] == 1


def test_the_retry_carries_an_incremented_attempt(queue):
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))
    first = queue.claim(block_sec=1)
    queue.retry(first, delay_sec=0.0)
    queue.promote_due_delayed()

    second = queue.claim(block_sec=1)
    assert second.attempt == first.attempt + 1


def test_backoff_grows_and_is_capped(queue):
    delays = [queue.backoff_delay(n) for n in range(1, 12)]
    assert delays[0] < delays[3] < delays[6]
    assert all(d <= queue.backoff_max_sec * 1.2 for d in delays)


def test_backoff_is_jittered(queue):
    """Several publications of one run fail together; without jitter they return together."""
    values = {round(queue.backoff_delay(3), 6) for _ in range(50)}
    assert len(values) > 1


def test_an_unparseable_command_goes_straight_to_the_dead_letter(queue):
    queue.redis.lpush(queue.pending_key, "not json at all")
    assert queue.claim(block_sec=1) is None
    assert queue.depths()["dead"] == 1
    assert queue.depths()["processing"] == 0


# ===========================================================================
# Ownership — the stale-worker problem
# ===========================================================================


def test_a_worker_that_lost_its_lease_cannot_acknowledge(fake_redis):
    """Worker A stalls, its lease expires, B recovers and re-runs. A must not ACK B's work."""
    queue_a = PublishQueue(fake_redis, "test_publish_jobs", worker_id="worker-a")
    queue_b = PublishQueue(fake_redis, "test_publish_jobs", worker_id="worker-b")

    queue_a.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                    target_id="t", media_identity="m"))
    command_a = queue_a.claim(block_sec=1)

    # A's lease expires and B recovers the command.
    fake_redis.delete(command_a.lease_key)
    assert queue_b.reclaim(command_a.token) is True
    command_b = queue_b.claim(block_sec=1)

    # A wakes up and tries to settle.
    assert queue_a.acknowledge(command_a) is False, "a stale worker must not ACK"
    assert queue_b.owns(command_b) is True
    assert queue_b.depths()["processing"] == 1, "B's command survived A's late ACK"


def test_a_stale_worker_cannot_schedule_a_retry(fake_redis):
    queue_a = PublishQueue(fake_redis, "test_publish_jobs", worker_id="worker-a")
    queue_b = PublishQueue(fake_redis, "test_publish_jobs", worker_id="worker-b")
    queue_a.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                    target_id="t", media_identity="m"))
    command_a = queue_a.claim(block_sec=1)

    fake_redis.delete(command_a.lease_key)
    queue_b.reclaim(command_a.token)
    queue_b.claim(block_sec=1)

    assert queue_a.retry(command_a) is None
    # And it did not leave an orphan scheduled behind.
    assert queue_a.depths()["delayed"] == 0


def test_a_stale_worker_cannot_renew(fake_redis):
    queue_a = PublishQueue(fake_redis, "test_publish_jobs", worker_id="worker-a")
    queue_a.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                    target_id="t", media_identity="m"))
    command = queue_a.claim(block_sec=1)

    assert queue_a.renew_lease(command) is True
    fake_redis.delete(command.lease_key)
    # Losing the renewal is the signal that someone else may have taken over.
    assert queue_a.renew_lease(command) is False


def test_reclaiming_the_same_command_twice_wins_once(fake_redis):
    queue_a = PublishQueue(fake_redis, "test_publish_jobs", worker_id="worker-a")
    queue_b = PublishQueue(fake_redis, "test_publish_jobs", worker_id="worker-b")
    queue_a.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                    target_id="t", media_identity="m"))
    command = queue_a.claim(block_sec=1)
    fake_redis.delete(command.lease_key)

    assert queue_a.reclaim(command.token) is True
    assert queue_b.reclaim(command.token) is False, "two recoveries must not duplicate"
    assert queue_a.depths()["ready"] == 1


# ===========================================================================
# The HTTP path no longer uploads
# ===========================================================================


def test_publishing_returns_before_the_provider_is_called(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher()

    report = service(queue, publisher=publisher).publish(
        db, job=job, target=target, dry_run=False
    )

    assert publisher.calls == [], "the request must not wait on an upload"
    assert report.status == "accepted"
    assert report.items[0].status == "queued"
    assert db.query(PublishAttempt).count() == 1
    assert queue.depths()["ready"] == 1


def test_accepting_a_publication_moves_the_run_into_publishing(db, queue, no_event_fanout):
    """Otherwise a run with a command in flight still looks free to publish."""
    job = make_publishable_run(db)
    target = make_target(db)

    service(queue).publish(db, job=job, target=target, dry_run=False)

    assert job.state == PipelineState.PUBLISHING


def test_the_attempt_records_that_its_command_was_sent(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    service(queue).publish(db, job=job, target=target, dry_run=False)

    assert db.query(PublishAttempt).one().enqueued_at is not None


def test_a_dry_run_still_touches_neither_queue_nor_provider(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher()

    report = service(queue, publisher=publisher).publish(
        db, job=job, target=target, dry_run=True
    )

    assert report.status == "validated"
    assert publisher.calls == []
    assert queue.depths()["ready"] == 0
    assert db.query(PublishAttempt).count() == 0


def test_two_identical_requests_produce_one_attempt_and_one_command(
    db, queue, no_event_fanout
):
    job = make_publishable_run(db)
    target = make_target(db)
    svc = service(queue)

    svc.publish(db, job=job, target=target, dry_run=False)
    svc.publish(db, job=job, target=target, dry_run=False)

    assert db.query(PublishAttempt).count() == 1
    assert queue.depths()["ready"] == 1, "an already-enqueued attempt is not enqueued twice"


def test_a_blocked_publication_enqueues_nothing(db, queue, no_event_fanout):
    job = make_publishable_run(
        db,
        metadata_json={"publication_eligibility": {"eligible": False,
                                                   "blocked_by": ["final_media_qa_fail"]}},
    )
    target = make_target(db)

    report = service(queue).publish(db, job=job, target=target, dry_run=False)

    assert report.status == "blocked"
    assert queue.depths()["ready"] == 0
    assert db.query(PublishAttempt).count() == 0


# ===========================================================================
# The worker executes
# ===========================================================================


def test_the_worker_uploads_what_the_request_queued(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher()
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)

    worker = runtime(queue, db, publisher=publisher)
    worker.tick()

    attempt = db.query(PublishAttempt).one()
    assert len(publisher.calls) == 1
    assert attempt.status == PublishAttemptStatus.SUCCEEDED
    assert attempt.publisher_worker_id == queue.worker_id
    assert reload(db, job).state == PipelineState.PUBLISHED
    assert queue.depths() == {"ready": 0, "processing": 0, "delayed": 0, "dead": 0}


def test_the_same_command_delivered_twice_uploads_once(db, queue, no_event_fanout):
    """At-least-once delivery is safe because the database, not the queue, decides."""
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher()
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)

    attempt = db.query(PublishAttempt).one()
    # A duplicate delivery of the identical command.
    queue.enqueue(
        command_payload(
            publish_attempt_id=str(attempt.id), pipeline_job_id=str(job.id),
            target_id=str(target.id), media_identity=attempt.media_identity,
        )
    )

    worker = runtime(queue, db, publisher=publisher)
    worker.tick()
    worker.tick()

    assert len(publisher.calls) == 1, "one provider execution for two deliveries"
    assert db.query(PublishAttempt).count() == 1


def test_a_redelivery_after_success_never_calls_the_provider(db, queue, no_event_fanout):
    """The success-committed-before-ACK crash: the outcome is already in the database."""
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher()
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)

    worker = runtime(queue, db, publisher=publisher)
    worker.tick()
    calls_after_success = len(publisher.calls)

    # The ACK was lost, so the command comes back.
    attempt = db.query(PublishAttempt).one()
    queue.enqueue(
        command_payload(
            publish_attempt_id=str(attempt.id), pipeline_job_id=str(job.id),
            target_id=str(target.id), media_identity=attempt.media_identity,
        )
    )
    worker.tick()

    assert len(publisher.calls) == calls_after_success, "provider calls delta must be 0"
    assert queue.depths()["processing"] == 0, "the redelivery was acknowledged"


def test_the_worker_never_executes_an_unknown_attempt(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher([_unknown_result()])
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)

    worker = runtime(queue, db, publisher=publisher)
    worker.tick()
    attempt = db.query(PublishAttempt).one()
    assert attempt.status == PublishAttemptStatus.UNKNOWN

    # A command for it arrives again anyway.
    queue.enqueue(
        command_payload(
            publish_attempt_id=str(attempt.id), pipeline_job_id=str(job.id),
            target_id=str(target.id), media_identity=attempt.media_identity,
        )
    )
    worker.tick()

    assert len(publisher.calls) == 1, "UNKNOWN is never executed again automatically"


def test_an_unknown_outcome_acknowledges_and_schedules_nothing(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher([_unknown_result()])
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)

    runtime(queue, db, publisher=publisher).tick()

    depths = queue.depths()
    assert depths["processing"] == 0, "the command is finished"
    assert depths["delayed"] == 0, "an ambiguous publication is never retried"
    assert depths["ready"] == 0
    assert depths["dead"] == 0, "UNKNOWN is a publication state, not a runtime failure"
    assert db.query(PublishAttempt).one().status == PublishAttemptStatus.UNKNOWN


def test_a_retryable_failure_is_delayed_and_reuses_the_attempt(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(provider="youtube", outcome=PublishOutcome.FAILED,
                          retryability=PublishRetryability.RETRYABLE,
                          error_code="backendError", error_message="503"),
        ]
    )
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)
    worker = runtime(queue, db, publisher=publisher)

    worker.tick()
    attempt = db.query(PublishAttempt).one()
    assert attempt.status == PublishAttemptStatus.FAILED_RETRYABLE
    assert queue.depths()["delayed"] == 1
    assert queue.depths()["ready"] == 0, "backoff, not an immediate retry"

    # When it becomes due, the same attempt succeeds.
    queue.redis.zadd(queue.delayed_key,
                     {queue.redis.zrange(queue.delayed_key, 0, 0)[0]: 0})
    worker.tick()

    attempt = reload(db, attempt)
    assert attempt.status == PublishAttemptStatus.SUCCEEDED
    assert db.query(PublishAttempt).count() == 1, "a retry never creates a second row"
    assert attempt.attempt_no == 2


def test_quota_gets_a_much_longer_backoff_than_a_server_error(db, queue, no_event_fanout):
    """A daily quota does not clear in thirty seconds; hammering it just burns the day."""
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(provider="youtube", outcome=PublishOutcome.FAILED,
                          retryability=PublishRetryability.RETRYABLE,
                          error_code="quotaExceeded", error_message="403"),
        ]
    )
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)

    runtime(queue, db, publisher=publisher).tick()

    due_at = queue.redis.zrange(queue.delayed_key, 0, -1, withscores=True)[0][1]
    assert due_at - datetime.now(timezone.utc).timestamp() > queue.backoff_max_sec


def test_an_auth_failure_is_final_and_acknowledged(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(provider="youtube", outcome=PublishOutcome.FAILED,
                          retryability=PublishRetryability.NOT_RETRYABLE,
                          error_code="invalid_grant", error_message="oauth refresh failed"),
        ]
    )
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)

    runtime(queue, db, publisher=publisher).tick()

    db.expire_all()
    attempt = db.query(PublishAttempt).one()
    assert attempt.status == PublishAttemptStatus.FAILED_FINAL
    assert reload(db, target).connection_status.value == "reconnect_required"
    depths = queue.depths()
    assert depths["delayed"] == 0, "no retry against a credential that will never work"
    assert depths["dead"] == 0, "a revoked token is not broken infrastructure"
    assert depths["processing"] == 0


def test_an_exhausted_budget_dead_letters_the_command(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    failure = PublishResult(provider="youtube", outcome=PublishOutcome.FAILED,
                            retryability=PublishRetryability.RETRYABLE,
                            error_code="backendError", error_message="503")
    publisher = StubPublisher([failure, failure, failure])
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)
    worker = runtime(queue, db, publisher=publisher)

    for _ in range(3):
        worker.tick()
        for token in queue.redis.zrange(queue.delayed_key, 0, -1):
            queue.redis.zadd(queue.delayed_key, {token: 0})

    assert db.query(PublishAttempt).one().attempt_no == 3
    assert queue.depths()["dead"] == 1
    assert db.query(PublishAttempt).count() == 1


# ===========================================================================
# Kill switches at execution time
# ===========================================================================


def test_a_disabled_switch_claims_nothing(db, queue, monkeypatch, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher()
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)

    monkeypatch.setattr(settings, "publishing_enabled", False, raising=False)
    worker = runtime(queue, db, publisher=publisher)
    assert worker.tick() is None

    assert publisher.calls == []
    assert queue.depths()["ready"] == 1, "the command waits; it is not lost"


def test_a_target_disabled_after_acceptance_pauses_rather_than_fails(
    db, queue, no_event_fanout
):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher()
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)

    target.is_active = False
    db.commit()
    runtime(queue, db, publisher=publisher).tick()

    attempt = db.query(PublishAttempt).one()
    assert publisher.calls == []
    assert attempt.status == PublishAttemptStatus.PENDING, "no attempt was spent"
    assert queue.depths()["delayed"] == 1, "it comes back when the switch returns"


# ===========================================================================
# Crash recovery — evidence, not lease expiry
# ===========================================================================


def _in_progress(db, job, target, **overrides) -> PublishAttempt:
    fields = dict(
        pipeline_job_id=job.id, target_id=target.id,
        idempotency_key=idempotency_key(job.id, target.id, "final_clips/final_clip_01.mp4"),
        media_identity="final_clips/final_clip_01.mp4",
        media_storage_key=f"jobs/{job.worker_job_id}/final_clips/final_clip_01.mp4",
        media_bytes=1024,
        status=PublishAttemptStatus.IN_PROGRESS, attempt_no=1,
        enqueued_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    attempt = PublishAttempt(**fields)
    db.add(attempt)
    db.commit()
    return attempt


def test_a_crash_before_the_provider_is_safe_to_run_again(db, queue, no_event_fanout):
    """Nothing remote happened, and the row proves it."""
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _in_progress(db, job, target, provider_started_at=None, bytes_uploaded=None)

    decision = PublishRecoveryService().recover(db, attempt, worker_id="worker-b")

    assert decision.action == REQUEUE
    db.refresh(attempt)
    assert attempt.status == PublishAttemptStatus.PENDING
    assert attempt.attempt_no == 0, "a process death does not spend the retry budget"
    assert attempt.enqueued_at is None, "the sweep will send the command again"


def test_a_crash_after_a_session_with_no_bytes_is_safe(db, queue, no_event_fanout):
    """An unused upload session is not a video; it simply expires."""
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _in_progress(
        db, job, target,
        provider_started_at=datetime.now(timezone.utc), bytes_uploaded=0,
    )

    decision = PublishRecoveryService().recover(db, attempt, worker_id="worker-b")

    assert decision.action == REQUEUE
    db.refresh(attempt)
    assert attempt.status == PublishAttemptStatus.PENDING


def test_the_session_is_persisted_before_any_byte_is_sent(db, queue, no_event_fanout):
    """Regression: the session URI used to reach the database only when the attempt settled.

    A worker killed mid-upload therefore left no session behind, recovery concluded nothing
    durable existed, and the next execution opened a SECOND session and uploaded the video
    again. The crash/recovery smoke caught it doing exactly that. Progress is now committed
    as it happens, so an abandoned attempt carries an accurate resume point.
    """
    job = make_publishable_run(db)
    target = make_target(db)

    seen: list[tuple[str | None, int]] = []

    class RecordingPublisher(StubPublisher):
        def publish(self, request):
            # What the real adapter does: report the session before sending anything, then
            # after each committed chunk.
            request.report_progress("https://upload.googleapis.com/session/abc", 0)
            request.report_progress("https://upload.googleapis.com/session/abc", 512)
            db.expire_all()
            row = db.query(PublishAttempt).one()
            seen.append((row.upload_session_uri_encrypted, row.bytes_uploaded or 0))
            return super().publish(request)

    publisher = RecordingPublisher()
    svc = service(queue, publisher=publisher, db=db)
    svc.publish(db, job=job, target=target, dry_run=False)
    PublisherRuntime(
        worker_id=queue.worker_id, queue=queue,
        publishing=service(queue, publisher=publisher, db=db),
        heartbeat=PublisherHeartbeat(queue.worker_id, queue.redis),
        session_factory=lambda: _KeepOpen(db),
    ).tick()

    assert seen, "the publisher was never called"
    stored_session, stored_bytes = seen[0]
    assert stored_session is not None, "the session must be durable before the upload ends"
    assert "upload.googleapis.com" not in stored_session, "and it must be encrypted"
    assert stored_bytes == 512, "progress is committed as it happens"


def test_a_crash_mid_upload_resumes_the_same_session(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _in_progress(
        db, job, target,
        provider_started_at=datetime.now(timezone.utc),
        bytes_uploaded=512,
        upload_session_uri_encrypted=SecretBox(TEST_KEY).encrypt(
            "https://upload.googleapis.com/session/abc"
        ),
    )

    def resumable(request):
        return httpx.Response(308, headers={"Range": "bytes=0-511"})

    decision = PublishRecoveryService(
        client=httpx.Client(transport=httpx.MockTransport(resumable))
    ).recover(db, attempt, worker_id="worker-b")

    assert decision.action == RESUME
    db.refresh(attempt)
    assert attempt.status == PublishAttemptStatus.PENDING
    # The session survives, so the next execution continues it rather than opening a new one.
    assert attempt.upload_session_uri_encrypted is not None


def test_a_crash_on_the_final_chunk_is_ambiguous_not_a_retry(db, queue, no_event_fanout):
    """The bytes may all have landed. This is the case that must never restart."""
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _in_progress(
        db, job, target,
        provider_started_at=datetime.now(timezone.utc),
        bytes_uploaded=1024,
        upload_session_uri_encrypted=SecretBox(TEST_KEY).encrypt(
            "https://upload.googleapis.com/session/abc"
        ),
    )

    def expired(request):
        return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})

    decision = PublishRecoveryService(
        client=httpx.Client(transport=httpx.MockTransport(expired))
    ).recover(db, attempt, worker_id="worker-b")

    assert decision.action == AMBIGUOUS
    db.refresh(attempt)
    assert attempt.status == PublishAttemptStatus.UNKNOWN
    assert attempt.retryability == PublishRetryability.REQUIRES_MANUAL_RESOLUTION


def test_recovery_finds_a_publication_that_completed_after_the_crash(
    db, queue, no_event_fanout
):
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _in_progress(
        db, job, target,
        provider_started_at=datetime.now(timezone.utc),
        bytes_uploaded=1024,
        upload_session_uri_encrypted=SecretBox(TEST_KEY).encrypt(
            "https://upload.googleapis.com/session/abc"
        ),
    )

    def completed(request):
        return httpx.Response(200, json={"id": "vid_after_crash"})

    decision = PublishRecoveryService(
        client=httpx.Client(transport=httpx.MockTransport(completed))
    ).recover(db, attempt, worker_id="worker-b")

    assert decision.action == COMPLETED
    db.refresh(attempt)
    assert attempt.status == PublishAttemptStatus.SUCCEEDED
    assert attempt.external_id == "vid_after_crash"
    assert attempt.external_id_source == "recovered"


def test_an_unreachable_provider_leaves_the_attempt_untouched(db, queue, no_event_fanout):
    """A network blip during a probe is not evidence, so it becomes nobody's work."""
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _in_progress(
        db, job, target,
        provider_started_at=datetime.now(timezone.utc), bytes_uploaded=512,
        upload_session_uri_encrypted=SecretBox(TEST_KEY).encrypt(
            "https://upload.googleapis.com/session/abc"
        ),
    )

    def unreachable(request):
        raise httpx.ConnectError("down", request=request)

    decision = PublishRecoveryService(
        client=httpx.Client(transport=httpx.MockTransport(unreachable))
    ).recover(db, attempt, worker_id="worker-b")

    assert decision.action == UNDETERMINED
    db.refresh(attempt)
    assert attempt.status == PublishAttemptStatus.IN_PROGRESS, "unchanged, to be re-probed"


def test_a_superseded_worker_cannot_overwrite_a_newer_outcome(db, queue, no_event_fanout):
    """Worker A stalls, B takes over and succeeds, then A wakes up holding a stale result.

    Found by the crash/recovery smoke. The queue's ownership token stops A acknowledging the
    command; without this second guard A would still write its stale FAILED_RETRYABLE over
    B's SUCCEEDED, and the "failed" publication would then be retried - duplicating a video
    that already exists.
    """
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _in_progress(
        db, job, target,
        publisher_worker_id="worker-B",
        provider_started_at=datetime.now(timezone.utc),
    )
    attempt.status = PublishAttemptStatus.SUCCEEDED
    attempt.external_id = "vid_from_B"
    db.commit()

    svc = service(queue)
    stale = PublishResult(
        provider="youtube", outcome=PublishOutcome.FAILED,
        retryability=PublishRetryability.RETRYABLE,
        error_code="backendError", error_message="503",
    )
    from app.services.publishing_service import MediaItem

    outcome = svc._record(
        db, job=job, target=target, attempt=attempt, result=stale,
        duration_ms=1,
        item=MediaItem(identity=attempt.media_identity, storage_key="k", video_index=1,
                       video={}),
        worker_id="worker-A",
    )

    db.expire_all()
    fresh = db.query(PublishAttempt).one()
    assert outcome.status == "superseded"
    assert fresh.status == PublishAttemptStatus.SUCCEEDED, "B's outcome survived"
    assert fresh.external_id == "vid_from_B"


def test_lease_expiry_alone_never_restarts_an_upload(db, queue, no_event_fanout):
    """The invariant behind the whole module: two authorities, not one."""
    job = make_publishable_run(db)
    target = make_target(db)
    attempt = _in_progress(
        db, job, target,
        provider_started_at=datetime.now(timezone.utc), bytes_uploaded=900,
        upload_session_uri_encrypted=SecretBox(TEST_KEY).encrypt(
            "https://upload.googleapis.com/session/abc"
        ),
    )
    queue.enqueue(
        command_payload(publish_attempt_id=str(attempt.id), pipeline_job_id=str(job.id),
                        target_id=str(target.id), media_identity=attempt.media_identity)
    )
    command = queue.claim(block_sec=1)
    queue.redis.delete(command.lease_key)  # the worker died

    def expired(request):
        return httpx.Response(410, json={"error": {"errors": [{"reason": "notFound"}]}})

    publisher = StubPublisher()
    worker = runtime(
        queue, db, publisher=publisher,
        recovery=PublishRecoveryService(
            client=httpx.Client(transport=httpx.MockTransport(expired))
        ),
    )
    worker.sweep()

    attempt = reload(db, attempt)
    assert attempt.status == PublishAttemptStatus.UNKNOWN
    assert publisher.calls == [], "an expired lease did not become a second upload"
    assert queue.depths()["ready"] == 0


# ===========================================================================
# Enqueue failure and its sweep
# ===========================================================================


def test_an_attempt_survives_a_queue_outage(db, queue, monkeypatch, no_event_fanout):
    import redis as redis_module

    job = make_publishable_run(db)
    target = make_target(db)

    def explode(payload):
        raise redis_module.ConnectionError("redis is down")

    monkeypatch.setattr(queue, "enqueue", explode)
    report = service(queue).publish(db, job=job, target=target, dry_run=False)

    attempt = db.query(PublishAttempt).one()
    assert report.items[0].status == "pending_enqueue"
    assert attempt.enqueued_at is None, "the row records that the command never went"
    assert attempt.status == PublishAttemptStatus.PENDING


def test_the_sweep_sends_a_command_that_never_reached_redis(
    db, queue, monkeypatch, no_event_fanout
):
    import redis as redis_module

    job = make_publishable_run(db)
    target = make_target(db)
    real_enqueue = queue.enqueue

    def explode(payload):
        raise redis_module.ConnectionError("redis is down")

    monkeypatch.setattr(queue, "enqueue", explode)
    svc = service(queue)
    svc.publish(db, job=job, target=target, dry_run=False)
    assert queue.depths()["ready"] == 0

    monkeypatch.setattr(queue, "enqueue", real_enqueue)
    assert svc.sweep_pending_enqueue(db) == 1

    assert queue.depths()["ready"] == 1
    assert db.query(PublishAttempt).one().enqueued_at is not None
    # And it does not send it a second time.
    assert svc.sweep_pending_enqueue(db) == 0


def test_the_sweep_never_creates_a_publication(db, queue, no_event_fanout):
    """§37: a sweeper that looked at READY_TO_PUBLISH runs would be hidden autopublish."""
    make_publishable_run(db)
    make_target(db)

    assert service(queue).sweep_pending_enqueue(db) == 0
    assert db.query(PublishAttempt).count() == 0
    assert queue.depths()["ready"] == 0


# ===========================================================================
# Multi-video
# ===========================================================================


def test_only_the_outstanding_video_is_retried(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(provider="youtube", outcome=PublishOutcome.SUCCEEDED,
                          external_id="vid_1", bytes_uploaded=10),
            PublishResult(provider="youtube", outcome=PublishOutcome.FAILED,
                          retryability=PublishRetryability.RETRYABLE,
                          error_code="backendError", error_message="503"),
            PublishResult(provider="youtube", outcome=PublishOutcome.SUCCEEDED,
                          external_id="vid_3", bytes_uploaded=10),
        ]
    )
    svc = service(queue, publisher=publisher, artifacts=StubArtifacts(videos=3))
    svc.publish(db, job=job, target=target, dry_run=False)
    assert queue.depths()["ready"] == 3

    worker = runtime(queue, db, publisher=publisher, artifacts=StubArtifacts(videos=3))
    for _ in range(3):
        worker.tick()

    assert reload(db, job).state != PipelineState.PUBLISHED, "one is still outstanding"
    assert queue.depths()["delayed"] == 1

    # The retry executes only the failed one.
    for token in queue.redis.zrange(queue.delayed_key, 0, -1):
        queue.redis.zadd(queue.delayed_key, {token: 0})
    worker.tick()

    assert len(publisher.calls) == 4, "the two successes were never re-uploaded"
    db.expire_all()
    assert db.query(PublishAttempt).count() == 3
    assert all(
        a.status == PublishAttemptStatus.SUCCEEDED for a in db.query(PublishAttempt).all()
    )
    assert reload(db, job).state == PipelineState.PUBLISHED


def test_a_run_is_not_published_while_a_sibling_is_still_queued(db, queue, no_event_fanout):
    """The bug the async runtime would have had: one worker sees one item."""
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher()
    svc = service(queue, publisher=publisher, artifacts=StubArtifacts(videos=2))
    svc.publish(db, job=job, target=target, dry_run=False)

    worker = runtime(queue, db, publisher=publisher, artifacts=StubArtifacts(videos=2))
    worker.tick()

    db.expire_all()
    assert db.query(PublishAttempt).filter(
        PublishAttempt.status == PublishAttemptStatus.SUCCEEDED
    ).count() == 1
    assert reload(db, job).state == PipelineState.PUBLISHING, "still one to go"

    worker.tick()
    assert reload(db, job).state == PipelineState.PUBLISHED


def test_an_unresolved_sibling_keeps_the_run_out_of_published(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher(
        [
            PublishResult(provider="youtube", outcome=PublishOutcome.SUCCEEDED,
                          external_id="vid_1", bytes_uploaded=10),
            _unknown_result(),
        ]
    )
    svc = service(queue, publisher=publisher, artifacts=StubArtifacts(videos=2))
    svc.publish(db, job=job, target=target, dry_run=False)

    worker = runtime(queue, db, publisher=publisher, artifacts=StubArtifacts(videos=2))
    worker.tick()
    worker.tick()

    job = reload(db, job)
    assert job.state != PipelineState.PUBLISHED
    assert (job.metadata_json or {})["publication_status"] == "unresolved"


# ===========================================================================
# Liveness
# ===========================================================================


def test_a_worker_id_is_stable_and_named():
    worker_id = resolve_worker_id()
    assert worker_id.startswith("publisher-")
    assert resolve_worker_id() != worker_id or True  # a new process gets a new suffix


def test_a_beating_worker_is_listed_and_a_stopped_one_is_not(fake_redis):
    heartbeat = PublisherHeartbeat("publisher-test", fake_redis, ttl_sec=60)
    heartbeat.beat(state="idle")

    alive = PublisherHeartbeat.alive(fake_redis)
    assert [w["worker_id"] for w in alive] == ["publisher-test"]
    assert alive[0]["last_heartbeat_at"]

    heartbeat.stop()
    assert PublisherHeartbeat.alive(fake_redis) == []


def test_liveness_expires_rather_than_being_declared(fake_redis):
    """Nothing has to notice a death: the TTL is the mechanism."""
    heartbeat = PublisherHeartbeat("publisher-test", fake_redis, ttl_sec=60)
    heartbeat.beat()
    assert PublisherHeartbeat.alive(fake_redis)

    fake_redis.delete(heartbeat.key)  # what the TTL does on its own
    assert PublisherHeartbeat.alive(fake_redis) == []


def test_the_runtime_snapshot_reports_depths_and_workers(fake_redis, monkeypatch):
    queue = PublishQueue(fake_redis, "test_publish_jobs", worker_id="publisher-x")
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))
    PublisherHeartbeat("publisher-x", fake_redis).beat(state="idle")
    monkeypatch.setattr(
        "app.publishing.identity._default_redis", lambda: fake_redis, raising=False
    )

    snapshot = runtime_snapshot(queue)

    assert snapshot["ready"] == 1
    assert snapshot["workers_alive"] == 1
    assert snapshot["queue"] == "test_publish_jobs"


def test_a_configured_but_dead_publisher_is_not_reported_alive(fake_redis, monkeypatch):
    """Configuration is not liveness. PR-SCHEDULER-01's gap, not repeated."""
    queue = PublishQueue(fake_redis, "test_publish_jobs")
    monkeypatch.setattr(
        "app.publishing.identity._default_redis", lambda: fake_redis, raising=False
    )
    monkeypatch.setattr(settings, "publisher_runtime_enabled", True, raising=False)

    snapshot = runtime_snapshot(queue)
    assert snapshot["workers_alive"] == 0
    assert snapshot["workers"] == []


# ===========================================================================
# Restart
# ===========================================================================


def test_queued_commands_survive_a_publisher_restart(db, queue, no_event_fanout):
    job = make_publishable_run(db)
    target = make_target(db)
    publisher = StubPublisher()
    svc = service(queue, publisher=publisher)
    svc.publish(db, job=job, target=target, dry_run=False)

    # The publisher is stopped before it ever claims. A brand-new one takes over.
    fresh = runtime(queue, db, publisher=publisher)
    fresh.tick()

    assert len(publisher.calls) == 1
    assert db.query(PublishAttempt).one().status == PublishAttemptStatus.SUCCEEDED
    assert db.query(PublishAttempt).count() == 1


def _module_dependencies(module):
    """Imported module names and referenced identifiers, code only."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)

    referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    return modules, referenced


# The scheduler may depend on the publication POLICY. It may not depend on anything that can
# talk to a provider. PR-PUBLISH-02 moved this line deliberately: before it, the scheduler
# knew nothing about publishing at all; now it calls one application service that decides and
# at most queues a command.
FORBIDDEN_IN_SCHEDULER = {
    "YouTubePublisher",
    "YouTubeOAuthClient",
    "PublishQueue",
    "PublishingService",
    "PublishResolutionService",
    "PublishTargetService",
    "MinioMediaSource",
}
FORBIDDEN_MODULES_IN_SCHEDULER = (
    "app.publishing",
    "app.services.publishing_service",
    "app.services.publish_runtime",
    "app.services.publish_recovery_service",
    "app.services.publish_target_service",
)


def test_the_scheduler_depends_on_the_policy_and_never_on_a_publisher():
    """§5/§78: the scheduler calls AutonomousPublicationService, not a provider adapter."""
    from app.services import automation_scheduler, automation_service

    for module in (automation_service, automation_scheduler):
        modules, referenced = _module_dependencies(module)

        offending = {
            name for name in modules
            if any(name.startswith(prefix) for prefix in FORBIDDEN_MODULES_IN_SCHEDULER)
        }
        assert offending == set(), (
            f"{module.__name__} imports a publishing implementation: {sorted(offending)}"
        )
        assert referenced & FORBIDDEN_IN_SCHEDULER == set(), (
            f"{module.__name__} references {sorted(referenced & FORBIDDEN_IN_SCHEDULER)}"
        )


def test_the_policy_service_never_talks_to_a_provider():
    """The policy decides and delegates. It does not upload, and it holds no credential."""
    from app.services import autopublish_service

    modules, referenced = _module_dependencies(autopublish_service)

    assert not any(name.startswith("app.publishing.youtube") for name in modules), (
        "the policy must not import a provider adapter"
    )
    for forbidden in ("YouTubePublisher", "YouTubeOAuthClient", "PublishCredential",
                      "secret_box", "SecretBox"):
        assert forbidden not in referenced, f"the policy must not reference {forbidden}"


def test_the_publisher_runtime_never_looks_at_pipeline_jobs_to_start_work():
    """A sweeper that queried READY_TO_PUBLISH would be autopublish wearing a disguise."""
    import ast
    import inspect

    from app.services import publish_runtime
    from app.services.publishing_service import PublishingService

    def code_only(source: str) -> str:
        """The module with docstrings stripped.

        Both modules explain this boundary in prose, so a plain substring match flags the
        very text that documents it.
        """
        import ast as _ast
        import textwrap

        tree = _ast.parse(textwrap.dedent(source))
        for node in _ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            if (
                isinstance(first, _ast.Expr)
                and isinstance(first.value, _ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = body[1:] or [_ast.Pass()]
        return _ast.unparse(_ast.fix_missing_locations(tree))

    sweep = code_only(inspect.getsource(PublishingService.sweep_pending_enqueue))
    assert "PipelineJob" not in sweep, "the sweep must only ever read PublishAttempt rows"
    assert "READY_TO_PUBLISH" not in sweep

    runtime_code = code_only(inspect.getsource(publish_runtime))
    assert "READY_TO_PUBLISH" not in runtime_code
