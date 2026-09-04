"""PR-PUBLISH-02 — the autonomous publication policy.

Almost every test here asserts that something did **not** happen. That is the shape of the
feature: autopublish is a list of reasons not to publish, and the one path through them is
narrow on purpose.

The policy never reaches a provider, so there is no transport to fake. What is faked is the
publisher fleet's liveness and the queue depths, because those are operational facts the
policy reads rather than owns.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from app.core.settings import settings
from app.models.content_topic import ContentTopic
from app.models.enums import (
    PipelineState,
    PublishAttemptStatus,
    PublishTargetConnectionStatus,
)
from app.models.publish_attempt import PublishAttempt
from app.publishing.publish_queue import PublishQueue
from app.services.autopublish_service import (
    ALREADY_PUBLISHED,
    DAILY_LIMIT,
    DEAD_LETTER_BACKPRESSURE,
    ELIGIBILITY_MISSING,
    GLOBAL_AUTOPUBLISH_DISABLED,
    GLOBAL_PUBLISHING_DISABLED,
    HISTORICAL,
    IN_FLIGHT,
    INITIATOR,
    OPERATOR_CANCELED,
    PER_RUN_LIMIT,
    POLICY_ONLY_REASONS,
    POLICY_VERSION,
    PREVIOUS_FINAL_FAILURE,
    PUBLIC_DISABLED,
    PUBLICATION_INELIGIBLE,
    PUBLISHER_UNAVAILABLE,
    QUEUE_BACKPRESSURE,
    TARGET_AUTOPUBLISH_DISABLED,
    TARGET_DISCONNECTED,
    TARGET_INACTIVE,
    TARGET_NOT_CONFIGURED,
    TOPIC_AUTOMATION_DISABLED,
    TOPIC_AUTOPUBLISH_DISABLED,
    UNRESOLVED_ATTEMPT,
    AutonomousPublicationService,
)
from app.services.autopublish_budget import utc_today
from app.services.publishing_service import PublishingService, idempotency_key
from tests.conftest import make_run
from tests.test_automation import StubAdmission, StubDiscovery, StubSelection
from tests.test_publishing import (  # noqa: F401 - publishing_config is autouse
    TEST_KEY,
    StubArtifacts,
    StubMediaSource,
    StubPublisher,
    make_target,
    publishing_config,
)

ALIVE = [{"worker_id": "publisher-1", "last_heartbeat_at": "2026-01-01T00:00:00+00:00"}]


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def autopublish_config(monkeypatch):
    """A deployment where automation is switched on. Tests turn things off."""
    monkeypatch.setattr(settings, "autopublish_enabled", True, raising=False)
    monkeypatch.setattr(settings, "autopublish_public_enabled", False, raising=False)
    monkeypatch.setattr(settings, "autopublish_default_privacy", "private", raising=False)
    monkeypatch.setattr(settings, "autopublish_max_per_tick", 5, raising=False)
    monkeypatch.setattr(settings, "autopublish_max_per_day", 10, raising=False)
    monkeypatch.setattr(settings, "autopublish_max_queue_backlog", 20, raising=False)
    monkeypatch.setattr(settings, "autopublish_max_dead_letter", 10, raising=False)


@pytest.fixture()
def queue():
    return PublishQueue(
        fakeredis.FakeRedis(decode_responses=True), "test_autopublish_jobs",
        worker_id="test",
    )


def policy(queue, publisher=None, artifacts=None, workers=ALIVE) -> AutonomousPublicationService:
    return AutonomousPublicationService(
        publishing=PublishingService(
            publisher=publisher or StubPublisher(),
            artifacts=artifacts or StubArtifacts(),
            media_source=StubMediaSource(),
            queue=queue,
        ),
        queue=queue,
        heartbeat_reader=lambda: list(workers),
    )


def make_topic(db, *, target=None, autopublish=True, enabled=True, **automation) -> ContentTopic:
    config = {
        "enabled": enabled,
        "autopublish_enabled": autopublish,
        "interval_minutes": 60,
    }
    if target is not None:
        config["publish_target_id"] = str(target.id)
    config.update(automation)

    topic = ContentTopic(
        name=f"Topic {uuid.uuid4().hex[:8]}",
        keywords_json=["futebol"],
        default_clip_mode="short_serie",
        default_video_ratio="portrait",
        is_active=True,
        metadata_json={"automation": config},
    )
    db.add(topic)
    db.flush()
    return topic


def make_ready_job(db, topic=None, *, ready_at=None, eligible=True, **overrides):
    """A run that finished and cleared the technical gate."""
    when = ready_at or datetime.now(timezone.utc)
    metadata = {
        "publication_eligibility": {
            "eligible": eligible,
            "technical_gate": "pass" if eligible else "fail",
            "blocked_by": [] if eligible else ["final_media_qa_fail"],
        },
        "first_ready_at": when.isoformat(),
    }
    fields = {
        "state": PipelineState.READY_TO_PUBLISH,
        "metadata_json": metadata,
        "finished_at": when,
    }
    if topic is not None:
        fields["topic_id"] = topic.id
    fields.update(overrides)
    return make_run(db, **fields)


def autopublish_target(db, **overrides):
    fields = {
        "autopublish_enabled": True,
        "autopublish_enabled_at": datetime.now(timezone.utc) - timedelta(days=1),
    }
    fields.update(overrides)
    return make_target(db, **fields)


def attempts_count(db) -> int:
    return db.query(PublishAttempt).count()


# ===========================================================================
# Kill switches
# ===========================================================================


def test_the_global_publishing_switch_blocks_everything(db, queue, monkeypatch,
                                                        no_event_fanout):
    monkeypatch.setattr(settings, "publishing_enabled", False, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue).run(db, dry_run=False)

    assert report.status == "blocked"
    assert GLOBAL_PUBLISHING_DISABLED in report.blocked_reasons
    assert attempts_count(db) == 0
    assert queue.depths()["ready"] == 0


def test_the_global_autopublish_switch_blocks_automatic_publication(
    db, queue, monkeypatch, no_event_fanout
):
    """Manual publishing stays on; only the automatic path stops."""
    monkeypatch.setattr(settings, "autopublish_enabled", False, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue).run(db, dry_run=False)

    assert GLOBAL_AUTOPUBLISH_DISABLED in report.blocked_reasons
    assert attempts_count(db) == 0
    assert settings.publishing_enabled is True, "manual publishing is unaffected"


def test_autopublish_defaults_to_off():
    """Deploying this PR must not make an installation start publishing by itself."""
    from app.core.settings import Settings

    assert Settings.model_fields["autopublish_enabled"].default is False
    assert Settings.model_fields["autopublish_public_enabled"].default is False


def test_a_topic_with_automation_off_is_never_autopublished(db, queue, no_event_fanout):
    target = autopublish_target(db)
    topic = make_topic(db, target=target, enabled=False)
    make_ready_job(db, topic)

    report = policy(queue).run(db, dry_run=False)

    assert report.candidates[0].reasons == [TOPIC_AUTOMATION_DISABLED]
    assert attempts_count(db) == 0


def test_a_topic_with_autopublish_off_is_never_autopublished(db, queue, no_event_fanout):
    target = autopublish_target(db)
    topic = make_topic(db, target=target, autopublish=False)
    make_ready_job(db, topic)

    report = policy(queue).run(db, dry_run=False)

    assert report.candidates[0].reasons == [TOPIC_AUTOPUBLISH_DISABLED]
    assert attempts_count(db) == 0


def test_a_topic_defaults_to_autopublish_off(db):
    """An existing topic does not gain automation because this PR shipped."""
    from app.services.automation_service import AutomationConfig

    topic = ContentTopic(name="legacy", metadata_json={"automation": {"enabled": True}})
    assert AutomationConfig.from_topic(topic).autopublish_enabled is False


def test_a_target_with_autopublish_off_is_never_autopublished(db, queue, no_event_fanout):
    """is_active is not consent to publish without a human."""
    target = autopublish_target(db, autopublish_enabled=False)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue).run(db, dry_run=False)

    assert report.candidates[0].reasons == [TARGET_AUTOPUBLISH_DISABLED]
    assert attempts_count(db) == 0


def test_an_inactive_target_is_never_autopublished(db, queue, no_event_fanout):
    target = autopublish_target(db, is_active=False)
    make_ready_job(db, make_topic(db, target=target))

    assert policy(queue).run(db, dry_run=False).candidates[0].reasons == [TARGET_INACTIVE]
    assert attempts_count(db) == 0


def test_a_disconnected_target_is_never_autopublished(db, queue, no_event_fanout):
    target = autopublish_target(
        db, connection_status=PublishTargetConnectionStatus.RECONNECT_REQUIRED
    )
    make_ready_job(db, make_topic(db, target=target))

    assert policy(queue).run(db, dry_run=False).candidates[0].reasons == [TARGET_DISCONNECTED]
    assert attempts_count(db) == 0


# ===========================================================================
# Target routing
# ===========================================================================


def test_a_topic_without_a_configured_target_is_blocked(db, queue, no_event_fanout):
    """No implicit channel: guessing one risks a video on the wrong audience."""
    autopublish_target(db)  # a perfectly good target exists, and is NOT used
    make_ready_job(db, make_topic(db, target=None))

    report = policy(queue).run(db, dry_run=False)

    assert report.candidates[0].reasons == [TARGET_NOT_CONFIGURED]
    assert attempts_count(db) == 0


def test_the_configured_target_is_the_one_used(db, queue, no_event_fanout):
    autopublish_target(db, channel_id="UC_wrong", name="Wrong channel")
    right = autopublish_target(db, channel_id="UC_right", name="Right channel")
    make_ready_job(db, make_topic(db, target=right))

    report = policy(queue).run(db, dry_run=False)

    assert report.queued == 1
    assert db.query(PublishAttempt).one().target_id == right.id


# ===========================================================================
# Safety gates
# ===========================================================================


def test_a_run_awaiting_review_is_never_autopublished(db, queue, no_event_fanout):
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target), state=PipelineState.REVIEW_REQUIRED)

    report = policy(queue).run(db, dry_run=False)

    assert report.considered == 0, "it is not even a candidate"
    assert attempts_count(db) == 0


def test_an_ineligible_run_is_never_autopublished(db, queue, no_event_fanout):
    """State and eligibility disagreeing is exactly when to refuse."""
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target), eligible=False)

    assert policy(queue).run(db, dry_run=False).candidates[0].reasons == [
        PUBLICATION_INELIGIBLE
    ]
    assert attempts_count(db) == 0


def test_a_run_with_no_eligibility_record_is_refused(db, queue, no_event_fanout):
    """Fail-closed: an unmeasured gate is not a passed gate."""
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target), metadata_json={})

    assert policy(queue).run(db, dry_run=False).candidates[0].reasons == [
        ELIGIBILITY_MISSING
    ]
    assert attempts_count(db) == 0


@pytest.mark.parametrize(
    "state", [PipelineState.FAILED, PipelineState.CANCELED, PipelineState.PUBLISHED,
              PipelineState.PUBLISHING, PipelineState.RENDERING],
)
def test_only_ready_runs_are_candidates(db, queue, state, no_event_fanout):
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target), state=state)

    report = policy(queue).run(db, dry_run=False)

    assert report.considered == 0
    assert attempts_count(db) == 0


# ===========================================================================
# Privacy
# ===========================================================================


def test_private_is_the_default_and_is_allowed(db, queue, no_event_fanout):
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue).run(db, dry_run=False)

    assert report.queued == 1
    snapshot = db.query(PublishAttempt).one().payload_json["metadata"]
    assert snapshot["privacy"] == "private"


def test_a_public_target_default_cannot_route_around_the_global_guard(
    db, queue, no_event_fanout
):
    """Checked on the resolved value, so a target preference cannot bypass it."""
    target = autopublish_target(db, config_json={"default_privacy": "public"})
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue).run(db, dry_run=False)

    assert report.candidates[0].reasons == [PUBLIC_DISABLED]
    assert attempts_count(db) == 0


def test_public_is_allowed_only_with_its_own_switch(db, queue, monkeypatch,
                                                    no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_public_enabled", True, raising=False)
    target = autopublish_target(db, config_json={"default_privacy": "public"})
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue).run(db, dry_run=False)

    assert report.queued == 1
    assert db.query(PublishAttempt).one().payload_json["metadata"]["privacy"] == "public"


def test_unlisted_requires_explicit_target_configuration(db, queue, no_event_fanout):
    target = autopublish_target(db, config_json={"default_privacy": "unlisted"})
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue).run(db, dry_run=False)

    assert report.queued == 1
    assert db.query(PublishAttempt).one().payload_json["metadata"]["privacy"] == "unlisted"


# ===========================================================================
# Historical backlog
# ===========================================================================


def test_a_run_ready_before_the_cutoff_is_not_autopublished(db, queue, no_event_fanout):
    """Enabling automation must not publish everything that was already waiting."""
    target = autopublish_target(
        db, autopublish_enabled_at=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    make_ready_job(
        db, make_topic(db, target=target),
        ready_at=datetime.now(timezone.utc) - timedelta(days=30),
    )

    report = policy(queue).run(db, dry_run=False)

    assert report.candidates[0].reasons == [HISTORICAL]
    assert attempts_count(db) == 0
    # ...and it is a policy pause, so a human may still publish it.
    assert report.candidates[0].as_dict()["manual_publish_still_allowed"] is True


def test_a_run_ready_after_the_cutoff_is_autopublished(db, queue, no_event_fanout):
    target = autopublish_target(
        db, autopublish_enabled_at=datetime.now(timezone.utc) - timedelta(hours=2)
    )
    make_ready_job(
        db, make_topic(db, target=target),
        ready_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    assert policy(queue).run(db, dry_run=False).queued == 1


def test_the_strictest_cutoff_wins(db, queue, no_event_fanout):
    """A topic and a target may each have one; the later of the two applies."""
    target = autopublish_target(
        db, autopublish_enabled_at=datetime.now(timezone.utc) - timedelta(days=10)
    )
    topic = make_topic(
        db, target=target,
        autopublish_enabled_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    make_ready_job(db, topic, ready_at=datetime.now(timezone.utc) - timedelta(days=2))

    assert policy(queue).run(db, dry_run=False).candidates[0].reasons == [HISTORICAL]


def test_first_ready_at_is_not_reset_by_a_failed_publication(db, no_event_fanout):
    """Regression guard: ``finished_at`` moves, so it cannot anchor the cutoff."""
    from app.services.pipeline_state_machine import PipelineStateMachine

    job = make_ready_job(db, ready_at=datetime.now(timezone.utc) - timedelta(days=5))
    original = job.metadata_json["first_ready_at"]

    machine = PipelineStateMachine()
    machine.start_publishing(db, job, actor="test")
    machine.publish_failed(db, job, reason="backendError")
    db.flush()

    assert job.metadata_json["first_ready_at"] == original
    assert job.finished_at is not None


# ===========================================================================
# Existing attempts
# ===========================================================================


def _attempt(db, job, target, status, media="final_clips/final_clip_01.mp4", **overrides):
    fields = dict(
        pipeline_job_id=job.id, target_id=target.id,
        idempotency_key=idempotency_key(job.id, target.id, media),
        media_identity=media, media_storage_key=f"jobs/x/{media}",
        media_bytes=1024, status=status, attempt_no=1, initiator=INITIATOR,
        # Automatic publications carry the UTC day they are charged to, exactly as the real
        # creation path writes it. A manual one carries none - it spends no automatic budget.
        budget_date=utc_today(),
    )
    if overrides.get("initiator") == "manual":
        fields["budget_date"] = None
    fields.update(overrides)
    attempt = PublishAttempt(**fields)
    db.add(attempt)
    db.flush()
    return attempt


def test_an_unresolved_attempt_stops_any_replacement(db, queue, no_event_fanout):
    """The invariant, one level up: never publish over a publication nobody can account for."""
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    _attempt(db, job, target, PublishAttemptStatus.UNKNOWN)

    publisher = StubPublisher()
    report = policy(queue, publisher=publisher).run(db, dry_run=False)

    assert report.candidates[0].reasons == [UNRESOLVED_ATTEMPT]
    assert attempts_count(db) == 1, "no replacement was created"
    assert publisher.calls == []
    # A safety refusal, not a policy pause.
    assert report.candidates[0].as_dict()["manual_publish_still_allowed"] is False


def test_a_needs_manual_resolution_attempt_also_stops_replacement(db, queue,
                                                                 no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    _attempt(db, job, target, PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION)

    assert policy(queue).run(db, dry_run=False).candidates[0].reasons == [UNRESOLVED_ATTEMPT]
    assert attempts_count(db) == 1


def test_a_final_failure_is_not_recreated_every_tick(db, queue, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    _attempt(db, job, target, PublishAttemptStatus.FAILED_FINAL)

    service = policy(queue)
    for _ in range(3):
        report = service.run(db, dry_run=False)

    assert report.candidates[0].reasons == [PREVIOUS_FINAL_FAILURE]
    assert attempts_count(db) == 1


def test_a_canceled_attempt_is_treated_as_an_operator_veto(db, queue, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    _attempt(db, job, target, PublishAttemptStatus.CANCELED)

    assert policy(queue).run(db, dry_run=False).candidates[0].reasons == [OPERATOR_CANCELED]
    assert attempts_count(db) == 1


@pytest.mark.parametrize(
    "status", [PublishAttemptStatus.PENDING, PublishAttemptStatus.IN_PROGRESS]
)
def test_a_publication_already_in_flight_is_not_duplicated(db, queue, status,
                                                           no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    _attempt(db, job, target, status)

    assert policy(queue).run(db, dry_run=False).candidates[0].reasons == [IN_FLIGHT]
    assert attempts_count(db) == 1


def test_only_the_unpublished_clips_of_a_partly_published_run_are_queued(
    db, queue, no_event_fanout
):
    """Three clips, one and three already done. Only the second is created."""
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    _attempt(db, job, target, PublishAttemptStatus.SUCCEEDED,
             media="final_clips/final_clip_01.mp4", external_id="vid_1")
    _attempt(db, job, target, PublishAttemptStatus.SUCCEEDED,
             media="final_clips/final_clip_03.mp4", external_id="vid_3")

    publisher = StubPublisher()
    report = policy(queue, publisher=publisher,
                    artifacts=StubArtifacts(videos=3)).run(db, dry_run=False)

    assert report.queued == 1
    assert report.candidates[0].queued_media == ["final_clips/final_clip_02.mp4"]
    assert attempts_count(db) == 3
    assert queue.depths()["ready"] == 1, "the two finished clips were not re-queued"


# ===========================================================================
# Caps
# ===========================================================================


def test_the_daily_cap_counts_logical_publications(db, queue, monkeypatch,
                                                   no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_max_per_day", 3, raising=False)
    target = autopublish_target(db)
    topic = make_topic(db, target=target)

    # Three automatic publications already made today.
    for index in range(3):
        job = make_ready_job(db, topic, state=PipelineState.PUBLISHED)
        _attempt(db, job, target, PublishAttemptStatus.SUCCEEDED,
                 media=f"final_clips/done_{index}.mp4", external_id=f"vid_{index}")

    make_ready_job(db, topic)
    report = policy(queue).run(db, dry_run=False)

    assert report.daily_used == 3
    assert report.daily_remaining == 0
    assert DAILY_LIMIT in report.blocked_reasons
    assert queue.depths()["ready"] == 0


def test_a_retry_does_not_spend_the_cap_again(db, queue, monkeypatch, no_event_fanout):
    """One row per logical publication; retries increment attempt_no, not the count."""
    monkeypatch.setattr(settings, "autopublish_max_per_day", 3, raising=False)
    target = autopublish_target(db)
    topic = make_topic(db, target=target)
    job = make_ready_job(db, topic, state=PipelineState.PUBLISHED)
    attempt = _attempt(db, job, target, PublishAttemptStatus.FAILED_RETRYABLE)

    attempt.attempt_no = 3  # retried twice
    db.flush()

    make_ready_job(db, topic)
    before = policy(queue)._published_today(db)
    report = policy(queue).run(db, dry_run=False)

    # The seeded row has attempt_no=3 - three provider attempts - and contributes exactly
    # one unit. The cap is charged per logical publication, not per try.
    assert before == 1, "three provider attempts on one row is one logical publication"
    assert report.queued == 1
    assert report.daily_used == 2, "the pre-existing one, plus the one this run created"


def test_manual_publications_do_not_consume_the_automatic_cap(db, queue, monkeypatch,
                                                              no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_max_per_day", 1, raising=False)
    target = autopublish_target(db)
    topic = make_topic(db, target=target)
    done = make_ready_job(db, topic, state=PipelineState.PUBLISHED)
    _attempt(db, done, target, PublishAttemptStatus.SUCCEEDED, initiator="manual",
             external_id="vid_manual")

    make_ready_job(db, topic)
    report = policy(queue).run(db, dry_run=False)

    # The cap is 1 and a manual publication already exists. Had manual spent the budget this
    # run would have been blocked; it was not, and the one unit now used is its own.
    assert report.queued == 1
    assert report.daily_used == 1, "only this run's automatic publication is charged"


def test_the_per_tick_cap_bounds_one_run(db, queue, monkeypatch, no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_max_per_tick", 2, raising=False)
    target = autopublish_target(db)
    topic = make_topic(db, target=target)
    for _ in range(10):
        make_ready_job(db, topic)

    report = policy(queue).run(db, dry_run=False)

    assert report.queued == 2
    assert report.blocked_reasons.get(PER_RUN_LIMIT) == 8
    assert attempts_count(db) == 2


def test_absurd_caps_are_clamped_server_side(db, queue, monkeypatch, no_event_fanout):
    """Configuration is intent, not a licence."""
    monkeypatch.setattr(settings, "autopublish_max_per_tick", 100000, raising=False)
    monkeypatch.setattr(settings, "autopublish_max_per_day", 100000, raising=False)
    target = autopublish_target(db)
    topic = make_topic(db, target=target)
    for _ in range(12):
        make_ready_job(db, topic)

    report = policy(queue).run(db, dry_run=False)

    from app.core.settings import AUTOPUBLISH_CEILING_PER_TICK

    assert report.queued <= AUTOPUBLISH_CEILING_PER_TICK
    assert report.daily_remaining <= 50


def test_the_oldest_ready_run_goes_first(db, queue, monkeypatch, no_event_fanout):
    """Deterministic ordering from persisted state, not database order."""
    monkeypatch.setattr(settings, "autopublish_max_per_tick", 1, raising=False)
    target = autopublish_target(db)
    topic = make_topic(db, target=target)

    newest = make_ready_job(db, topic, ready_at=datetime.now(timezone.utc))
    oldest = make_ready_job(
        db, topic, ready_at=datetime.now(timezone.utc) - timedelta(hours=3)
    )

    report = policy(queue).run(db, dry_run=False)

    assert report.queued == 1
    assert db.query(PublishAttempt).one().pipeline_job_id == oldest.id
    assert newest.id != oldest.id


# ===========================================================================
# Operational backpressure
# ===========================================================================


def test_no_publisher_alive_means_nothing_is_queued(db, queue, no_event_fanout):
    """Otherwise a dead fleet accumulates a day of uploads that all arrive at once."""
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue, workers=[]).run(db, dry_run=False)

    assert PUBLISHER_UNAVAILABLE in report.blocked_reasons
    assert attempts_count(db) == 0
    assert queue.depths()["ready"] == 0


def test_unreadable_liveness_blocks_rather_than_assumes(db, queue, no_event_fanout):
    def explode():
        raise RuntimeError("redis is down")

    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    service = policy(queue)
    service._heartbeat_reader = explode
    report = service.run(db, dry_run=False)

    assert PUBLISHER_UNAVAILABLE in report.blocked_reasons
    assert attempts_count(db) == 0


def test_a_saturated_queue_pauses_automation(db, queue, monkeypatch, no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_max_queue_backlog", 2, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))
    for _ in range(3):
        queue.enqueue({"version": 1, "publish_attempt_id": str(uuid.uuid4()),
                       "pipeline_job_id": "j", "target_id": "t", "media_identity": "m"})

    report = policy(queue).run(db, dry_run=False)

    assert QUEUE_BACKPRESSURE in report.blocked_reasons
    assert attempts_count(db) == 0


def test_a_pile_of_dead_letters_pauses_automation(db, queue, monkeypatch,
                                                  no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_max_dead_letter", 2, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))
    for _ in range(2):
        queue.redis.lpush(queue.dead_key, "{}")

    assert DEAD_LETTER_BACKPRESSURE in policy(queue).run(
        db, dry_run=False
    ).blocked_reasons


def test_one_old_dead_letter_does_not_stop_publishing_forever(db, queue, monkeypatch,
                                                              no_event_fanout):
    """Not zero-tolerance, deliberately."""
    monkeypatch.setattr(settings, "autopublish_max_dead_letter", 10, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))
    queue.redis.lpush(queue.dead_key, "{}")

    assert policy(queue).run(db, dry_run=False).queued == 1


# ===========================================================================
# Dry run and provenance
# ===========================================================================


def test_a_dry_run_creates_nothing(db, queue, no_event_fanout):
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))
    publisher = StubPublisher()

    report = policy(queue, publisher=publisher).run(db, dry_run=True)

    assert report.eligible == 1
    assert report.candidates[0].status == "would_queue"
    assert attempts_count(db) == 0
    assert queue.depths()["ready"] == 0
    assert publisher.calls == []


def test_a_dry_run_reports_the_operational_picture(db, queue, no_event_fanout):
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue).run(db, dry_run=True).as_dict()

    assert report["considered"] == 1
    assert report["daily_remaining"] > 0
    assert report["publisher_workers_alive"] == 1
    assert report["queue_backlog"] == 0


def test_an_automatic_publication_records_its_provenance(db, queue, no_event_fanout):
    target = autopublish_target(db)
    topic = make_topic(db, target=target)
    make_ready_job(db, topic)

    policy(queue).run(db, dry_run=False, automation_run_id="run-42")

    attempt = db.query(PublishAttempt).one()
    provenance = attempt.payload_json["provenance"]
    assert attempt.initiator == "automatic"
    assert provenance["policy_version"] == POLICY_VERSION
    assert provenance["automation_run_id"] == "run-42"
    assert provenance["topic_id"] == str(topic.id)
    assert provenance["autopublish_run_id"]


def test_a_manual_publication_is_marked_manual(db, queue, no_event_fanout):
    from tests.test_publishing import make_publishable_run

    target = make_target(db)
    job = make_publishable_run(db)
    PublishingService(
        publisher=StubPublisher(), artifacts=StubArtifacts(),
        media_source=StubMediaSource(), queue=queue,
    ).publish(db, job=job, target=target, dry_run=False)

    attempt = db.query(PublishAttempt).one()
    assert attempt.initiator == "manual"
    assert attempt.payload_json["provenance"] == {}


def test_the_policy_never_uploads(db, queue, no_event_fanout):
    """§79: a tick queues commands. The provider is the publisher's business."""
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))
    publisher = StubPublisher()

    report = policy(queue, publisher=publisher).run(db, dry_run=False)

    assert report.queued == 1
    assert publisher.calls == [], "no provider call from the policy"
    assert queue.depths()["ready"] == 1, "a command is waiting for the publisher"


# ===========================================================================
# Status read model
# ===========================================================================


def test_the_status_endpoint_reports_the_whole_picture(db, queue, no_event_fanout):
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    status = policy(queue).status(db)

    assert status["autopublish_enabled"] is True
    assert status["public_enabled"] is False
    assert status["default_privacy"] == "private"
    assert status["ready_jobs"] == 1
    assert status["publisher_workers_alive"] == 1
    assert status["daily_remaining"] == status["daily_cap"]
    assert status["policy_version"] == POLICY_VERSION


def test_the_status_contains_no_secret(db, queue, no_event_fanout):
    import json

    from tests.test_publishing import REFRESH_TOKEN

    autopublish_target(db)
    flat = json.dumps(policy(queue).status(db), default=str)
    assert REFRESH_TOKEN not in flat
    assert "refresh_token" not in flat


# ===========================================================================
# Reason semantics
# ===========================================================================


def test_policy_pauses_and_safety_refusals_are_distinguished():
    """§38: a human may publish through a pause, but not through a safety gate."""
    assert DAILY_LIMIT in POLICY_ONLY_REASONS
    assert HISTORICAL in POLICY_ONLY_REASONS
    assert PUBLIC_DISABLED in POLICY_ONLY_REASONS
    assert PUBLISHER_UNAVAILABLE in POLICY_ONLY_REASONS

    assert PUBLICATION_INELIGIBLE not in POLICY_ONLY_REASONS
    assert UNRESOLVED_ATTEMPT not in POLICY_ONLY_REASONS
    assert TARGET_DISCONNECTED not in POLICY_ONLY_REASONS
    assert ELIGIBILITY_MISSING not in POLICY_ONLY_REASONS


def test_no_run_reports_a_bare_blocked(db, queue, monkeypatch, no_event_fanout):
    """Every refusal names itself."""
    monkeypatch.setattr(settings, "autopublish_enabled", False, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue).run(db, dry_run=False)

    assert report.blocked_reasons
    assert "blocked" not in report.blocked_reasons


def test_an_empty_run_is_not_a_failure(db, queue, no_event_fanout):
    """§45: nothing ready is the normal state of a working system."""
    report = policy(queue).run(db, dry_run=False)

    assert report.status == "noop"
    assert report.considered == 0
    assert report.blocked == 0


# ===========================================================================
# Scheduler integration
# ===========================================================================


def test_the_automation_report_carries_a_publication_stage(db, queue, no_event_fanout):
    from app.services.automation_service import AutonomousPipelineService

    target = autopublish_target(db)
    topic = make_topic(db, target=target)
    make_ready_job(db, topic)

    service = AutonomousPipelineService(
        discovery=StubDiscovery(), selection=StubSelection(), admission=StubAdmission(),
        publication=policy(queue),
    )
    report = service.run_topic(db, topic=topic)

    assert report.publication.status == "ok"
    assert report.publication.counts["queued"] == 1
    assert "publication" in report.as_dict()


def test_a_publication_failure_does_not_abort_the_earlier_stages(db, queue,
                                                                 no_event_fanout):
    from app.services.automation_service import AutonomousPipelineService

    class Exploding:
        def run(self, *args, **kwargs):
            raise RuntimeError("policy blew up")

    target = autopublish_target(db)
    topic = make_topic(db, target=target)

    service = AutonomousPipelineService(
        discovery=StubDiscovery(), selection=StubSelection(), admission=StubAdmission(),
        publication=Exploding(),
    )
    report = service.run_topic(db, topic=topic)

    assert report.discovery.status == "ok"
    assert report.selection.status == "ok"
    assert report.admission.status == "ok"
    assert report.publication.status == "failed"
    assert report.publication.reasons == ["RuntimeError"]
    assert report.status == "partial", "a failed stage does not fail the whole run"


def test_a_topic_without_autopublish_skips_the_stage(db, queue, no_event_fanout):
    from app.services.automation_service import AutonomousPipelineService

    target = autopublish_target(db)
    topic = make_topic(db, target=target, autopublish=False)

    service = AutonomousPipelineService(
        discovery=StubDiscovery(), selection=StubSelection(), admission=StubAdmission(),
        publication=policy(queue),
    )
    report = service.run_topic(db, topic=topic)

    assert report.publication.status == "disabled"


# ===========================================================================
# Concurrency
# ===========================================================================


def test_two_policy_runs_at_once_do_not_duplicate_a_publication(db, queue,
                                                                no_event_fanout):
    """Two replicas may evaluate the policy simultaneously.

    The policy does not need a lock of its own to be safe: the publishing command it calls
    is idempotent under a unique index on the idempotency key, so the second run finds the
    first one's attempt and adds nothing. Running them back to back is the sequential
    equivalent, and the assertion is the same one that matters - one logical publication.
    """
    target = autopublish_target(db)
    topic = make_topic(db, target=target)
    make_ready_job(db, topic)

    publisher = StubPublisher()
    first = policy(queue, publisher=publisher)
    second = policy(queue, publisher=publisher)

    report_a = first.run(db, dry_run=False)
    report_b = second.run(db, dry_run=False)

    assert report_a.queued == 1
    assert report_b.queued == 0, "the second run found the publication already made"
    assert attempts_count(db) == 1
    assert queue.depths()["ready"] == 1


def test_a_second_run_respects_the_cap_the_first_consumed(db, queue, monkeypatch,
                                                          no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_max_per_day", 1, raising=False)
    target = autopublish_target(db)
    topic = make_topic(db, target=target)
    make_ready_job(db, topic)
    make_ready_job(db, topic)

    service = policy(queue)
    first = service.run(db, dry_run=False)
    second = service.run(db, dry_run=False)

    assert first.queued == 1
    assert second.queued == 0
    assert DAILY_LIMIT in second.blocked_reasons
    assert attempts_count(db) == 1


# ===========================================================================
# API
# ===========================================================================


@pytest.fixture()
def admin_user(db):
    from app.models.enums import UserRole, UserStatus
    from app.models.user import User

    user = User(
        phone_number="+5511955554444", full_name="Autopublish Admin",
        role=UserRole.ADMIN, status=UserStatus.ACTIVE, credits=100,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture()
def client(db, admin_user, queue, no_event_fanout, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import publishing as publishing_api
    from app.api.router import api_router
    from app.db.session import get_db
    from app.security.auth_middleware import get_current_admin

    monkeypatch.setattr(publishing_api, "_autopublish", lambda: policy(queue))

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    with TestClient(app) as test_client:
        yield test_client


def test_the_autopublish_routes_require_an_admin(db, no_event_fanout):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.router import api_router
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        assert anonymous.get("/admin/autopublish/status").status_code in (401, 403)
        assert anonymous.post("/admin/autopublish/run", json={}).status_code in (401, 403)


def test_the_run_endpoint_defaults_to_a_dry_run(client, db, queue):
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    body = client.post("/admin/autopublish/run", json={}).json()

    assert body["dry_run"] is True
    assert body["candidates"][0]["status"] == "would_queue"
    assert attempts_count(db) == 0
    assert queue.depths()["ready"] == 0


def test_the_run_endpoint_can_act_and_is_audited(client, db, queue):
    from app.models.audit_log import AuditLog

    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    body = client.post("/admin/autopublish/run", json={"dry_run": False}).json()

    assert body["queued"] == 1
    assert attempts_count(db) == 1
    entry = db.query(AuditLog).filter(AuditLog.action == "admin.autopublish.run").one()
    assert entry.metadata_json["queued"] == 1


def test_a_dry_run_is_not_audited(client, db):
    from app.models.audit_log import AuditLog

    client.post("/admin/autopublish/run", json={"dry_run": True})
    assert db.query(AuditLog).filter(AuditLog.action == "admin.autopublish.run").count() == 0


def test_the_status_endpoint_answers_whether_the_system_may_publish(client, db):
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    body = client.get("/admin/autopublish/status").json()

    assert body["publishing_enabled"] is True
    assert body["autopublish_enabled"] is True
    assert body["public_enabled"] is False
    assert body["ready_jobs"] == 1
    assert body["daily_cap"] >= 1


def test_enabling_a_target_for_autopublish_stamps_the_cutoff(client, db):
    from app.models.publish_target import PublishTarget

    target = make_target(db)
    assert target.autopublish_enabled is False

    body = client.put(
        f"/admin/publish-targets/{target.id}", json={"autopublish_enabled": True}
    ).json()

    assert body["autopublish_enabled"] is True
    assert body["autopublish_enabled_at"], "the cutoff is stamped on the transition"
    fresh = db.query(PublishTarget).filter(PublishTarget.id == target.id).one()
    assert fresh.autopublish_enabled_at is not None


def test_a_disconnected_target_cannot_be_enabled_for_autopublish(client, db):
    target = make_target(db, refresh_token_encrypted=None)
    response = client.put(
        f"/admin/publish-targets/{target.id}", json={"autopublish_enabled": True}
    )
    assert response.status_code == 409


def test_the_cutoff_is_not_moved_by_a_second_enable(client, db):
    target = autopublish_target(db)
    original = target.autopublish_enabled_at

    client.put(f"/admin/publish-targets/{target.id}", json={"autopublish_enabled": True})

    db.refresh(target)
    # Compared without tzinfo: SQLite returns naive datetimes where PostgreSQL returns
    # aware ones, and the point of the assertion is the instant, not the representation.
    assert target.autopublish_enabled_at.replace(tzinfo=None) == original.replace(
        tzinfo=None
    ), "re-confirming must not reopen the backlog"
