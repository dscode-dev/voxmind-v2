"""Worker heartbeat and identity (PR-RUNTIME-01)."""

import json

import fakeredis
import pytest

from app.runtime.heartbeat import STATUS_BUSY, STATUS_IDLE, WorkerHeartbeat, list_workers
from app.runtime.identity import WORKER_ID, worker_id
from app.runtime.reliable_queue import ClaimedJob, ReliableQueue


@pytest.fixture
def redis_client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture
def heartbeat(redis_client):
    return WorkerHeartbeat(
        redis_client,
        "worker-1",
        interval_sec=1,
        ttl_sec=30,
    )


def make_job(job_id="job-1", attempt=1):
    return ClaimedJob(
        token=json.dumps({"job_id": job_id}),
        payload={"job_id": job_id},
        attempt=attempt,
        max_attempts=3,
        lease_key="lease-key",
    )


# ==========================================================================
# Identity
# ==========================================================================


def test_worker_id_is_stable_within_the_process():
    assert worker_id() == WORKER_ID
    assert worker_id() == worker_id()
    assert WORKER_ID.startswith("worker-")


# ==========================================================================
# Heartbeat
# ==========================================================================


def test_heartbeat_is_created_with_a_ttl(heartbeat, redis_client):
    heartbeat.publish()

    assert redis_client.exists(heartbeat.key)
    assert 0 < redis_client.ttl(heartbeat.key) <= 30

    payload = json.loads(redis_client.get(heartbeat.key))
    assert payload["worker_id"] == "worker-1"
    assert payload["status"] == STATUS_IDLE
    assert payload["last_seen_at"]


def test_busy_state_includes_the_current_job(heartbeat, redis_client):
    heartbeat.mark_busy(make_job("job-42", attempt=2))

    payload = json.loads(redis_client.get(heartbeat.key))
    assert payload["status"] == STATUS_BUSY
    assert payload["job_id"] == "job-42"
    assert payload["attempt"] == 2
    assert payload["started_at"]


def test_idle_state_clears_the_current_job(heartbeat, redis_client):
    heartbeat.mark_busy(make_job("job-42"))
    heartbeat.mark_idle()

    payload = json.loads(redis_client.get(heartbeat.key))
    assert payload["status"] == STATUS_IDLE
    assert payload["job_id"] is None
    assert payload["attempt"] is None
    assert payload["started_at"] is None


def test_publishing_refreshes_the_ttl(heartbeat, redis_client):
    heartbeat.publish()
    redis_client.expire(heartbeat.key, 2)
    assert redis_client.ttl(heartbeat.key) <= 2

    heartbeat.publish()
    assert redis_client.ttl(heartbeat.key) > 2


def test_an_expired_worker_is_distinguishable_from_an_active_one(redis_client):
    alive = WorkerHeartbeat(redis_client, "alive-worker", ttl_sec=30)
    dead = WorkerHeartbeat(redis_client, "dead-worker", ttl_sec=30)
    alive.publish()
    dead.publish()

    assert {w["worker_id"] for w in list_workers(redis_client)} == {
        "alive-worker",
        "dead-worker",
    }

    # A dead worker stops refreshing; its key simply expires.
    redis_client.delete(dead.key)

    remaining = list_workers(redis_client)
    assert [w["worker_id"] for w in remaining] == ["alive-worker"]


def test_clear_removes_the_heartbeat(heartbeat, redis_client):
    heartbeat.publish()
    heartbeat.clear()
    assert not redis_client.exists(heartbeat.key)


def test_heartbeat_renews_the_lease_of_the_running_job(redis_client):
    queue = ReliableQueue(
        redis_client, "hb_jobs", worker_id="worker-1", visibility_timeout_sec=60
    )
    queue.enqueue({"job_id": "job-1"})
    job = queue.claim(block_sec=1)

    heartbeat = WorkerHeartbeat(redis_client, "worker-1", ttl_sec=30, queue=queue)
    heartbeat.mark_busy(job)

    redis_client.expire(job.lease_key, 1)
    heartbeat._renew_lease()

    # The lease outlives the crash window precisely because the worker is alive.
    assert redis_client.ttl(job.lease_key) > 1
    assert queue.recover_stale() == {"recovered": 0, "dead_lettered": 0}
