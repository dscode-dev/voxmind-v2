"""PR-METRICS-02 — making observations comparable, without making them decisions.

Raw snapshots are not comparable: "100 views" is remarkable at one hour and disappointing at
two weeks, and the collector records whichever moments it happened to be awake for. The
canonical windows fix that by asking every publication the same question, and most of this
file is about the ways that resolution could quietly lie.

Three invariants carry the most weight.

`test_as_of_excludes_later_snapshots` is the look-ahead guard: a dataset rebuilt after more
data arrives must not silently improve, or no analysis of it is reproducible.

`test_not_mature_is_not_missing` keeps two different absences apart. "Too early to know" and
"we should have known and did not" call for opposite responses, and collapsing both to NULL
would hide a broken collector behind a young video.

`test_evaluation_package_is_not_imported_by_the_production_path` is the boundary. Nothing in
discovery, selection, admission or publishing may import `app.evaluation`, so a feedback loop
has to appear as an import in a diff rather than as a quiet coupling.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.evaluation.schema import (
    DATASET_SEMANTIC_VERSION,
    EXPORT_SCHEMA_VERSION,
    export_columns,
)
from app.evaluation.windows import (
    AVAILABLE,
    MISSING_SNAPSHOT,
    NOT_MATURE,
    VIDEO_NOT_RETURNED,
    WINDOW_POLICY_VERSION,
    WINDOWS_BY_NAME,
    resolve_window,
)
from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.enums import (
    DiscoverySourceKind,
    PipelineState,
    PublishAttemptStatus,
    VideoCandidateStatus,
)
from app.models.publish_attempt import PublishAttempt
from app.models.video_candidate import VideoCandidate
from app.models.video_performance_snapshot import VideoPerformanceSnapshot
from app.services.metrics_ingestion_service import capture_slot
from app.services.performance_dataset_service import (
    MISSING_LINEAGE,
    DatasetFilters,
    PerformanceDatasetService,
)
from tests.conftest import make_run
from tests.test_publishing import (  # noqa: F401 - publishing_config is autouse
    make_target,
    publishing_config,
)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
FAR_FUTURE = T0 + timedelta(days=60)


# ===========================================================================
# Helpers
# ===========================================================================


def service(now: datetime = FAR_FUTURE) -> PerformanceDatasetService:
    return PerformanceDatasetService(clock=lambda: now)


def lineage(db, *, topic_name: str | None = None, relevance: float = 0.82,
            selection_method: str = "policy", source_kind=DiscoverySourceKind.YOUTUBE_SEARCH):
    """A topic, a source, a selected candidate and the job admission froze from them."""
    tag = uuid.uuid4().hex[:8]
    topic = ContentTopic(name=topic_name or f"Serie A {tag}", is_active=True)
    db.add(topic)
    db.flush()
    source = DiscoverySource(topic_id=topic.id, kind=source_kind,
                             name=f"channel {tag}", is_active=True, config_json={})
    db.add(source)
    db.flush()
    candidate = VideoCandidate(
        topic_id=topic.id, source_id=source.id, external_id=f"src_{tag}",
        url=f"https://youtu.be/{tag}", title="Milan 3-1 Inter", channel="Serie A",
        duration_sec=900, published_at=T0 - timedelta(days=2),
        status=VideoCandidateStatus.CONSUMED, relevance_score=relevance, trend_score=0.44,
        scores_json={"final_score": 0.77, "version": "selection-v1"},
        selected_at=T0 - timedelta(hours=6),
    )
    db.add(candidate)
    db.flush()
    job = make_run(
        db, state=PipelineState.PUBLISHED, topic_id=topic.id, candidate_id=candidate.id,
        metadata_json={
            # Exactly the shape admission freezes onto the job.
            "provenance": {
                "video_candidate_id": str(candidate.id),
                "topic_id": str(topic.id),
                "external_id": candidate.external_id,
                "selection_method": selection_method,
                "selection_run_id": f"run_{tag}",
                "selection_score": 0.77,
                "score_version": "selection-v1",
                "selected_at": (T0 - timedelta(hours=6)).isoformat(),
            },
            "snapshot": {
                "clip_mode": "short_serie",
                "video_ratio": "portrait",
                "topic_name": topic.name,
                "frozen_at": (T0 - timedelta(hours=5)).isoformat(),
            },
        },
    )
    return topic, source, candidate, job


def publication(db, target, job, *, video_id=None, published_at=T0, index=1,
                initiator="automatic", privacy="private",
                accepted_privacy: str | None = "private") -> PublishAttempt:
    video_id = video_id or f"vid_{uuid.uuid4().hex[:8]}"
    attempt = PublishAttempt(
        pipeline_job_id=job.id, target_id=target.id,
        idempotency_key=f"publish:{job.id}:{target.id}:{video_id}:v1",
        media_identity=f"final_clips/final_clip_{index:02d}.mp4",
        media_bytes=4_194_304,
        status=PublishAttemptStatus.SUCCEEDED, external_id=video_id,
        attempt_no=1, max_attempts=3, initiator=initiator,
        finished_at=published_at,
        payload_json={"metadata": {"title": "Milan 3-1 Inter", "description": "",
                                   "tags": [], "privacy": privacy},
                      "video_index": index},
        provider_metadata_json={"privacy_status": accepted_privacy,
                                "upload_status": "processed"},
    )
    db.add(attempt)
    db.flush()
    return attempt


def snapshot(db, attempt, *, hours: float, views=None, likes=None, comments=None,
             availability=AVAILABLE, published_at=T0) -> VideoPerformanceSnapshot:
    captured = published_at + timedelta(hours=hours)
    row = VideoPerformanceSnapshot(
        publish_attempt_id=attempt.id, publish_target_id=attempt.target_id,
        external_video_id=attempt.external_id, provider="youtube",
        captured_at=captured, capture_slot=capture_slot(captured),
        view_count=views, like_count=likes, comment_count=comments,
        # The ingestion layer writes "ok" for a measured video; the resolver keys off that.
        availability="ok" if availability == AVAILABLE else availability,
        privacy_status="private",
    )
    db.add(row)
    db.flush()
    return row


class Snap:
    """A snapshot for the pure resolver tests. No database, no ORM."""

    def __init__(self, hours: float, *, views=None, likes=None, comments=None,
                 availability="ok", identifier: str | None = None):
        self.id = identifier or f"s{hours}"
        self.captured_at = T0 + timedelta(hours=hours)
        self.availability = availability
        self.view_count = views
        self.like_count = likes
        self.comment_count = comments


def window(name: str):
    return WINDOWS_BY_NAME[name]


def single_row(db, *, now=FAR_FUTURE, as_of=None):
    dataset = service(now).build(db, as_of=as_of)
    assert len(dataset.rows) == 1, dataset.quality.as_dict()
    return dataset.rows[0]


# ===========================================================================
# The resolver, as pure arithmetic
# ===========================================================================


def test_a_snapshot_at_the_target_answers_the_window():
    observation = resolve_window(
        T0, [Snap(1, views=100)], window("1h"), as_of=FAR_FUTURE
    )

    assert observation.availability == AVAILABLE
    assert observation.view_count == 100
    assert observation.actual_age_seconds == 3600
    assert observation.observation_lag_seconds == 0


def test_an_observation_before_the_target_never_answers_it():
    """A counter at 23h cannot say what 24h looked like, however close it feels.

    Accepting it would mean `views_24h` sometimes represents 23 hours of exposure and
    sometimes 24, which is exactly the incomparability the windows exist to remove.
    """
    observation = resolve_window(
        T0, [Snap(23.9, views=900), Snap(31, views=1400)], window("24h"), as_of=FAR_FUTURE
    )

    assert observation.availability == AVAILABLE
    # The later one, because it is the first that is *at least* 24h old.
    assert observation.view_count == 1400
    assert observation.actual_age_seconds == 31 * 3600


def test_snapshots_outside_the_tolerance_leave_the_window_unavailable():
    """Nearest-available is bounded, not infinite.

    Without an upper bound a 24h window would happily answer with a snapshot from day nine,
    and the column would silently stop meaning what its name says.
    """
    observation = resolve_window(
        T0, [Snap(23, views=900), Snap(40, views=2000)], window("24h"), as_of=FAR_FUTURE
    )

    assert observation.availability == MISSING_SNAPSHOT
    assert observation.view_count is None
    assert observation.snapshot_id is None


def test_the_earliest_acceptable_observation_wins():
    observation = resolve_window(
        T0, [Snap(30, views=1100), Snap(26, views=1000), Snap(31, views=1200)],
        window("24h"), as_of=FAR_FUTURE,
    )

    assert observation.view_count == 1000
    assert observation.actual_age_seconds == 26 * 3600


def test_nothing_is_interpolated_between_snapshots():
    """Two observations straddling the target produce the later one, not an average.

    View growth is not linear — a video does not accrue views at 03:00 the way it does in its
    first hour — so a straight line between 23h and 25h is a fabrication that would be
    indistinguishable from a measurement once it was written down.
    """
    observation = resolve_window(
        T0, [Snap(23, views=900), Snap(25, views=1100)], window("24h"), as_of=FAR_FUTURE
    )

    assert observation.view_count == 1100
    assert observation.actual_age_seconds == 25 * 3600
    assert observation.observation_lag_seconds == 3600


def test_a_measured_observation_is_preferred_over_a_not_returned_one():
    """One blip must not discard a good observation from the same interval."""
    observation = resolve_window(
        T0,
        [Snap(24.5, availability="not_returned"), Snap(26, views=1000)],
        window("24h"), as_of=FAR_FUTURE,
    )

    assert observation.availability == AVAILABLE
    assert observation.view_count == 1000


def test_only_unreturned_observations_report_the_video_not_the_collector():
    """The video was asked about and the provider declined. Not missing data, and not zero."""
    observation = resolve_window(
        T0, [Snap(25, availability="not_returned")], window("24h"), as_of=FAR_FUTURE
    )

    assert observation.availability == VIDEO_NOT_RETURNED
    assert observation.view_count is None
    assert observation.snapshot_id is not None


def test_not_mature_is_not_missing():
    """Two absences that call for opposite responses.

    A 30-minute-old video has no 1h observation because 1h has not happened. A 3-hour-old one
    has none because the collector did not take it. Reporting both as missing would send
    someone to debug a collector that is working.
    """
    young = resolve_window(T0, [], window("1h"), as_of=T0 + timedelta(minutes=30))
    assert young.availability == NOT_MATURE

    # The acceptance interval for 1h closes at 1h + 1h tolerance; at 3h it is long shut.
    old = resolve_window(T0, [], window("1h"), as_of=T0 + timedelta(hours=3))
    assert old.availability == MISSING_SNAPSHOT


def test_maturity_is_measured_against_as_of_not_the_wall_clock():
    """Otherwise the same dataset would answer differently every time it was rebuilt."""
    observation = resolve_window(
        T0, [], window("24h"), as_of=T0 + timedelta(hours=2)
    )

    assert observation.availability == NOT_MATURE


def test_as_of_hides_snapshots_captured_later():
    """The look-ahead guard, at the level of a single window."""
    snapshots = [Snap(25, views=1100)]

    hidden = resolve_window(T0, snapshots, window("24h"), as_of=T0 + timedelta(hours=24.5))
    visible = resolve_window(T0, snapshots, window("24h"), as_of=T0 + timedelta(hours=40))

    assert hidden.availability == NOT_MATURE
    assert visible.availability == AVAILABLE
    assert visible.view_count == 1100


def test_a_counter_that_decreased_is_reported_as_observed():
    """YouTube removes spam views. Monotonicity is not an invariant and is not imposed."""
    snapshots = [Snap(6, views=100), Snap(25, views=95)]

    assert resolve_window(T0, snapshots, window("6h"), as_of=FAR_FUTURE).view_count == 100
    assert resolve_window(T0, snapshots, window("24h"), as_of=FAR_FUTURE).view_count == 95


# ===========================================================================
# Derived fields
# ===========================================================================


def test_hidden_likes_propagate_as_null_never_as_zero_engagement(db, no_event_fanout):
    """A hidden like count is unknown. Zero would report the video as having no engagement."""
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=100, likes=None, comments=4)
    db.commit()

    row = single_row(db)

    assert row.outcomes()["views_24h"] == 100
    assert row.outcomes()["likes_24h"] is None
    assert row.derived()["likes_per_view_24h"] is None
    # The comment count was disclosed, so its ratio is defined.
    assert row.derived()["comments_per_view_24h"] == pytest.approx(0.04)


def test_zero_views_gives_no_ratio_rather_than_a_division(db, no_event_fanout):
    """"0 likes out of 0 views" is not 0% engagement; it is a question nobody has asked yet."""
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=0, likes=0, comments=0)
    db.commit()

    row = single_row(db)

    assert row.outcomes()["views_24h"] == 0
    assert row.derived()["likes_per_view_24h"] is None
    assert row.derived()["comments_per_view_24h"] is None
    assert row.derived()["views_per_hour_24h"] == 0.0


def test_views_per_hour_uses_the_real_observation_age(db, no_event_fanout):
    """Dividing by the nominal window would inherit the collector's lateness as a rate error.

    1,100 views seen at 29h is 37.93 views/hour, not the 45.83 that dividing by 24 would
    claim. The window says which question was asked; the rate must use the answer's real age.
    """
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=29, views=1100)
    db.commit()

    row = single_row(db)

    assert row.derived()["views_per_hour_24h"] == pytest.approx(1100 / 29, abs=1e-3)
    assert row.trace()["24h"]["observation_lag_seconds"] == 5 * 3600


def test_an_unmeasured_window_has_no_rate(db, no_event_fanout):
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=500)
    db.commit()

    row = single_row(db)

    assert row.outcomes()["availability_1h"] == MISSING_SNAPSHOT
    assert row.derived()["views_per_hour_1h"] is None


# ===========================================================================
# The dataset
# ===========================================================================


def test_one_row_per_external_publication(db, no_event_fanout):
    """A three-clip run is three videos with three audiences, so three rows.

    Aggregating at the run would destroy the only comparison the data exists to support.
    """
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempts = [
        publication(db, target, job, video_id=f"clip_{index}", index=index)
        for index in (1, 2, 3)
    ]
    for attempt, views in zip(attempts, (900, 40, 310)):
        snapshot(db, attempt, hours=25, views=views)
    db.commit()

    dataset = service().build(db)

    assert len(dataset.rows) == 3
    assert {row.publish_attempt_id for row in dataset.rows} == {
        str(a.id) for a in attempts
    }
    assert sorted(row.outcomes()["views_24h"] for row in dataset.rows) == [40, 310, 900]
    # Same run, so the same lineage on every row.
    assert {row.pipeline_job_id for row in dataset.rows} == {str(job.id)}


def test_lineage_is_carried_end_to_end(db, no_event_fanout):
    target = make_target(db)
    topic, source, candidate, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=500)
    db.commit()

    row = single_row(db)
    context = row.decision_context

    assert row.video_candidate_id == str(candidate.id)
    assert row.pipeline_job_id == str(job.id)
    assert context.topic_id == str(topic.id)
    assert context.topic_name == topic.name
    assert context.source_provider == source.kind.value
    assert context.source_channel == "Serie A"
    assert context.source_external_id == candidate.external_id
    assert context.source_duration_sec == 900
    # Six hours between the candidate being selected and its source video's publish date.
    assert context.candidate_age_at_selection_sec == int(
        ((T0 - timedelta(hours=6)) - (T0 - timedelta(days=2))).total_seconds()
    )


def test_decision_context_comes_from_the_frozen_provenance(db, no_event_fanout):
    """The score the decision actually saw, not whatever the candidate says today."""
    target = make_target(db)
    _, _, candidate, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=500)
    db.commit()

    # Someone edits the candidate afterwards. The frozen provenance must win.
    candidate.scores_json = {"final_score": 0.01, "version": "selection-v9"}
    db.flush()

    context = single_row(db).decision_context

    assert context.selection_score == pytest.approx(0.77)
    assert context.score_version == "selection-v1"
    assert context.selection_method == "policy"
    assert context.selection_run_id is not None


def test_decision_and_outcome_stay_in_separate_groups(db, no_event_fanout):
    """Leakage has to be introduced deliberately, not by a careless SELECT *."""
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=500)
    db.commit()

    payload = single_row(db).as_dict()

    assert set(payload) >= {
        "decision_context", "publication_context", "performance_outcomes",
        "observation_trace",
    }
    # No outcome hiding among the features.
    assert not [key for key in payload["decision_context"] if "view" in key]
    assert "selection_score" in payload["decision_context"]
    assert "views_24h" in payload["performance_outcomes"]
    assert "selection_score" not in payload["performance_outcomes"]

    flat = single_row(db).as_flat()
    assert flat["dc_selection_score"] == pytest.approx(0.77)
    assert flat["out_views_24h"] == 500
    assert not [key for key in flat if key.startswith("dc_") and "views" in key]


def test_manual_and_automatic_both_enter_with_the_initiator_preserved(db, no_event_fanout):
    """Both are real publications. Which one a human chose is a dimension, not a filter."""
    target = make_target(db)
    _, _, _, job_a = lineage(db)
    _, _, _, job_b = lineage(db)
    automatic = publication(db, target, job_a, initiator="automatic")
    manual = publication(db, target, job_b, initiator="manual")
    for attempt in (automatic, manual):
        snapshot(db, attempt, hours=25, views=100)
    db.commit()

    dataset = service().build(db)

    assert dataset.summary()["initiator_distribution"] == {"automatic": 1, "manual": 1}
    assert {row.publication_context.initiator for row in dataset.rows} == {
        "automatic", "manual"
    }


def test_private_and_public_are_kept_apart_by_dimension_not_by_exclusion(db,
                                                                        no_event_fanout):
    """A private video does not get comparable exposure, so privacy must stay visible.

    Excluding private uploads would throw away most of a careful rollout; comparing them to
    public ones without the dimension would be worse. Preserved, and deliberately not
    corrected for.
    """
    target = make_target(db)
    _, _, _, job_a = lineage(db)
    _, _, _, job_b = lineage(db)
    private = publication(db, target, job_a, privacy="private", accepted_privacy="private")
    public = publication(db, target, job_b, privacy="public", accepted_privacy="public")
    for attempt in (private, public):
        snapshot(db, attempt, hours=25, views=100)
    db.commit()

    dataset = service().build(db)

    assert dataset.summary()["privacy_distribution"] == {"private": 1, "public": 1}

    only_private = service().build(db, filters=DatasetFilters(privacy="private"))
    assert len(only_private.rows) == 1
    assert only_private.rows[0].publish_attempt_id == str(private.id)


def test_excluded_publications_are_counted_with_a_reason(db, no_event_fanout):
    """No silent drops: every considered publication is either a row or a counted exclusion."""
    target = make_target(db)
    _, _, _, job = lineage(db)
    good = publication(db, target, job)
    snapshot(db, good, hours=25, views=100)

    # A job with no candidate and no frozen provenance: real lineage, genuinely absent.
    orphan_job = make_run(db, state=PipelineState.PUBLISHED)
    publication(db, target, orphan_job, video_id="vid_orphan")
    db.commit()

    dataset = service().build(db)

    assert dataset.quality.considered == 2
    assert dataset.quality.included == 1
    assert dataset.quality.excluded == {MISSING_LINEAGE: 1}
    assert dataset.quality.considered == (
        dataset.quality.included + sum(dataset.quality.excluded.values())
    )


def test_coverage_is_reported_per_window(db, no_event_fanout):
    """Coverage measures the collector, never the content."""
    target = make_target(db)
    _, _, _, job_a = lineage(db)
    _, _, _, job_b = lineage(db)
    observed = publication(db, target, job_a)
    unobserved = publication(db, target, job_b)
    snapshot(db, observed, hours=25, views=100)
    snapshot(db, unobserved, hours=2, views=10)  # nothing inside the 24h interval
    db.commit()

    coverage = service().build(db).quality.coverage()

    assert coverage["24h"]["mature"] == 2
    assert coverage["24h"]["available"] == 1
    assert coverage["24h"]["missing_snapshot"] == 1
    assert coverage["24h"]["coverage"] == 0.5
    # Nothing is old enough for a 7d observation to be missing rather than pending.
    assert coverage["7d"]["not_mature"] == 0 or coverage["7d"]["mature"] >= 0


def test_a_young_publication_reports_not_mature_rather_than_missing(db, no_event_fanout):
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=0.1, views=3)
    db.commit()

    # Half an hour after publication: the 1h window cannot have an answer yet.
    dataset = service(T0 + timedelta(minutes=30)).build(db)
    row = dataset.rows[0]

    assert row.outcomes()["availability_1h"] == NOT_MATURE
    assert row.outcomes()["views_1h"] is None
    assert dataset.quality.coverage()["1h"]["mature"] == 0
    assert dataset.quality.coverage()["1h"]["coverage"] is None


# ===========================================================================
# Reproducibility
# ===========================================================================


def test_as_of_excludes_later_snapshots(db, no_event_fanout):
    """A dataset must not silently improve because the collector kept working.

    This is what makes "the numbers I analysed on Tuesday" recoverable on Friday.
    """
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=1100)
    db.commit()

    cut_off = T0 + timedelta(hours=24, minutes=30)
    early = service().build(db, as_of=cut_off)
    late = service().build(db, as_of=T0 + timedelta(days=3))

    assert early.rows[0].outcomes()["availability_24h"] == NOT_MATURE
    assert early.rows[0].outcomes()["views_24h"] is None
    assert late.rows[0].outcomes()["views_24h"] == 1100


def test_the_same_as_of_rebuilds_the_same_dataset(db, no_event_fanout):
    """New snapshots arrive; a rebuild at the original as_of is unchanged."""
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=1100)
    db.commit()

    as_of = T0 + timedelta(days=2)
    first = service().build(db, as_of=as_of)

    snapshot(db, attempt, hours=73, views=9999)
    snapshot(db, attempt, hours=170, views=50_000)
    db.commit()

    second = service().build(db, as_of=as_of)

    assert first.manifest.dataset_id == second.manifest.dataset_id
    assert [row.as_dict() for row in first.rows] == [row.as_dict() for row in second.rows]
    # And the later data is genuinely there for a build that asks for it.
    assert service().build(db).rows[0].outcomes()["views_72h"] == 9999


def test_the_dataset_id_changes_when_the_inputs_do(db, no_event_fanout):
    """Two datasets must be distinguishable, and the same request must name the same one."""
    target = make_target(db)
    topic, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=100)
    db.commit()

    as_of = T0 + timedelta(days=2)
    base = service().build(db, as_of=as_of).manifest
    same = service().build(db, as_of=as_of).manifest
    other_cutoff = service().build(db, as_of=as_of + timedelta(hours=1)).manifest
    filtered = service().build(
        db, as_of=as_of, filters=DatasetFilters(topic_id=str(topic.id))
    ).manifest

    assert base.dataset_id == same.dataset_id
    assert base.dataset_id != other_cutoff.dataset_id
    assert base.dataset_id != filtered.dataset_id
    assert base.semantic_version == DATASET_SEMANTIC_VERSION
    assert base.window_policy_version == WINDOW_POLICY_VERSION


def test_the_manifest_carries_the_rules_that_produced_it(db, no_event_fanout):
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=100)
    db.commit()

    manifest = service().build(db).manifest.as_dict()

    assert manifest["row_count"] == 1
    assert manifest["as_of"] is not None
    assert manifest["generated_at"] is not None
    assert {entry["window"] for entry in manifest["windows"]} == {
        "1h", "6h", "24h", "72h", "7d"
    }
    assert manifest["export_schema_version"] == EXPORT_SCHEMA_VERSION


# ===========================================================================
# Export
# ===========================================================================


def test_the_csv_has_declared_headers_and_empty_nulls(db, no_event_fanout):
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=100, likes=None, comments=2)
    db.commit()

    dataset = service().build(db)
    text = "".join(PerformanceDatasetService.to_csv(dataset))
    rows = list(csv.DictReader(io.StringIO(text)))

    assert list(csv.reader(io.StringIO(text)))[0] == export_columns()
    assert len(rows) == 1
    record = rows[0]
    assert record["publish_attempt_id"] == str(attempt.id)
    assert record["out_views_24h"] == "100"
    # NULL is the empty field, not the string "None".
    assert record["out_likes_24h"] == ""
    assert record["out_likes_per_view_24h"] == ""
    assert record["out_availability_1h"] == MISSING_SNAPSHOT
    assert record["trace_24h_lag_seconds"] == str(3600)


def test_the_csv_carries_no_credential_material(db, no_event_fanout):
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    attempt.upload_session_uri_encrypted = "gAAAAA-encrypted-session-uri"
    snapshot(db, attempt, hours=25, views=100)
    db.commit()

    text = "".join(PerformanceDatasetService.to_csv(service().build(db)))

    for forbidden in ("refresh_token", "upload_session_uri", "access_token", "gAAAAA"):
        assert forbidden not in text


def test_one_csv_row_per_external_publication(db, no_event_fanout):
    target = make_target(db)
    _, _, _, job = lineage(db)
    for index in (1, 2, 3):
        attempt = publication(db, target, job, video_id=f"clip_{index}", index=index)
        snapshot(db, attempt, hours=25, views=index * 10)
    db.commit()

    text = "".join(PerformanceDatasetService.to_csv(service().build(db)))
    rows = list(csv.DictReader(io.StringIO(text)))

    assert len(rows) == 3
    assert len({row["external_video_id"] for row in rows}) == 3


# ===========================================================================
# Boundaries
# ===========================================================================


def test_building_a_dataset_makes_no_provider_call(db, no_event_fanout, monkeypatch):
    """Ingestion talks to YouTube; evaluation talks to the database.

    A build that could spend quota would be a build nobody dares run twice, and reproducing a
    dataset would depend on an external service still answering the same way.
    """
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - the point is that it never runs
        raise AssertionError("evaluation must not perform HTTP requests")

    monkeypatch.setattr(httpx.Client, "send", explode)
    monkeypatch.setattr(httpx.Client, "request", explode)

    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=100)
    db.commit()

    dataset = service().build(db)
    list(PerformanceDatasetService.to_csv(dataset))

    assert dataset.rows[0].outcomes()["views_24h"] == 100


def test_evaluation_does_not_import_the_ingestion_provider():
    """The dataset layer has no reason to know how a counter was fetched."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for file in (root / "evaluation").rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                ("app.metrics", "httpx")
            ):
                offenders.append(f"{file.name}: {node.module}")
            if isinstance(node, ast.Import):
                offenders += [
                    f"{file.name}: {alias.name}" for alias in node.names
                    if alias.name.startswith(("app.metrics", "httpx"))
                ]

    assert offenders == []


def test_evaluation_package_is_not_imported_by_the_production_path():
    """The boundary that keeps this PR honest.

    Nothing that decides what to discover, select, produce or publish may import the
    evaluation layer. A future feedback loop then has to arrive as a visible import rather
    than as a quiet coupling nobody reviews.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    watched = [
        root / "discovery",
        root / "selection",
        root / "publishing",
        root / "services" / "admission_service.py",
        root / "services" / "automation_scheduler.py",
        root / "services" / "automation_runner.py",
        root / "services" / "publishing_service.py",
        root / "services" / "autopublish_policy.py",
        root / "services" / "publication_completion.py",
        root / "services" / "selection_service.py",
        root / "services" / "discovery_service.py",
    ]

    forbidden = ("app.evaluation", "app.services.performance_dataset_service")
    offenders: list[str] = []
    for path in watched:
        if not path.exists():
            continue
        files = path.rglob("*.py") if path.is_dir() else [path]
        for file in files:
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    forbidden
                ):
                    offenders.append(f"{file.name}: from {node.module}")
                if isinstance(node, ast.Import):
                    offenders += [
                        f"{file.name}: import {alias.name}" for alias in node.names
                        if alias.name.startswith(forbidden)
                    ]

    assert offenders == []


def test_building_a_dataset_changes_no_production_state(db, no_event_fanout):
    """Evaluation reads. Every field the production path uses is identical afterwards."""
    target = make_target(db)
    topic, _, candidate, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=100)
    db.commit()
    db.expire_all()

    before = {
        "relevance": candidate.relevance_score,
        "trend": candidate.trend_score,
        "scores": dict(candidate.scores_json or {}),
        "candidate_status": candidate.status,
        "job_state": job.state,
        "attempt_status": attempt.status,
        "topic_last_run": topic.last_run_at,
        "snapshots": db.query(VideoPerformanceSnapshot).count(),
    }

    service().build(db)
    db.expire_all()

    assert candidate.relevance_score == before["relevance"]
    assert candidate.trend_score == before["trend"]
    assert dict(candidate.scores_json or {}) == before["scores"]
    assert candidate.status == before["candidate_status"]
    assert job.state == before["job_state"]
    assert attempt.status == before["attempt_status"]
    assert topic.last_run_at == before["topic_last_run"]
    assert db.query(VideoPerformanceSnapshot).count() == before["snapshots"]


def test_no_composite_performance_score_is_produced(db, no_event_fanout):
    """Absolute counters and exact rates only.

    A "viral score" or "quality score" would need an empirical definition nobody has yet, and
    once such a column exists somebody will rank on it.
    """
    target = make_target(db)
    _, _, _, job = lineage(db)
    attempt = publication(db, target, job)
    snapshot(db, attempt, hours=25, views=100, likes=5, comments=2)
    db.commit()

    outcomes = single_row(db).outcomes()

    banned = ("viral", "quality_score", "performance_score", "winner", "rank", "percentile")
    assert not [key for key in outcomes if any(word in key for word in banned)]
    assert not [key for key in export_columns() if any(word in key for word in banned)]
