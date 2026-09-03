"""Production admission: idempotency, capacity, snapshot, enqueue failure, concurrency.

The queue is substituted throughout, so "Redis is down" is a thing a test can cause rather
than something only production discovers.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import discovery as discovery_api
from app.api.router import api_router
from app.db.session import get_db
from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.enums import (
    DiscoverySourceKind,
    PipelineState,
    UserRole,
    UserStatus,
    VideoCandidateStatus,
)
from app.models.pipeline_job import PipelineJob
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin
from app.services.admission_service import (
    ACTIVE_STATES,
    ADMITTED,
    ALREADY_ADMITTED,
    CAPACITY_LIMIT,
    ENQUEUE_FAILED,
    HARD_MAX_ACTIVE_JOBS,
    INVALID_STATE,
    PERMANENTLY_BLOCKED,
    TEMPORARILY_BLOCKED,
    AdmissionConfig,
    ProductionAdmissionService,
    admission_key_for,
)
from app.services.work_queue import EnqueueError

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


class FakeQueue:
    """Records what was published, and can be made to fail on demand."""

    def __init__(self, fail: EnqueueError | None = None):
        self.published: list[dict] = []
        self.fail = fail

    def publish(self, payload: dict) -> str:
        if self.fail is not None:
            raise self.fail
        self.published.append(payload)
        return "token"


@pytest.fixture()
def queue():
    return FakeQueue()


@pytest.fixture()
def service(queue):
    return ProductionAdmissionService(queue=queue)


@pytest.fixture()
def topic(db):
    topic = ContentTopic(
        name="Futebol brasileiro",
        keywords_json=["futebol"],
        default_clip_mode="short_serie",
        default_video_ratio="portrait",
        metadata_json={},
    )
    db.add(topic)
    db.flush()
    return topic


@pytest.fixture()
def source(db, topic):
    source = DiscoverySource(
        topic_id=topic.id, kind=DiscoverySourceKind.RSS, is_active=True, config_json={}
    )
    db.add(source)
    db.flush()
    return source


def make_candidate(db, topic, source, name="c0", **overrides):
    normalized = {
        "channel_id": overrides.pop("channel_id", f"UC_{name}"),
        "available": overrides.pop("available", True),
    }
    row = VideoCandidate(
        topic_id=topic.id,
        source_id=source.id,
        external_id=name,
        url=overrides.pop("url", f"https://www.youtube.com/watch?v={name}"),
        title=overrides.pop("title", "Entrevista sobre futebol"),
        channel="Canal",
        published_at=NOW - timedelta(hours=4),
        dedup_hash=f"hash-{name}",
        status=overrides.pop("status", VideoCandidateStatus.SELECTED),
        selected_at=overrides.pop("selected_at", NOW - timedelta(minutes=10)),
        relevance_score=overrides.pop("relevance_score", 0.8),
        scores_json=overrides.pop(
            "scores_json", {"version": "selection-v1", "final_score": 0.82}
        ),
        metadata_json={
            "provider": "youtube",
            "normalized": normalized,
            "selection": {"method": "policy", "selection_run_id": "run-1"},
        },
        **overrides,
    )
    db.add(row)
    db.flush()
    return row


def make_active_run(db, topic, state=PipelineState.DOWNLOADING):
    run = PipelineJob(
        worker_job_id=str(uuid.uuid4()),
        topic_id=topic.id,
        source_url="https://example.invalid/v",
        state=state,
        clip_mode="short_serie",
        video_ratio="portrait",
        pipeline_stage="prepare",
    )
    db.add(run)
    db.flush()
    return run


def runs(db):
    return db.query(PipelineJob).all()


# ==========================================================================
# The happy path
# ==========================================================================


def test_a_selected_candidate_becomes_a_queued_production(db, topic, source, service, queue, no_event_fanout):
    candidate = make_candidate(db, topic, source)

    decision = service.admit_candidate(db, candidate=candidate, now=NOW)

    assert decision.outcome == ADMITTED
    run = db.query(PipelineJob).one()
    assert run.state == PipelineState.QUEUED
    assert str(run.candidate_id) == str(candidate.id)
    assert len(queue.published) == 1
    assert queue.published[0]["pipeline_job_id"] == str(run.id)
    assert queue.published[0]["job_id"] == run.worker_job_id


def test_the_candidate_is_consumed_only_after_the_handoff(db, topic, source, service, no_event_fanout):
    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    db.refresh(candidate)
    assert candidate.status == VideoCandidateStatus.CONSUMED
    assert candidate.metadata_json["production"]["pipeline_job_id"]


def test_enqueued_at_is_recorded_separately_from_queued_at(db, topic, source, service, no_event_fanout):
    """"queued in the database" and "queued in Redis" are different facts."""
    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    run = db.query(PipelineJob).one()
    assert run.queued_at is not None
    assert run.enqueued_at is not None


def test_no_clip_job_is_fabricated(db, topic, source, service, no_event_fanout):
    """ClipJob requires a user, a purchase and a product. An autonomous run has none of
    them, and PipelineJob has no ClipJob FK — so nothing fake is created to satisfy one."""
    from app.models.clip_job import ClipJob

    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    assert db.query(ClipJob).count() == 0


def test_the_payload_stays_small(db, topic, source, service, queue, no_event_fanout):
    """The worker needs the source, the shape and two ids — not the candidate's metadata."""
    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    payload = queue.published[0]
    assert set(payload) == {
        "job_id", "pipeline_job_id", "video_url", "pipeline_stage",
        "clip_mode", "video_ratio", "build_ia", "manual_response", "origin",
    }


# ==========================================================================
# Idempotency
# ==========================================================================


def test_admitting_the_same_candidate_twice_creates_one_run(db, topic, source, service, queue, no_event_fanout):
    candidate = make_candidate(db, topic, source)

    first = service.admit_candidate(db, candidate=candidate, now=NOW)
    second = service.admit_candidate(db, candidate=candidate, now=NOW)

    assert first.outcome == ADMITTED
    assert second.outcome == ALREADY_ADMITTED
    assert second.pipeline_job_id == first.pipeline_job_id
    assert len(runs(db)) == 1
    assert len(queue.published) == 1, "the retry must not enqueue a second message"


def test_the_admission_key_is_deterministic_and_not_time_based():
    """A timestamp key would make every retry a new identity — the opposite of idempotency."""
    candidate_id = uuid.uuid4()
    assert admission_key_for(candidate_id) == admission_key_for(candidate_id)
    assert admission_key_for(candidate_id) != admission_key_for(uuid.uuid4())
    assert admission_key_for(candidate_id) == f"admit:{candidate_id}:v1"


def test_a_different_profile_is_a_different_admission():
    """Re-producing the same source later is possible, but only deliberately."""
    candidate_id = uuid.uuid4()
    assert admission_key_for(candidate_id, "v1") != admission_key_for(candidate_id, "v2")


def test_the_database_refuses_a_duplicate_admission_key(db, topic, source):
    """The constraint, not the service, is what makes concurrent admission safe."""
    from sqlalchemy.exc import IntegrityError

    key = admission_key_for(uuid.uuid4())
    for _ in range(2):
        db.add(PipelineJob(
            worker_job_id=str(uuid.uuid4()), topic_id=topic.id,
            source_url="https://example.invalid/v", state=PipelineState.QUEUED,
            clip_mode="short_serie", video_ratio="portrait", admission_key=key,
        ))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_runs_without_an_admission_key_do_not_collide(db, topic):
    """API, Telegram and scheduler runs have no candidate; they must not collide on NULL."""
    for _ in range(3):
        db.add(PipelineJob(
            worker_job_id=str(uuid.uuid4()), topic_id=topic.id,
            source_url="https://example.invalid/v", state=PipelineState.QUEUED,
            clip_mode="short_serie", video_ratio="portrait",
        ))
    db.flush()
    assert len(runs(db)) == 3


def test_a_race_lost_at_the_constraint_reports_the_winner(db, topic, source, service, queue, monkeypatch, no_event_fanout):
    """The interleaving the constraint exists for: both requests see no run and both insert.

    The pre-check is blinded so the second admission reaches the unique index, which is
    exactly what a concurrent request does.
    """
    candidate = make_candidate(db, topic, source)
    first = service.admit_candidate(db, candidate=candidate, now=NOW)
    db.refresh(candidate)
    candidate.status = VideoCandidateStatus.SELECTED  # pretend the other request had not finished
    db.flush()

    real_query = db.query
    calls = {"n": 0}

    def blinded(*args, **kwargs):
        if args and args[0] is PipelineJob:
            calls["n"] += 1
            if calls["n"] == 1:
                class Blind:
                    def filter(self, *a, **k):
                        return self

                    def first(self):
                        return None

                return Blind()
        return real_query(*args, **kwargs)

    monkeypatch.setattr(db, "query", blinded)
    second = ProductionAdmissionService(queue=queue).admit_candidate(
        db, candidate=candidate, now=NOW
    )
    monkeypatch.undo()

    assert second.outcome == ALREADY_ADMITTED
    assert second.pipeline_job_id == first.pipeline_job_id
    assert len(runs(db)) == 1, "the index collapsed the race, not the service"


def test_a_crashed_admission_repairs_the_candidate_status(db, topic, source, service, no_event_fanout):
    """Enqueue succeeded, then the process died before marking the candidate CONSUMED."""
    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    db.refresh(candidate)
    candidate.status = VideoCandidateStatus.SELECTED  # the lost update
    db.flush()

    decision = service.admit_candidate(db, candidate=candidate, now=NOW)

    assert decision.outcome == ALREADY_ADMITTED
    assert "candidate_status_repaired" in decision.reasons
    db.refresh(candidate)
    assert candidate.status == VideoCandidateStatus.CONSUMED
    assert len(runs(db)) == 1


# ==========================================================================
# Status eligibility
# ==========================================================================


@pytest.mark.parametrize(
    "status,expected",
    [
        (VideoCandidateStatus.DISCOVERED, INVALID_STATE),
        (VideoCandidateStatus.RANKED, INVALID_STATE),
        (VideoCandidateStatus.REJECTED, INVALID_STATE),
        (VideoCandidateStatus.SELECTED, ADMITTED),
        (VideoCandidateStatus.CONSUMED, ALREADY_ADMITTED),
    ],
)
def test_only_a_selected_candidate_can_be_admitted(db, topic, source, service, status, expected, no_event_fanout):
    candidate = make_candidate(db, topic, source, status=status)

    decision = service.admit_candidate(db, candidate=candidate, now=NOW)

    assert decision.outcome == expected


def test_an_unavailable_candidate_is_permanently_blocked(db, topic, source, service, no_event_fanout):
    """Revalidated at admission time: discovery may have run hours ago."""
    candidate = make_candidate(db, topic, source, available=False)

    decision = service.admit_candidate(db, candidate=candidate, now=NOW)

    assert decision.outcome == PERMANENTLY_BLOCKED
    assert "candidate_unavailable" in decision.reasons
    assert runs(db) == []


def test_a_candidate_without_a_url_is_permanently_blocked(db, topic, source, service, no_event_fanout):
    candidate = make_candidate(db, topic, source, url="   ")

    decision = service.admit_candidate(db, candidate=candidate, now=NOW)

    assert decision.outcome == PERMANENTLY_BLOCKED
    assert "missing_source_url" in decision.reasons


def test_a_blocked_candidate_keeps_its_status(db, topic, source, service, no_event_fanout):
    """Admission never rejects a candidate — that is selection's decision to make."""
    candidate = make_candidate(db, topic, source, available=False)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    db.refresh(candidate)
    assert candidate.status == VideoCandidateStatus.SELECTED


# ==========================================================================
# Capacity
# ==========================================================================


def test_active_states_exclude_finished_runs():
    """Counting a finished run as capacity would wedge the system permanently."""
    for state in (
        PipelineState.PUBLISHED, PipelineState.CANCELED, PipelineState.FAILED,
        PipelineState.READY_TO_PUBLISH, PipelineState.REVIEW_REQUIRED,
    ):
        assert state not in ACTIVE_STATES
    for state in (
        PipelineState.QUEUED, PipelineState.DOWNLOADING,
        PipelineState.TRANSCRIBING, PipelineState.RENDERING,
    ):
        assert state in ACTIVE_STATES


def test_capacity_blocks_admission_without_rejecting(db, topic, source, service, no_event_fanout):
    topic.metadata_json = {"admission": {"max_active_jobs": 1}}
    make_active_run(db, topic)
    candidate = make_candidate(db, topic, source)
    db.flush()

    decision = service.admit_candidate(db, candidate=candidate, now=NOW)

    assert decision.outcome == TEMPORARILY_BLOCKED
    assert decision.reasons == [CAPACITY_LIMIT]
    db.refresh(candidate)
    assert candidate.status == VideoCandidateStatus.SELECTED, "still admissible later"


def test_the_same_candidate_is_admitted_once_capacity_frees(db, topic, source, service, no_event_fanout):
    topic.metadata_json = {"admission": {"max_active_jobs": 1}}
    blocker = make_active_run(db, topic)
    candidate = make_candidate(db, topic, source)
    db.flush()

    assert service.admit_candidate(db, candidate=candidate, now=NOW).outcome == TEMPORARILY_BLOCKED

    blocker.state = PipelineState.PUBLISHED
    db.flush()

    assert service.admit_candidate(db, candidate=candidate, now=NOW).outcome == ADMITTED


def test_a_finished_run_does_not_hold_a_slot(db, topic, source, service, no_event_fanout):
    topic.metadata_json = {"admission": {"max_active_jobs": 1}}
    make_active_run(db, topic, state=PipelineState.FAILED)
    candidate = make_candidate(db, topic, source)
    db.flush()

    assert service.admit_candidate(db, candidate=candidate, now=NOW).outcome == ADMITTED


def test_configuration_cannot_exceed_the_server_ceiling():
    config = AdmissionConfig().with_overrides({"max_active_jobs": 10_000})
    assert config.max_active_jobs == HARD_MAX_ACTIVE_JOBS


def test_a_malformed_capacity_value_falls_back(db):
    config = AdmissionConfig().with_overrides({"max_active_jobs": "muitos"})
    assert config.max_active_jobs == AdmissionConfig().max_active_jobs


# ==========================================================================
# Runs
# ==========================================================================


def test_a_dry_run_changes_nothing(db, topic, source, service, queue, no_event_fanout):
    for index in range(3):
        make_candidate(db, topic, source, f"c{index}")

    report = service.run(db, topic=topic, dry_run=True, now=NOW)

    assert report.as_dict()["counts"]["admitted"] == 3, "it still reports what it would do"
    assert runs(db) == []
    assert queue.published == []
    assert all(
        row.status == VideoCandidateStatus.SELECTED
        for row in db.query(VideoCandidate)
    )


def test_a_dry_run_reports_capacity(db, topic, source, service, no_event_fanout):
    topic.metadata_json = {"admission": {"max_active_jobs": 3}}
    make_active_run(db, topic)
    make_candidate(db, topic, source)
    db.flush()

    payload = service.run(db, topic=topic, dry_run=True, now=NOW).as_dict()

    assert payload["capacity_limit"] == 3
    assert payload["active_jobs"] == 1
    assert payload["available_slots"] == 2
    assert payload["selected_waiting"] == 1


def test_a_committed_run_respects_the_limit(db, topic, source, service, queue, no_event_fanout):
    for index in range(5):
        make_candidate(db, topic, source, f"c{index}")

    report = service.run(db, topic=topic, limit=2, dry_run=False, now=NOW)

    assert report.as_dict()["counts"]["admitted"] == 2
    assert len(runs(db)) == 2
    assert len(queue.published) == 2


def test_a_committed_run_respects_capacity(db, topic, source, service, no_event_fanout):
    topic.metadata_json = {"admission": {"max_active_jobs": 2}}
    make_active_run(db, topic)
    for index in range(4):
        make_candidate(db, topic, source, f"c{index}")
    db.flush()

    report = service.run(db, topic=topic, limit=5, dry_run=False, now=NOW)

    counts = report.as_dict()["counts"]
    assert counts["admitted"] == 1, "one slot free of two"
    assert counts["temporarily_blocked"] == 3


def test_selection_cannot_flood_the_worker(db, topic, source, service, queue, no_event_fanout):
    """The point of admission: ten selected candidates do not become ten productions."""
    for index in range(10):
        make_candidate(db, topic, source, f"c{index}")

    service.run(db, topic=topic, dry_run=False, now=NOW)

    assert len(queue.published) <= AdmissionConfig().max_active_jobs


def test_admission_order_is_deterministic(db, topic, source, service, no_event_fanout):
    """Not the database's arbitrary order: highest score first, then oldest selection."""
    make_candidate(db, topic, source, "low", relevance_score=0.3)
    make_candidate(db, topic, source, "high", relevance_score=0.95)
    make_candidate(db, topic, source, "mid", relevance_score=0.6)

    report = service.run(db, topic=topic, limit=1, dry_run=False, now=NOW)

    admitted = report.as_dict()["admitted"]
    assert len(admitted) == 1
    run = db.query(PipelineJob).one()
    winner = db.query(VideoCandidate).filter(VideoCandidate.id == run.candidate_id).one()
    assert winner.external_id == "high"


def test_a_run_can_span_every_topic(db, topic, source, service, no_event_fanout):
    other = ContentTopic(name="Outro tema", metadata_json={})
    db.add(other)
    db.flush()
    other_source = DiscoverySource(
        topic_id=other.id, kind=DiscoverySourceKind.RSS, is_active=True, config_json={}
    )
    db.add(other_source)
    db.flush()
    make_candidate(db, topic, source, "a")
    make_candidate(db, other, other_source, "b")

    report = service.run(db, topic=None, limit=5, dry_run=True, now=NOW)

    assert report.selected_waiting == 2


# ==========================================================================
# Enqueue failure
# ==========================================================================


def test_a_queue_outage_does_not_lose_the_candidate(db, topic, source, no_event_fanout):
    """The classic window: the row committed, the message did not."""
    queue = FakeQueue(fail=EnqueueError("redis down", retryable=True))
    service = ProductionAdmissionService(queue=queue)
    candidate = make_candidate(db, topic, source)

    decision = service.admit_candidate(db, candidate=candidate, now=NOW)

    assert decision.outcome == ENQUEUE_FAILED
    assert "queue_unavailable" in decision.reasons
    db.refresh(candidate)
    assert candidate.status == VideoCandidateStatus.SELECTED, "still recoverable"

    run = db.query(PipelineJob).one()
    assert run.enqueued_at is None, "findable as pending-enqueue"


def test_a_pending_enqueue_is_recoverable(db, topic, source, no_event_fanout):
    failing = FakeQueue(fail=EnqueueError("redis down", retryable=True))
    candidate = make_candidate(db, topic, source)
    ProductionAdmissionService(queue=failing).admit_candidate(db, candidate=candidate, now=NOW)

    working = FakeQueue()
    recovered = ProductionAdmissionService(queue=working).retry_pending_enqueue(db, now=NOW)

    assert len(recovered) == 1
    assert recovered[0].outcome == ADMITTED
    assert "recovered_pending_enqueue" in recovered[0].reasons
    assert len(working.published) == 1
    db.refresh(candidate)
    assert candidate.status == VideoCandidateStatus.CONSUMED
    assert len(runs(db)) == 1, "recovery re-dispatches, it does not re-create"


def test_recovery_is_idempotent(db, topic, source, no_event_fanout):
    failing = FakeQueue(fail=EnqueueError("down", retryable=True))
    candidate = make_candidate(db, topic, source)
    ProductionAdmissionService(queue=failing).admit_candidate(db, candidate=candidate, now=NOW)

    working = FakeQueue()
    service = ProductionAdmissionService(queue=working)
    service.retry_pending_enqueue(db, now=NOW)
    second = service.retry_pending_enqueue(db, now=NOW)

    assert second == []
    assert len(working.published) == 1


def test_retrying_admission_after_a_queue_outage_does_not_duplicate(db, topic, source, no_event_fanout):
    """The admission key protects the retry path too."""
    failing = FakeQueue(fail=EnqueueError("down", retryable=True))
    candidate = make_candidate(db, topic, source)
    ProductionAdmissionService(queue=failing).admit_candidate(db, candidate=candidate, now=NOW)

    working = FakeQueue()
    decision = ProductionAdmissionService(queue=working).admit_candidate(
        db, candidate=candidate, now=NOW
    )

    assert decision.outcome == ALREADY_ADMITTED
    assert "pending_enqueue" in decision.reasons
    assert len(runs(db)) == 1


def test_an_unserialisable_payload_is_not_retried_forever(db, topic, source, no_event_fanout):
    queue = FakeQueue(fail=EnqueueError("bad payload", retryable=False))
    service = ProductionAdmissionService(queue=queue)
    candidate = make_candidate(db, topic, source)

    decision = service.admit_candidate(db, candidate=candidate, now=NOW)

    assert decision.outcome == ENQUEUE_FAILED
    assert "payload_rejected" in decision.reasons


# ==========================================================================
# Snapshot
# ==========================================================================


def test_production_inputs_are_frozen_at_admission(db, topic, source, service, no_event_fanout):
    """Editing the topic must not reshape a run already in flight."""
    topic.default_clip_mode = "short_serie"
    topic.default_video_ratio = "portrait"
    db.flush()
    candidate = make_candidate(db, topic, source, url="https://www.youtube.com/watch?v=orig")

    service.admit_candidate(db, candidate=candidate, now=NOW)

    topic.default_clip_mode = "long_series"
    topic.default_video_ratio = "landscape"
    candidate.url = "https://www.youtube.com/watch?v=changed"
    db.flush()

    run = db.query(PipelineJob).one()
    frozen = run.metadata_json["snapshot"]
    assert frozen["clip_mode"] == "short_serie"
    assert frozen["video_ratio"] == "portrait"
    assert frozen["source_url"] == "https://www.youtube.com/watch?v=orig"
    assert run.source_url == "https://www.youtube.com/watch?v=orig"


def test_the_snapshot_is_compact(db, topic, source, service, no_event_fanout):
    """Only what the worker consumes — not a copy of the candidate."""
    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    frozen = db.query(PipelineJob).one().metadata_json["snapshot"]
    assert set(frozen) == {
        "source_url", "clip_mode", "video_ratio", "build_ia", "topic_name", "frozen_at"
    }


# ==========================================================================
# Provenance
# ==========================================================================


def test_a_run_records_where_it_came_from(db, topic, source, service, no_event_fanout):
    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    run = db.query(PipelineJob).one()
    provenance = run.metadata_json["provenance"]
    assert provenance["video_candidate_id"] == str(candidate.id)
    assert provenance["selection_method"] == "policy"
    assert provenance["selection_run_id"] == "run-1"
    assert provenance["selection_score"] == 0.82
    assert provenance["score_version"] == "selection-v1"


def test_the_candidate_records_where_it_went(db, topic, source, service, no_event_fanout):
    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    db.refresh(candidate)
    production = candidate.metadata_json["production"]
    run = db.query(PipelineJob).one()
    assert production["pipeline_job_id"] == str(run.id)
    assert production["worker_job_id"] == run.worker_job_id
    assert production["admission_key"] == admission_key_for(candidate.id)


def test_the_run_read_model_exposes_the_origin(db, topic, source, service, no_event_fanout):
    from app.services.pipeline_job_service import PipelineJobService

    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    view = PipelineJobService().serialize(db.query(PipelineJob).one())
    assert view["video_candidate_id"] == str(candidate.id)
    assert view["admission_key"]
    assert view["enqueued_at"]
    assert view["provenance"]["score_version"] == "selection-v1"


# ==========================================================================
# Worker job id
# ==========================================================================


def test_the_worker_job_id_is_its_own_namespace(db, topic, source, service, no_event_fanout):
    """Not the candidate id: it addresses storage (jobs/<id>/…) and a queue payload, which
    are different lifetimes from a discovery row."""
    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    run = db.query(PipelineJob).one()
    assert run.worker_job_id != str(candidate.id)
    uuid.UUID(run.worker_job_id)


def test_a_retried_admission_keeps_the_same_worker_job_id(db, topic, source, service, no_event_fanout):
    candidate = make_candidate(db, topic, source)
    first = service.admit_candidate(db, candidate=candidate, now=NOW)
    second = service.admit_candidate(db, candidate=candidate, now=NOW)

    assert second.worker_job_id == first.worker_job_id


# ==========================================================================
# Events
# ==========================================================================


def test_admission_emits_a_run_event_and_one_per_admission(db, topic, source, service, no_event_fanout):
    from app.models.pipeline_event import PipelineEvent

    for index in range(2):
        make_candidate(db, topic, source, f"c{index}")

    service.run(db, topic=topic, limit=2, dry_run=False, now=NOW)

    stages = [
        event.stage
        for event in db.query(PipelineEvent).filter(PipelineEvent.service == "admission")
    ]
    assert stages.count("admission.completed") == 1
    assert stages.count("candidate.admitted") == 2


def test_an_admission_event_is_attached_to_its_run(db, topic, source, service, no_event_fanout):
    """From here on the candidate's story is the run's story."""
    from app.models.pipeline_event import PipelineEvent

    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    event = (
        db.query(PipelineEvent)
        .filter(PipelineEvent.stage == "candidate.admitted")
        .one()
    )
    assert str(event.pipeline_job_id) == str(db.query(PipelineJob).one().id)


# ==========================================================================
# API
# ==========================================================================


@pytest.fixture()
def admin_user(db):
    user = User(
        phone_number="+5511977776666", full_name="Admin",
        role=UserRole.ADMIN, status=UserStatus.ACTIVE, credits=100,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture()
def client(db, admin_user, queue, no_event_fanout, monkeypatch):
    monkeypatch.setattr(
        discovery_api, "_admission_service",
        lambda: ProductionAdmissionService(queue=queue),
    )
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    with TestClient(app) as test_client:
        yield test_client


def test_the_run_endpoint_defaults_to_a_dry_run(client, db, topic, source, queue):
    make_candidate(db, topic, source)

    body = client.post("/admin/admission/run", json={"topic_id": str(topic.id)}).json()

    assert body["dry_run"] is True
    assert runs(db) == []
    assert queue.published == []


def test_the_run_endpoint_can_commit(client, db, topic, source, queue):
    make_candidate(db, topic, source)

    body = client.post(
        "/admin/admission/run", json={"topic_id": str(topic.id), "dry_run": False}
    ).json()

    assert body["counts"]["admitted"] == 1
    assert len(runs(db)) == 1
    assert len(queue.published) == 1


def test_the_direct_endpoint_uses_the_same_service(client, db, topic, source, queue):
    candidate = make_candidate(db, topic, source)

    body = client.post(f"/admin/video-candidates/{candidate.id}/admit").json()

    assert body["outcome"] == ADMITTED
    assert body["admission_key"] == admission_key_for(candidate.id)
    assert len(queue.published) == 1


def test_the_direct_endpoint_is_idempotent(client, db, topic, source, queue):
    candidate = make_candidate(db, topic, source)

    client.post(f"/admin/video-candidates/{candidate.id}/admit")
    second = client.post(f"/admin/video-candidates/{candidate.id}/admit").json()

    assert second["outcome"] == ALREADY_ADMITTED
    assert len(runs(db)) == 1
    assert len(queue.published) == 1


def test_an_unbounded_limit_is_rejected(client, topic):
    response = client.post(
        "/admin/admission/run", json={"topic_id": str(topic.id), "limit": 100_000}
    )
    assert response.status_code == 422


def test_admission_endpoints_are_admin_only(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        assert anonymous.post("/admin/admission/run", json={}).status_code in (401, 403)
        assert anonymous.post(
            f"/admin/video-candidates/{uuid.uuid4()}/admit"
        ).status_code in (401, 403)


def test_admission_is_audited(client, db, topic, source):
    from app.models.audit_log import AuditLog

    candidate = make_candidate(db, topic, source)
    client.post(f"/admin/video-candidates/{candidate.id}/admit")

    assert "admin.admission.candidate" in {e.action for e in db.query(AuditLog)}


def test_the_candidate_detail_shows_its_production(client, db, topic, source):
    candidate = make_candidate(db, topic, source)
    client.post(f"/admin/video-candidates/{candidate.id}/admit")

    body = client.get(f"/admin/video-candidates/{candidate.id}").json()

    assert body["status"] == "consumed"
    assert body["production"]["pipeline_job_id"]
    assert body["selection_method"] == "policy"


def test_the_retry_endpoint_recovers_pending_enqueues(db, admin_user, topic, source, no_event_fanout, monkeypatch):
    failing = FakeQueue(fail=EnqueueError("down", retryable=True))
    candidate = make_candidate(db, topic, source)
    ProductionAdmissionService(queue=failing).admit_candidate(db, candidate=candidate, now=NOW)

    working = FakeQueue()
    monkeypatch.setattr(
        discovery_api, "_admission_service",
        lambda: ProductionAdmissionService(queue=working),
    )
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    with TestClient(app) as client:
        body = client.post("/admin/admission/retry-pending").json()

    assert len(body["recovered"]) == 1
    assert len(working.published) == 1


# ==========================================================================
# Selection stays separate
# ==========================================================================


def test_manual_selection_no_longer_creates_a_run(client, db, topic, source, queue):
    """It used to create a PipelineJob and never enqueue it — an orphan by construction."""
    candidate = make_candidate(db, topic, source, status=VideoCandidateStatus.RANKED)

    body = client.post(f"/admin/video-candidates/{candidate.id}/select").json()

    assert body["admitted"] is False
    assert runs(db) == []
    assert queue.published == []
    db.refresh(candidate)
    assert candidate.status == VideoCandidateStatus.SELECTED


def test_selecting_an_already_admitted_candidate_is_refused(client, db, topic, source):
    candidate = make_candidate(db, topic, source, status=VideoCandidateStatus.CONSUMED)

    assert client.post(f"/admin/video-candidates/{candidate.id}/select").status_code == 409


def test_nothing_here_publishes(db, topic, source, service, no_event_fanout):
    """Admission ends at the queue. Publishing does not exist."""
    from app.models.publish_attempt import PublishAttempt

    candidate = make_candidate(db, topic, source)
    service.admit_candidate(db, candidate=candidate, now=NOW)

    assert db.query(PublishAttempt).count() == 0
    assert db.query(PipelineJob).one().state == PipelineState.QUEUED
