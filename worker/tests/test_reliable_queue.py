"""Reliable queue invariants (PR-RUNTIME-01).

The property under test throughout: **a job cannot disappear**. It is in exactly one of
pending / delayed / processing / dead at any moment, and it leaves `processing` only through
an explicit acknowledge, retry or dead-letter.
"""

import json

import fakeredis
import pytest

from app.runtime.reliable_queue import ReliableQueue


QUEUE = "test_jobs"


@pytest.fixture
def redis_client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture
def queue(redis_client):
    return ReliableQueue(
        redis_client,
        QUEUE,
        worker_id="worker-test",
        max_attempts=3,
        visibility_timeout_sec=60,
        backoff_base_sec=10,
        backoff_max_sec=100,
    )


def payload(**overrides):
    base = {
        "job_id": "job-abc",
        "video_url": "https://example.com/v",
        "pipeline_stage": "prepare",
    }
    base.update(overrides)
    return base


# ==========================================================================
# Success: queued → processing → acknowledged
# ==========================================================================


def test_claim_moves_the_job_from_pending_to_processing(queue, redis_client):
    queue.enqueue(payload())

    assert queue.depths() == {"pending": 1, "processing": 0, "delayed": 0, "dead": 0}

    job = queue.claim(block_sec=1)

    assert job is not None
    assert job.job_id == "job-abc"
    assert queue.depths() == {"pending": 0, "processing": 1, "delayed": 0, "dead": 0}
    # The payload is still recoverable: it lives in processing, not in the worker's memory.
    assert redis_client.lrange(queue.processing_key, 0, -1) == [job.token]


def test_claim_creates_a_lease(queue, redis_client):
    queue.enqueue(payload())
    job = queue.claim(block_sec=1)

    assert redis_client.exists(job.lease_key)
    lease = json.loads(redis_client.get(job.lease_key))
    assert lease["worker_id"] == "worker-test"
    assert lease["job_id"] == "job-abc"
    assert 0 < redis_client.ttl(job.lease_key) <= 60


def test_acknowledge_removes_the_job_and_its_lease(queue, redis_client):
    queue.enqueue(payload())
    job = queue.claim(block_sec=1)

    queue.acknowledge(job)

    assert queue.depths() == {"pending": 0, "processing": 0, "delayed": 0, "dead": 0}
    assert not redis_client.exists(job.lease_key)


def test_claim_returns_none_when_the_queue_is_empty(queue):
    assert queue.claim(block_sec=1) is None


def test_claim_is_fifo(queue):
    queue.enqueue(payload(job_id="first"))
    queue.enqueue(payload(job_id="second"))

    assert queue.claim(block_sec=1).job_id == "first"
    assert queue.claim(block_sec=1).job_id == "second"


# ==========================================================================
# Recoverable failure: retry with backoff
# ==========================================================================


def test_retry_removes_from_processing_and_schedules_a_delayed_job(queue, redis_client):
    queue.enqueue(payload())
    job = queue.claim(block_sec=1)

    delay = queue.retry(job, RuntimeError("boom"))

    assert delay == 10  # base 10 * 2**0
    assert queue.depths() == {"pending": 0, "processing": 0, "delayed": 1, "dead": 0}
    assert not redis_client.exists(job.lease_key)


def test_retry_increments_the_attempt_counter(queue):
    queue.enqueue(payload())
    job = queue.claim(block_sec=1)
    assert job.attempt == 1

    queue.retry(job, RuntimeError("boom"))
    queue.promote_due_delayed(now=1e12)

    retried = queue.claim(block_sec=1)
    assert retried.attempt == 2
    assert retried.max_attempts == 3
    assert retried.job_id == "job-abc"


def test_delayed_jobs_are_not_claimable_before_they_are_due(queue):
    queue.enqueue(payload())
    job = queue.claim(block_sec=1)
    queue.retry(job, RuntimeError("boom"))

    # Nothing promoted: the backoff has not elapsed, and the worker does not busy-loop.
    assert queue.claim(block_sec=1) is None
    assert queue.depths()["delayed"] == 1


def test_backoff_grows_and_is_capped(queue):
    assert queue.backoff_delay(1) == 10
    assert queue.backoff_delay(2) == 20
    assert queue.backoff_delay(3) == 40
    assert queue.backoff_delay(10) == 100  # capped at backoff_max_sec


def test_full_retry_cycle_ends_in_success(queue):
    queue.enqueue(payload())

    job = queue.claim(block_sec=1)
    queue.retry(job, RuntimeError("transient"))
    queue.promote_due_delayed(now=1e12)

    retried = queue.claim(block_sec=1)
    queue.acknowledge(retried)

    assert queue.depths() == {"pending": 0, "processing": 0, "delayed": 0, "dead": 0}


# ==========================================================================
# Retry exhaustion → dead-letter queue
# ==========================================================================


def test_dead_letter_records_failure_metadata(queue, redis_client):
    queue.enqueue(payload(attempt=3))
    job = queue.claim(block_sec=1)

    assert job.is_last_attempt

    queue.dead_letter(job, ValueError("bad payload"), reason="non_retryable_failure")

    assert queue.depths() == {"pending": 0, "processing": 0, "delayed": 0, "dead": 1}

    entry = json.loads(redis_client.lrange(queue.dead_key, 0, -1)[0])
    # Original payload preserved.
    assert entry["job_id"] == "job-abc"
    assert entry["video_url"] == "https://example.com/v"
    failure = entry["_failure"]
    assert failure["error_type"] == "ValueError"
    assert failure["error_message"] == "bad payload"
    assert failure["reason"] == "non_retryable_failure"
    assert failure["worker_id"] == "worker-test"
    assert failure["attempt"] == 3
    assert failure["failed_at"]


def test_is_last_attempt_tracks_max_attempts(queue):
    queue.enqueue(payload(attempt=2, max_attempts=3))
    assert queue.claim(block_sec=1).is_last_attempt is False

    queue.enqueue(payload(attempt=3, max_attempts=3))
    assert queue.claim(block_sec=1).is_last_attempt is True


def test_unparseable_payload_goes_straight_to_the_dead_letter_queue(queue, redis_client):
    redis_client.lpush(queue.pending_key, "this is not json")

    assert queue.claim(block_sec=1) is None
    assert queue.depths()["processing"] == 0
    assert queue.depths()["dead"] == 1

    entry = json.loads(redis_client.lrange(queue.dead_key, 0, -1)[0])
    assert entry["_raw_payload"] == "this is not json"
    assert entry["_failure"]["error_type"] == "InvalidPayload"


# ==========================================================================
# Crash recovery: expired lease
# ==========================================================================


def test_stale_job_is_recovered_when_its_lease_expires(queue, redis_client):
    queue.enqueue(payload())
    job = queue.claim(block_sec=1)

    # Simulate the worker dying: the payload stays in processing, nothing renews the lease.
    redis_client.delete(job.lease_key)

    result = queue.recover_stale()

    assert result == {"recovered": 1, "dead_lettered": 0}
    assert queue.depths() == {"pending": 1, "processing": 0, "delayed": 0, "dead": 0}

    recovered = queue.claim(block_sec=1)
    assert recovered.job_id == "job-abc"
    assert recovered.attempt == 2  # the crashed attempt is counted


def test_recovery_does_not_touch_jobs_with_a_live_lease(queue):
    queue.enqueue(payload())
    job = queue.claim(block_sec=1)

    result = queue.recover_stale()

    assert result == {"recovered": 0, "dead_lettered": 0}
    assert queue.depths()["processing"] == 1
    assert job is not None


def test_stale_job_on_its_last_attempt_is_dead_lettered(queue, redis_client):
    queue.enqueue(payload(attempt=3, max_attempts=3))
    job = queue.claim(block_sec=1)
    redis_client.delete(job.lease_key)

    result = queue.recover_stale()

    assert result == {"recovered": 0, "dead_lettered": 1}
    assert queue.depths() == {"pending": 0, "processing": 0, "delayed": 0, "dead": 1}

    entry = json.loads(redis_client.lrange(queue.dead_key, 0, -1)[0])
    assert entry["_failure"]["reason"] == "lease_expired_attempts_exhausted"


def test_renew_lease_keeps_a_long_job_owned(queue, redis_client):
    queue.enqueue(payload())
    job = queue.claim(block_sec=1)

    redis_client.expire(job.lease_key, 1)
    assert queue.renew_lease(job) is True
    assert redis_client.ttl(job.lease_key) > 1

    # Still owned, so recovery leaves it alone.
    assert queue.recover_stale() == {"recovered": 0, "dead_lettered": 0}


def test_no_job_is_lost_across_a_crash_and_recovery(queue, redis_client):
    """The headline invariant, end to end."""
    queue.enqueue(payload(job_id="survivor"))

    job = queue.claim(block_sec=1)
    redis_client.delete(job.lease_key)  # worker dies mid-processing

    queue.recover_stale()
    recovered = queue.claim(block_sec=1)
    queue.acknowledge(recovered)

    assert recovered.job_id == "survivor"
    assert sum(queue.depths().values()) == 0


# ==========================================================================
# Backward compatibility with pre-PR payloads
# ==========================================================================


def test_payload_without_attempt_fields_is_processable(queue, redis_client):
    legacy = {
        "video_url": "https://example.com/legacy",
        "job_id": "legacy-job",
        "pipeline_stage": "prepare",
        "manual_response": None,
        "clip_mode": "short_serie",
        "video_ratio": "portrait",
        "job_preset": "short_series",
        "build_ia": True,
    }
    # Exactly what the old producers LPUSHed: no attempt, no max_attempts.
    redis_client.lpush(queue.pending_key, json.dumps(legacy))

    job = queue.claim(block_sec=1)

    assert job is not None
    assert job.job_id == "legacy-job"
    assert job.attempt == 1
    assert job.max_attempts == 3
    assert job.payload["build_ia"] is True
    queue.acknowledge(job)
    assert sum(queue.depths().values()) == 0


def test_legacy_payload_gains_attempt_fields_on_retry(queue, redis_client):
    redis_client.lpush(
        queue.pending_key,
        json.dumps({"job_id": "legacy-job", "video_url": "u", "pipeline_stage": "prepare"}),
    )
    job = queue.claim(block_sec=1)
    queue.retry(job, RuntimeError("boom"))
    queue.promote_due_delayed(now=1e12)

    retried = queue.claim(block_sec=1)
    assert retried.attempt == 2
    assert retried.max_attempts == 3
    assert retried.payload["video_url"] == "u"


@pytest.mark.parametrize("bad", [None, "", 0, -1, "abc", {}])
def test_malformed_attempt_values_fall_back_to_defaults(queue, redis_client, bad):
    redis_client.lpush(
        queue.pending_key,
        json.dumps({"job_id": "j", "attempt": bad, "max_attempts": bad}),
    )
    job = queue.claim(block_sec=1)
    assert job.attempt == 1
    assert job.max_attempts == 3
