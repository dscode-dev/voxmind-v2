"""PR-PUBLISH-COMPLETE-01 — what a run OWES, versus what it has attempted.

The whole PR turns on one distinction. `test_two_of_four_succeeded_is_not_published` is the
regression that names it: before this, a four-clip run with two successful attempts satisfied
`all existing attempts succeeded` and was marked PUBLISHED with two videos never uploaded and
nothing left to say so.

Everything else here is that distinction applied — to partial progress, to blocked items, to
budgets spread across days, and to a manual publish of a subset.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.settings import settings
from app.models.enums import PipelineState, PublishAttemptStatus
from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt
from app.publishing.manifest import (
    LEGACY_VERSION,
    MANIFEST_KEY,
    MANIFEST_VERSION,
    SOURCE_LEGACY_ATTEMPTS,
    SOURCE_PACKAGE,
    ManifestService,
    ManifestUnavailableError,
)
from app.services.autopublish_budget import AutopublishBudget, utc_today
from app.services.publication_completion import (
    BLOCKED,
    COMPLETE,
    IN_PROGRESS,
    NOT_STARTED,
    PARTIAL,
    UNRESOLVED,
    PublicationCompletionEvaluator,
)
from app.services.publishing_service import PublishingService, idempotency_key
from tests.test_autopublish import (  # noqa: F401 - fixtures used by pytest
    ALIVE,
    attempts_count,
    autopublish_config,
    autopublish_target,
    make_ready_job,
    make_topic,
    policy,
    queue,
)
from tests.test_autonomy_harden import _drain, _KeepOpen  # noqa: F401
from tests.test_publishing import (  # noqa: F401 - publishing_config is autouse
    StubArtifacts,
    StubMediaSource,
    StubPublisher,
    make_target,
    publishing_config,
)


# ===========================================================================
# Helpers
# ===========================================================================


def service(queue, publisher=None, artifacts=None, db=None) -> PublishingService:
    return PublishingService(
        publisher=publisher or StubPublisher(),
        artifacts=artifacts or StubArtifacts(videos=4),
        media_source=StubMediaSource(),
        queue=queue,
        session_factory=(lambda: _KeepOpen(db)) if db is not None else None,
    )


def manifest_for(db, job, videos: int = 4):
    return ManifestService(artifacts=StubArtifacts(videos=videos)).resolve(db, job)


def attempt_for(db, job, target, index: int, status, **overrides) -> PublishAttempt:
    identity = f"final_clips/final_clip_{index:02d}.mp4"
    fields = dict(
        pipeline_job_id=job.id, target_id=target.id,
        idempotency_key=idempotency_key(job.id, target.id, identity),
        media_identity=identity, media_storage_key=f"jobs/x/{identity}",
        media_bytes=1024, status=status, attempt_no=1, max_attempts=3,
        initiator="automatic", budget_date=utc_today(),
        payload_json={"video_index": index},
    )
    if status == PublishAttemptStatus.SUCCEEDED:
        fields["external_id"] = f"vid_{index}"
    fields.update(overrides)
    attempt = PublishAttempt(**fields)
    db.add(attempt)
    db.flush()
    return attempt


def evaluate(db, job, target, videos: int = 4):
    return PublicationCompletionEvaluator().evaluate(
        db, job, manifest=manifest_for(db, job, videos), target_id=target.id
    )


# ===========================================================================
# The manifest
# ===========================================================================


def test_the_manifest_lists_only_generated_outputs(db, no_event_fanout):
    """A clip that never rendered is not something to wait for for ever."""
    artifacts = StubArtifacts(videos=3)
    artifacts.package["videos"][1]["final_clip"]["status"] = "missing"
    job = make_ready_job(db)

    manifest = ManifestService(artifacts=artifacts).resolve(db, job)

    assert [item.video_index for item in manifest.ordered()] == [1, 3]
    assert manifest.version == MANIFEST_VERSION
    assert manifest.source == SOURCE_PACKAGE


def test_the_manifest_is_written_once_and_never_moves(db, no_event_fanout):
    """A later re-render must not redefine what this run was supposed to do."""
    artifacts = StubArtifacts(videos=2)
    job = make_ready_job(db)
    first = ManifestService(artifacts=artifacts).resolve(db, job)

    # The upstream artifact grows a third clip after the fact.
    artifacts.package["videos"].append({
        "video_index": 3,
        "post": {"title": "late", "description": "d", "hashtags": []},
        "final_clip": {"status": "generated", "file_name": "final_clip_03.mp4"},
    })
    second = ManifestService(artifacts=artifacts).resolve(db, job)

    assert len(first.items) == len(second.items) == 2
    assert second.created_at == first.created_at


def test_the_manifest_is_persisted_on_the_run(db, no_event_fanout):
    job = make_ready_job(db)
    ManifestService(artifacts=StubArtifacts(videos=2)).resolve(db, job)

    stored = (job.metadata_json or {}).get(MANIFEST_KEY)
    assert stored["version"] == MANIFEST_VERSION
    assert len(stored["items"]) == 2
    assert all(item["required"] for item in stored["items"])


def test_media_identity_maps_deterministically_to_an_attempt(db, no_event_fanout):
    job = make_ready_job(db)
    manifest = ManifestService(artifacts=StubArtifacts(videos=2)).resolve(db, job)

    assert manifest.identities() == [
        "final_clips/final_clip_01.mp4", "final_clips/final_clip_02.mp4"
    ]
    # The same rule the publishing service uses to name an attempt's media.
    items = service(None, artifacts=StubArtifacts(videos=2)).resolve_media(db, job)
    assert [item.identity for item in items] == manifest.identities()


def test_an_unreadable_package_with_no_history_blocks_rather_than_completes(
    db, no_event_fanout
):
    """Fail-closed: an empty manifest would read as 'everything required is done'."""

    class NoArtifacts:
        def load_json(self, key):
            return None

    job = make_ready_job(db)
    with pytest.raises(ManifestUnavailableError):
        ManifestService(artifacts=NoArtifacts()).resolve(db, job)

    assert (job.metadata_json or {}).get(MANIFEST_KEY) is None


def test_a_legacy_run_derives_its_manifest_from_what_it_attempted(db, no_event_fanout):
    """Rows created before this feature keep exactly the behaviour they were made under."""

    class NoArtifacts:
        def load_json(self, key):
            return None

    target = autopublish_target(db)
    job = make_ready_job(db)
    attempt_for(db, job, target, 1, PublishAttemptStatus.SUCCEEDED)
    attempt_for(db, job, target, 2, PublishAttemptStatus.SUCCEEDED)

    manifest = ManifestService(artifacts=NoArtifacts()).resolve(db, job)

    assert manifest.version == LEGACY_VERSION
    assert manifest.source == SOURCE_LEGACY_ATTEMPTS
    assert len(manifest.items) == 2
    assert manifest.is_legacy


def test_a_legacy_run_with_all_attempts_succeeded_is_complete(db, no_event_fanout):
    """Compatibility, stated explicitly: those rows had no other definition of done."""

    class NoArtifacts:
        def load_json(self, key):
            return None

    target = autopublish_target(db)
    job = make_ready_job(db)
    attempt_for(db, job, target, 1, PublishAttemptStatus.SUCCEEDED)

    manifest = ManifestService(artifacts=NoArtifacts()).resolve(db, job)
    result = PublicationCompletionEvaluator().evaluate(
        db, job, manifest=manifest, target_id=target.id
    )
    assert result.status == COMPLETE


# ===========================================================================
# The core regression
# ===========================================================================


def test_two_of_four_succeeded_is_not_published(db, no_event_fanout):
    """THE regression. This exact shape used to be marked PUBLISHED."""
    target = autopublish_target(db)
    job = make_ready_job(db)
    attempt_for(db, job, target, 1, PublishAttemptStatus.SUCCEEDED)
    attempt_for(db, job, target, 2, PublishAttemptStatus.SUCCEEDED)

    result = evaluate(db, job, target)

    assert result.required_count == 4
    assert result.succeeded_count == 2
    assert result.missing_count == 2
    assert result.is_complete is False
    assert result.status == PARTIAL
    assert [item.video_index for item in result.outstanding_items] == [3, 4]


def test_all_four_succeeded_is_complete(db, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db)
    for index in range(1, 5):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)

    result = evaluate(db, job, target)

    assert result.status == COMPLETE
    assert result.is_complete is True
    assert sorted(result.external_ids()) == ["vid_1", "vid_2", "vid_3", "vid_4"]


def test_no_attempts_at_all_is_not_started(db, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db)

    result = evaluate(db, job, target)

    assert result.status == NOT_STARTED
    assert result.missing_count == 4
    assert result.is_complete is False


def test_an_empty_manifest_never_reads_as_complete(db, no_event_fanout):
    from app.publishing.manifest import PublicationManifest

    target = autopublish_target(db)
    job = make_ready_job(db)
    empty = PublicationManifest(version=1, source="test", items=(), created_at="")

    result = PublicationCompletionEvaluator().evaluate(
        db, job, manifest=empty, target_id=target.id
    )
    assert result.is_complete is False
    assert result.status == NOT_STARTED


# ===========================================================================
# Per-status semantics
# ===========================================================================


def test_an_active_item_makes_the_run_in_progress(db, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db)
    for index in (1, 2, 3):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)
    attempt_for(db, job, target, 4, PublishAttemptStatus.IN_PROGRESS)

    result = evaluate(db, job, target)

    assert result.status == IN_PROGRESS
    assert result.in_flight_count == 1
    assert result.has_active_work is True


def test_a_retry_still_coming_is_active_and_not_outstanding(db, no_event_fanout):
    """§24: the queue owns the retry, so nothing may create a replacement."""
    target = autopublish_target(db)
    job = make_ready_job(db)
    for index in (1, 2, 3):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)
    attempt_for(db, job, target, 4, PublishAttemptStatus.FAILED_RETRYABLE,
                attempt_no=1, max_attempts=3)

    result = evaluate(db, job, target)

    assert result.status == IN_PROGRESS
    assert result.retryable_count == 1
    assert result.missing_count == 0, "not missing - it has an attempt"
    assert result.outstanding_items == []


def test_a_retry_with_no_budget_left_stops_counting_as_active(db, no_event_fanout):
    """Otherwise the run sits in PUBLISHING for ever with nothing running."""
    target = autopublish_target(db)
    job = make_ready_job(db)
    for index in (1, 2, 3):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)
    attempt_for(db, job, target, 4, PublishAttemptStatus.FAILED_RETRYABLE,
                attempt_no=3, max_attempts=3)

    result = evaluate(db, job, target)

    assert result.has_active_work is False
    assert result.status == BLOCKED
    assert result.exhausted_count == 1


def test_an_unresolved_item_blocks_and_outranks_everything(db, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db)
    for index in (1, 2, 3):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)
    attempt_for(db, job, target, 4, PublishAttemptStatus.UNKNOWN)

    result = evaluate(db, job, target)

    assert result.status == UNRESOLVED
    assert result.is_complete is False
    assert result.outstanding_items == [], "no replacement may be created"


def test_a_final_failure_blocks_completion(db, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db)
    for index in (1, 2, 3):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)
    attempt_for(db, job, target, 4, PublishAttemptStatus.FAILED_FINAL)

    result = evaluate(db, job, target)

    assert result.status == BLOCKED
    assert result.final_failed_count == 1
    assert result.is_complete is False


def test_a_canceled_item_is_an_operator_veto_not_a_removal(db, no_event_fanout):
    """§27: cancelling does not make the item stop being required."""
    target = autopublish_target(db)
    job = make_ready_job(db)
    for index in (1, 2, 3):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)
    attempt_for(db, job, target, 4, PublishAttemptStatus.CANCELED)

    result = evaluate(db, job, target)

    assert result.status == BLOCKED
    assert result.canceled_count == 1
    assert result.required_count == 4, "still required"
    assert result.is_complete is False


# ===========================================================================
# Scoping
# ===========================================================================


def test_completion_is_scoped_to_one_target(db, no_event_fanout):
    """A publication to channel B cannot satisfy channel A's manifest."""
    target_a = autopublish_target(db, channel_id="UC_a", name="A")
    target_b = autopublish_target(db, channel_id="UC_b", name="B")
    job = make_ready_job(db)

    for index in range(1, 5):
        attempt_for(db, job, target_b, index, PublishAttemptStatus.SUCCEEDED)

    assert evaluate(db, job, target_b).status == COMPLETE
    assert evaluate(db, job, target_a).status == NOT_STARTED


def test_who_published_does_not_change_completion(db, no_event_fanout):
    """§21: manual and automatic successes are both external successes."""
    target = autopublish_target(db)
    job = make_ready_job(db)
    attempt_for(db, job, target, 1, PublishAttemptStatus.SUCCEEDED,
                initiator="manual", budget_date=None)
    for index in (2, 3, 4):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)

    result = evaluate(db, job, target)

    assert result.status == COMPLETE
    # ...but only the three automatic rows spent budget.
    assert AutopublishBudget(db, limit=10).used() == 3


# ===========================================================================
# The run's state follows completion
# ===========================================================================


def test_a_partially_published_run_returns_to_ready(db, queue, no_event_fanout):
    """§14: neither PUBLISHED nor permanently PUBLISHING."""
    target = autopublish_target(db)
    job = make_ready_job(db)
    svc = service(queue, db=db)
    for index in (1, 2):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)
    job.state = PipelineState.PUBLISHING
    db.flush()

    result = svc._settle_job(db, job, target=target)

    assert result.status == PARTIAL
    assert job.state == PipelineState.READY_TO_PUBLISH
    assert job.state != PipelineState.PUBLISHED


def test_a_complete_run_reaches_published(db, queue, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db)
    svc = service(queue, db=db)
    for index in range(1, 5):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)
    job.state = PipelineState.PUBLISHING
    db.flush()

    svc._settle_job(db, job, target=target)
    assert job.state == PipelineState.PUBLISHED


def test_a_run_with_live_work_stays_publishing(db, queue, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db)
    svc = service(queue, db=db)
    for index in (1, 2, 3):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)
    attempt_for(db, job, target, 4, PublishAttemptStatus.PENDING)
    job.state = PipelineState.PUBLISHING
    db.flush()

    svc._settle_job(db, job, target=target)
    assert job.state == PipelineState.PUBLISHING


def test_first_ready_at_survives_a_partial_cycle(db, queue, no_event_fanout):
    """§32: the historical cutoff must not be reopened by ordinary progress."""
    ready_at = datetime.now(timezone.utc) - timedelta(days=5)
    target = autopublish_target(db)
    job = make_ready_job(db, ready_at=ready_at)
    original = job.metadata_json["first_ready_at"]
    svc = service(queue, db=db)

    for index in (1, 2):
        attempt_for(db, job, target, index, PublishAttemptStatus.SUCCEEDED)
    job.state = PipelineState.PUBLISHING
    db.flush()
    svc._settle_job(db, job, target=target)

    assert job.state == PipelineState.READY_TO_PUBLISH
    assert job.metadata_json["first_ready_at"] == original


# ===========================================================================
# Safe partial allocation
# ===========================================================================


def test_a_budget_smaller_than_the_run_allocates_what_it_can(db, queue, monkeypatch,
                                                              no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_max_per_day", 2, raising=False)
    target = autopublish_target(db)
    make_ready_job(db, make_topic(db, target=target))

    report = policy(queue, artifacts=StubArtifacts(videos=4)).run(db, dry_run=False)

    assert attempts_count(db) == 2
    assert report.candidates[0].deferred == 2
    identities = sorted(a.media_identity for a in db.query(PublishAttempt).all())
    assert identities == [
        "final_clips/final_clip_01.mp4", "final_clips/final_clip_02.mp4"
    ], "deterministic: the earlier clips first"


def test_the_run_finishes_on_a_later_budget_and_never_republishes(
    db, queue, monkeypatch, no_event_fanout
):
    """Day 1 takes two clips, day 2 the rest, and then the run is PUBLISHED."""
    monkeypatch.setattr(settings, "autopublish_max_per_day", 2, raising=False)
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    svc = policy(queue, artifacts=StubArtifacts(videos=4))

    svc.run(db, dry_run=False)
    _drain(db, queue, svc)
    assert attempts_count(db) == 2
    assert job.state != PipelineState.PUBLISHED

    # A new UTC day restores the allowance.
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    svc.clock = lambda: tomorrow
    svc.run(db, dry_run=False)
    _drain(db, queue, svc)

    db.expire_all()
    assert attempts_count(db) == 4, "no clip was published twice"
    fresh = db.query(PipelineJob).filter(PipelineJob.id == job.id).one()
    assert fresh.state == PipelineState.PUBLISHED


def test_a_per_tick_cap_of_one_advances_one_clip_at_a_time(db, queue, monkeypatch,
                                                             no_event_fanout):
    monkeypatch.setattr(settings, "autopublish_max_per_tick", 1, raising=False)
    monkeypatch.setattr(settings, "autopublish_max_per_day", 50, raising=False)
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    svc = policy(queue, artifacts=StubArtifacts(videos=4))

    progress = []
    for _ in range(4):
        svc.run(db, dry_run=False)
        _drain(db, queue, svc)
        db.expire_all()
        progress.append(attempts_count(db))

    assert progress == [1, 2, 3, 4]
    fresh = db.query(PipelineJob).filter(PipelineJob.id == job.id).one()
    assert fresh.state == PipelineState.PUBLISHED


def test_successes_are_never_offered_for_allocation_again(db, queue, no_event_fanout):
    target = autopublish_target(db)
    job = make_ready_job(db, make_topic(db, target=target))
    attempt_for(db, job, target, 1, PublishAttemptStatus.SUCCEEDED)
    attempt_for(db, job, target, 3, PublishAttemptStatus.SUCCEEDED)

    svc = policy(queue, artifacts=StubArtifacts(videos=4))
    outstanding = svc._outstanding_media(db, job, target)

    assert outstanding == [2, 4]


# ===========================================================================
# Manual subset
# ===========================================================================


def test_a_manual_subset_does_not_finish_the_run(db, queue, no_event_fanout):
    """§20: publishing clips 1 and 2 by hand leaves 3 and 4 owed."""
    target = make_target(db)
    job = make_ready_job(db)
    publisher = StubPublisher()
    svc = service(queue, publisher=publisher, db=db)

    report = svc.publish(db, job=job, target=target, dry_run=False,
                         media_selection=[1, 2])
    _drain(db, queue, svc)

    db.expire_all()
    fresh = db.query(PipelineJob).filter(PipelineJob.id == job.id).one()
    result = PublicationCompletionEvaluator().evaluate(
        db, fresh, manifest=manifest_for(db, fresh), target_id=target.id
    )

    assert attempts_count(db) == 2
    assert result.required_count == 4
    assert result.succeeded_count == 2
    assert result.status == PARTIAL
    assert fresh.state != PipelineState.PUBLISHED
    assert [item.video_index for item in result.outstanding_items] == [3, 4]
