"""PR-METRICS-01 — measuring what happened, without letting it change what happens.

Two claims are under test here, and they pull in opposite directions.

The first is that the measurement is *honest*: a video the API declined to return is recorded
as unreturned and never as zero views; a counter that goes down is stored as it was observed;
a hidden like count stays NULL. Every one of those has a plausible, wrong alternative that
would look fine on a chart and quietly corrupt the series.

The second is that the measurement is *inert*. `test_collection_does_not_touch_production`
is the one that names it: a full collection runs, and every field the production path reads —
relevance, trend, selection, job state, publication status — is byte-identical afterwards.
That test is the reason this PR can be merged before anyone has decided what the data is for.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.settings import settings
from app.metrics.contracts import (
    NOT_RETURNED,
    OK,
    UNAVAILABLE,
    MetricsAuthError,
    MetricsFetchResult,
    VideoMetrics,
)
from app.metrics.youtube_metrics import YouTubeVideoMetricsProvider
from app.models.enums import (
    PipelineState,
    PublishAttemptStatus,
    PublishTargetConnectionStatus,
    VideoCandidateStatus,
)
from app.models.content_topic import ContentTopic
from app.models.publish_attempt import PublishAttempt
from app.models.video_candidate import VideoCandidate
from app.models.video_performance_snapshot import VideoPerformanceSnapshot
from app.publishing.contracts import PublishCredential
from app.services.content_lineage_service import ContentLineageService
from app.services.metrics_ingestion_service import (
    AUTH_FAILED,
    TARGET_UNAVAILABLE,
    YouTubeMetricsIngestionService,
    capture_slot,
)
from tests.conftest import make_run
from tests.test_publishing import (  # noqa: F401 - publishing_config is autouse
    REFRESH_TOKEN,
    make_target,
    oauth_client,
    publishing_config,
)


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


# ===========================================================================
# Fixtures and helpers
# ===========================================================================


@pytest.fixture(autouse=True)
def metrics_config(monkeypatch):
    monkeypatch.setattr(settings, "metrics_collection_enabled", True, raising=False)
    monkeypatch.setattr(settings, "metrics_tracking_days", 30, raising=False)
    monkeypatch.setattr(settings, "metrics_interval_fresh_hours", 1, raising=False)
    monkeypatch.setattr(settings, "metrics_interval_recent_hours", 6, raising=False)
    monkeypatch.setattr(settings, "metrics_interval_mature_hours", 24, raising=False)
    monkeypatch.setattr(settings, "metrics_max_videos_per_run", 200, raising=False)
    monkeypatch.setattr(settings, "metrics_stale_hours", 48, raising=False)


class StubProvider:
    """A provider that answers from a script, and records what it was asked.

    ``answers`` maps a video id to the metrics to return. An id that is absent is simply not
    returned, which is exactly how the real API behaves for a deleted or hidden video — the
    unreturned case is produced by omission rather than by a special value.
    """

    provider = "youtube"
    max_batch = 50

    def __init__(self, answers: dict[str, VideoMetrics] | None = None, *, raises=None):
        self.answers = answers or {}
        self.raises = raises
        self.calls: list[list[str]] = []
        self.credentials: list[str] = []

    def fetch_metrics(self, video_ids, *, credential) -> MetricsFetchResult:
        self.calls.append(list(video_ids))
        self.credentials.append(credential.refresh_token)
        if self.raises is not None:
            raise self.raises

        result = MetricsFetchResult(requested=len(video_ids))
        for batch_start in range(0, len(video_ids), self.max_batch):
            result.calls += 1
            for video_id in video_ids[batch_start:batch_start + self.max_batch]:
                found = self.answers.get(video_id)
                if found is not None:
                    result.metrics[video_id] = found
                    result.returned += 1
        for video_id in video_ids:
            result.metrics.setdefault(
                video_id, VideoMetrics(external_video_id=video_id,
                                       availability=NOT_RETURNED)
            )
        return result


def ingestion(provider=None, *, now=NOW) -> YouTubeMetricsIngestionService:
    return YouTubeMetricsIngestionService(provider=provider or StubProvider(),
                                          clock=lambda: now)


def published(db, target, *, video_id="vid_1", finished=None, job=None,
              status=PublishAttemptStatus.SUCCEEDED, **overrides) -> PublishAttempt:
    """A publication that actually produced a video."""
    job = job or make_run(db, state=PipelineState.PUBLISHED)
    fields = dict(
        pipeline_job_id=job.id,
        target_id=target.id,
        idempotency_key=f"publish:{job.id}:{target.id}:{video_id}:v1",
        media_identity=f"final_clips/{video_id}.mp4",
        status=status,
        external_id=video_id,
        attempt_no=1,
        max_attempts=3,
        initiator="automatic",
        finished_at=finished or (NOW - timedelta(hours=2)),
    )
    fields.update(overrides)
    attempt = PublishAttempt(**fields)
    db.add(attempt)
    db.flush()
    return attempt


def metrics(video_id, views=None, likes=None, comments=None, availability=OK, **kw):
    return VideoMetrics(
        external_video_id=video_id, availability=availability,
        view_count=views, like_count=likes, comment_count=comments, **kw,
    )


def snapshots_for(db, attempt) -> list[VideoPerformanceSnapshot]:
    return (
        db.query(VideoPerformanceSnapshot)
        .filter(VideoPerformanceSnapshot.publish_attempt_id == attempt.id)
        .order_by(VideoPerformanceSnapshot.captured_at.asc())
        .all()
    )


# ===========================================================================
# What is due
# ===========================================================================


def test_only_successful_publications_with_a_video_are_tracked(db, no_event_fanout):
    """A publication with no video on the internet has nothing to measure.

    Asking the API about a FAILED or PENDING attempt spends quota to be told nothing, and an
    UNKNOWN one is precisely the case where this system does not know whether a video exists —
    inventing a metrics series for it would assert the very thing that is in doubt.
    """
    target = make_target(db)
    ok = published(db, target, video_id="vid_ok")
    published(db, target, video_id="vid_failed", status=PublishAttemptStatus.FAILED_FINAL)
    published(db, target, video_id="vid_unknown", status=PublishAttemptStatus.UNKNOWN)
    published(db, target, video_id=None, external_id=None)
    db.commit()

    due = ingestion().due_publications(db, now=NOW)

    assert [a.id for items in due.values() for a in items] == [ok.id]


def test_publications_older_than_the_tracking_window_are_dropped(db, no_event_fanout):
    """Views accrue for ever; the window worth watching does not.

    Past the horizon a video's series is history rather than signal, and continuing to poll it
    would spend the quota that fresh videos need.
    """
    target = make_target(db)
    fresh = published(db, target, video_id="fresh", finished=NOW - timedelta(days=2))
    published(db, target, video_id="ancient", finished=NOW - timedelta(days=90))
    db.commit()

    due = ingestion().due_publications(db, now=NOW)

    assert [a.id for items in due.values() for a in items] == [fresh.id]


@pytest.mark.parametrize(
    "age, since_last, expected",
    [
        # Under a day old: hourly.
        (timedelta(hours=3), timedelta(minutes=30), False),
        (timedelta(hours=3), timedelta(hours=2), True),
        # One to seven days: every six hours.
        (timedelta(days=3), timedelta(hours=2), False),
        (timedelta(days=3), timedelta(hours=7), True),
        # Older: daily.
        (timedelta(days=20), timedelta(hours=7), False),
        (timedelta(days=20), timedelta(hours=30), True),
    ],
)
def test_cadence_follows_the_age_of_the_video(db, no_event_fanout, age, since_last,
                                              expected):
    """The cadence table, exercised against an injected clock rather than real time.

    A test that waited an hour to prove an hourly interval would be a test nobody runs.
    """
    target = make_target(db)
    attempt = published(db, target, finished=NOW - age)
    db.add(
        VideoPerformanceSnapshot(
            publish_attempt_id=attempt.id, publish_target_id=target.id,
            external_video_id="vid_1", provider="youtube",
            captured_at=NOW - since_last, capture_slot=capture_slot(NOW - since_last),
            view_count=10, availability=OK,
        )
    )
    db.commit()

    due = ingestion().due_publications(db, now=NOW)

    assert bool(due) is expected


def test_a_never_collected_publication_is_always_due(db, no_event_fanout):
    target = make_target(db)
    published(db, target, finished=NOW - timedelta(days=25))
    db.commit()

    assert ingestion().due_publications(db, now=NOW)


# ===========================================================================
# Collection
# ===========================================================================


def test_first_collection_creates_a_snapshot(db, no_event_fanout):
    target = make_target(db)
    attempt = published(db, target, video_id="vid_1")
    db.commit()

    provider = StubProvider({"vid_1": metrics("vid_1", views=120, likes=8, comments=3)})
    report = ingestion(provider).run(db, dry_run=False)

    assert report.status == "completed"
    assert report.snapshots_created == 1

    (snapshot,) = snapshots_for(db, attempt)
    assert snapshot.view_count == 120
    assert snapshot.like_count == 8
    assert snapshot.comment_count == 3
    assert snapshot.availability == OK
    assert snapshot.external_video_id == "vid_1"


def test_snapshots_are_appended_never_updated(db, no_event_fanout):
    """The series is the product. Overwriting would leave a number and no history.

    Two collections an hour apart must produce two rows, because "410 views" on its own says
    nothing about whether the video is climbing or dead.
    """
    target = make_target(db)
    attempt = published(db, target, video_id="vid_1")
    db.commit()

    later = NOW + timedelta(hours=3)
    ingestion(StubProvider({"vid_1": metrics("vid_1", views=100)}), now=NOW).run(
        db, dry_run=False
    )
    ingestion(StubProvider({"vid_1": metrics("vid_1", views=180)}), now=later).run(
        db, dry_run=False
    )

    series = snapshots_for(db, attempt)
    assert [s.view_count for s in series] == [100, 180]
    assert series[0].capture_slot != series[1].capture_slot


def test_a_counter_that_decreases_is_recorded_as_observed(db, no_event_fanout):
    """YouTube removes spam views and deleted comments, so ``new >= old`` is not an invariant.

    Rejecting or clamping a decrease would replace a real measurement with a fiction that is
    then indistinguishable from a real one.
    """
    target = make_target(db)
    attempt = published(db, target, video_id="vid_1")
    db.commit()

    ingestion(StubProvider({"vid_1": metrics("vid_1", views=500, comments=40)}),
              now=NOW).run(db, dry_run=False)
    ingestion(StubProvider({"vid_1": metrics("vid_1", views=460, comments=12)}),
              now=NOW + timedelta(hours=3)).run(db, dry_run=False)

    series = snapshots_for(db, attempt)
    assert [s.view_count for s in series] == [500, 460]
    assert [s.comment_count for s in series] == [40, 12]


def test_hidden_counters_are_null_not_zero(db, no_event_fanout):
    """A hidden like count is *unknown*, and zero is a claim about the audience.

    Collapsing the two would make "nobody liked this" and "the owner hides likes" the same
    fact, and no later reader could tell them apart.
    """
    target = make_target(db)
    attempt = published(db, target, video_id="vid_1")
    db.commit()

    ingestion(StubProvider({"vid_1": metrics("vid_1", views=90, likes=None,
                                             comments=0)})).run(db, dry_run=False)

    (snapshot,) = snapshots_for(db, attempt)
    assert snapshot.like_count is None
    # Zero survives as zero: it is an observation, not an absence.
    assert snapshot.comment_count == 0
    assert snapshot.view_count == 90


def test_a_video_the_api_does_not_return_is_classified_not_zeroed(db, no_event_fanout):
    """Deleted, made private, or region-blocked — the API does not say which.

    What it must never become is ``views=0``, which would look like a catastrophic collapse
    on any chart drawn from the series.
    """
    target = make_target(db)
    attempt = published(db, target, video_id="vid_gone")
    db.commit()

    report = ingestion(StubProvider({})).run(db, dry_run=False)

    (snapshot,) = snapshots_for(db, attempt)
    assert snapshot.availability == NOT_RETURNED
    assert snapshot.view_count is None
    assert report.targets[0].missing == 1


def test_a_partial_response_persists_what_came_back(db, no_event_fanout):
    """Three asked for, two answered: two measurements and one classification.

    The failure mode this excludes is discarding the whole batch because one video was
    missing, which would lose two good observations to protect nothing.
    """
    target = make_target(db)
    first = published(db, target, video_id="vid_a")
    second = published(db, target, video_id="vid_b")
    third = published(db, target, video_id="vid_c")
    db.commit()

    ingestion(StubProvider({
        "vid_a": metrics("vid_a", views=10),
        "vid_c": metrics("vid_c", views=30),
    })).run(db, dry_run=False)

    assert snapshots_for(db, first)[0].view_count == 10
    assert snapshots_for(db, third)[0].view_count == 30
    missing = snapshots_for(db, second)[0]
    assert missing.availability == NOT_RETURNED
    assert missing.view_count is None


def test_a_private_video_is_recorded_with_its_privacy(db, no_event_fanout):
    """Autopublish uploads privately by default, so this is the normal case, not an edge one."""
    target = make_target(db)
    attempt = published(db, target, video_id="vid_1")
    db.commit()

    ingestion(StubProvider({
        "vid_1": metrics("vid_1", views=0, privacy_status="private"),
    })).run(db, dry_run=False)

    (snapshot,) = snapshots_for(db, attempt)
    assert snapshot.privacy_status == "private"
    assert snapshot.availability == OK
    assert snapshot.view_count == 0


def test_a_second_run_in_the_same_hour_collects_nothing(db, no_event_fanout):
    """The cadence is the first line: an hour has not passed, so nothing is due."""
    target = make_target(db)
    attempt = published(db, target, video_id="vid_1")
    db.commit()

    ingestion(StubProvider({"vid_1": metrics("vid_1", views=100)})).run(db, dry_run=False)
    provider = StubProvider({"vid_1": metrics("vid_1", views=101)})
    second = ingestion(provider).run(db, dry_run=False)

    assert second.status == "noop"
    # No second call, so no second quota unit spent either.
    assert provider.calls == []
    assert len(snapshots_for(db, attempt)) == 1
    assert snapshots_for(db, attempt)[0].view_count == 100


def test_the_database_rejects_a_duplicate_capture_slot(db, no_event_fanout):
    """The second line, for the case the cadence cannot cover.

    Two replicas can both evaluate "is this due?" before either has written, so the cadence
    check is a check-then-insert that a race walks straight through. The unique constraint on
    (publish_attempt_id, capture_slot) is what actually makes a repeated collection
    idempotent, and this exercises it directly rather than through a schedule that would hide
    it.
    """
    target = make_target(db)
    attempt = published(db, target, video_id="vid_1")
    db.commit()

    service = ingestion()
    first = service._persist(db, attempt, target, metrics("vid_1", views=100), now=NOW)
    # The same capture slot, a different figure: the racing replica's answer.
    second = service._persist(db, attempt, target, metrics("vid_1", views=101), now=NOW)
    db.commit()

    assert first is True
    assert second is False
    assert [s.view_count for s in snapshots_for(db, attempt)] == [100]


def test_ids_are_batched_not_fetched_one_by_one(db, no_event_fanout):
    """50 ids per call is one quota unit; 50 calls is fifty."""
    target = make_target(db)
    for index in range(60):
        published(db, target, video_id=f"vid_{index:03d}")
    db.commit()

    provider = StubProvider()
    report = ingestion(provider).run(db, dry_run=False)

    assert len(provider.calls) == 1
    assert len(provider.calls[0]) == 60
    assert report.targets[0].provider_calls == 2


# ===========================================================================
# Targets and credentials
# ===========================================================================


def test_each_target_is_called_with_its_own_credential(db, no_event_fanout):
    """One refresh token per channel. Reading channel A with channel B's token is wrong.

    The grouping is what makes that structurally impossible rather than merely unlikely.
    """
    from app.security.secret_box import SecretBox

    box = SecretBox(settings.publish_secret_key)
    first = make_target(db, name="Channel A", channel_id="UC_a")
    second = make_target(
        db, name="Channel B", channel_id="UC_b",
        refresh_token_encrypted=box.encrypt("1//token-for-channel-b"),
    )
    published(db, first, video_id="vid_a")
    published(db, second, video_id="vid_b")
    db.commit()

    provider = StubProvider()
    ingestion(provider).run(db, dry_run=False)

    by_credential = dict(zip(provider.credentials, provider.calls))
    assert by_credential[REFRESH_TOKEN] == ["vid_a"]
    assert by_credential["1//token-for-channel-b"] == ["vid_b"]


def test_a_disconnected_target_is_skipped_without_blocking_the_others(db, no_event_fanout):
    """One broken channel must not cost every other channel its collection."""
    broken = make_target(
        db, name="Broken", channel_id="UC_broken",
        connection_status=PublishTargetConnectionStatus.RECONNECT_REQUIRED,
        refresh_token_encrypted=None,
    )
    healthy = make_target(db, name="Healthy", channel_id="UC_ok")
    published(db, broken, video_id="vid_broken")
    good = published(db, healthy, video_id="vid_ok")
    db.commit()

    report = ingestion(StubProvider({"vid_ok": metrics("vid_ok", views=7)})).run(
        db, dry_run=False
    )

    results = {target.target_name: target for target in report.targets}
    assert results["Broken"].status == "skipped"
    assert results["Broken"].reason == TARGET_UNAVAILABLE
    assert results["Healthy"].snapshots_created == 1
    assert snapshots_for(db, good)[0].view_count == 7


def test_an_auth_failure_reuses_the_existing_reconnect_semantics(db, no_event_fanout):
    """A rejected token is a target problem, and this system already has one word for it.

    Inventing a second notion of "the credential is broken" would let the publisher and the
    collector disagree about whether a channel is usable.
    """
    target = make_target(db)
    published(db, target, video_id="vid_1")
    db.commit()

    provider = StubProvider(raises=MetricsAuthError("invalid_grant", recoverable=False))
    report = ingestion(provider).run(db, dry_run=False)

    db.refresh(target)
    assert target.connection_status == PublishTargetConnectionStatus.RECONNECT_REQUIRED
    assert target.last_error_code == "invalid_grant"
    # The dead token is dropped, not kept.
    assert target.refresh_token_encrypted is None
    assert report.targets[0].reason == AUTH_FAILED


def test_a_provider_crash_fails_one_target_and_not_the_run(db, no_event_fanout):
    target = make_target(db)
    published(db, target, video_id="vid_1")
    db.commit()

    report = ingestion(StubProvider(raises=RuntimeError("boom"))).run(db, dry_run=False)

    assert report.targets[0].status == "failed"
    assert report.status == "failed"
    # No snapshot invented for a video that was never actually read.
    assert db.query(VideoPerformanceSnapshot).count() == 0


# ===========================================================================
# Dry run
# ===========================================================================


def test_dry_run_reports_without_calling_youtube_or_writing(db, no_event_fanout):
    """The default for the manual endpoint: show me what it would do.

    A "run" that silently spends the channel's quota is not a safe default for a button.
    """
    target = make_target(db)
    published(db, target, video_id="vid_1")
    db.commit()

    provider = StubProvider({"vid_1": metrics("vid_1", views=100)})
    report = ingestion(provider).run(db, dry_run=True)

    assert report.dry_run is True
    assert provider.calls == []
    assert db.query(VideoPerformanceSnapshot).count() == 0
    assert report.targets[0].status == "would_collect"
    assert report.targets[0].videos_requested == 1


# ===========================================================================
# The provider
# ===========================================================================


def youtube_provider(handler) -> YouTubeVideoMetricsProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return YouTubeVideoMetricsProvider(oauth=oauth_client(handler), client=client)


def credential() -> PublishCredential:
    return PublishCredential(
        refresh_token=REFRESH_TOKEN, client_id="client-id.apps.google",
        client_secret="client-secret",
    )


def test_provider_parses_statistics_and_omits_what_youtube_omits(db):
    """The real response shape: counters are strings, and absent keys stay absent."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        assert request.url.params["part"] == "statistics,status"
        return httpx.Response(200, json={"items": [
            {
                "id": "vid_a",
                "statistics": {"viewCount": "1234", "commentCount": "5"},
                "status": {"uploadStatus": "processed", "privacyStatus": "public"},
            },
        ]})

    result = youtube_provider(handler).fetch_metrics(["vid_a", "vid_b"],
                                                     credential=credential())

    measured = result.metrics["vid_a"]
    assert measured.view_count == 1234
    assert measured.comment_count == 5
    # likeCount was absent because the owner hides likes. Not zero.
    assert measured.like_count is None
    assert measured.privacy_status == "public"
    # Asked for and never returned.
    assert result.metrics["vid_b"].availability == NOT_RETURNED
    assert result.metrics["vid_b"].view_count is None


def test_provider_marks_a_still_processing_video_unavailable(db):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return httpx.Response(200, json={"items": [
            {"id": "vid_a", "statistics": {"viewCount": "0"},
             "status": {"uploadStatus": "processing", "privacyStatus": "private"}},
        ]})

    result = youtube_provider(handler).fetch_metrics(["vid_a"], credential=credential())

    assert result.metrics["vid_a"].availability == UNAVAILABLE


def test_provider_raises_auth_error_on_a_rejected_token(db):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(MetricsAuthError) as raised:
        youtube_provider(handler).fetch_metrics(["vid_a"], credential=credential())

    assert raised.value.code == "invalid_grant"
    assert raised.value.recoverable is False


def test_provider_never_puts_a_response_body_in_the_error_code(db):
    """Google echoes request parameters into some error messages. Only the code is kept."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return httpx.Response(403, json={"error": {"errors": [
            {"reason": "quotaExceeded",
             "message": "Request had key=SECRET-LOOKING-VALUE"},
        ]}})

    result = youtube_provider(handler).fetch_metrics(["vid_a"], credential=credential())

    assert result.error_code == "quotaExceeded"
    assert "SECRET-LOOKING-VALUE" not in str(result.error_code)


def test_provider_batches_at_fifty(db):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        calls.append(len(request.url.params["id"].split(",")))
        return httpx.Response(200, json={"items": []})

    result = youtube_provider(handler).fetch_metrics(
        [f"vid_{i}" for i in range(120)], credential=credential()
    )

    assert calls == [50, 50, 20]
    assert result.calls == 3


# ===========================================================================
# Lineage
# ===========================================================================


def full_chain(db, target):
    """A publication with every provenance link a real foreign key."""
    topic = ContentTopic(name=f"Serie A {uuid.uuid4().hex[:6]}", is_active=True)
    db.add(topic)
    db.flush()
    candidate = VideoCandidate(
        topic_id=topic.id, external_id="src_video_9", url="https://youtu.be/src_video_9",
        title="Milan 3-1 Inter", channel="Serie A", duration_sec=600,
        status=VideoCandidateStatus.SELECTED, relevance_score=0.82, trend_score=0.44,
        scores_json={"relevance": 0.82, "trend": 0.44}, selected_at=NOW - timedelta(days=1),
    )
    db.add(candidate)
    db.flush()
    job = make_run(db, state=PipelineState.PUBLISHED, topic_id=topic.id,
                   candidate_id=candidate.id)
    attempt = published(db, target, video_id="vid_pub", job=job)
    db.commit()
    return topic, candidate, job, attempt


def test_lineage_walks_from_the_video_back_to_the_source_item(db, no_event_fanout):
    """The end-to-end question: which discovered video produced this published clip?"""
    target = make_target(db)
    topic, candidate, job, attempt = full_chain(db, target)

    chain = ContentLineageService().lineage(db, attempt)

    assert chain["complete"] is True
    assert chain["topic"]["content_topic_id"] == str(topic.id)
    assert chain["candidate"]["video_candidate_id"] == str(candidate.id)
    assert chain["candidate"]["external_id"] == "src_video_9"
    assert chain["candidate"]["relevance_score"] == pytest.approx(0.82)
    assert chain["job"]["pipeline_job_id"] == str(job.id)
    assert chain["publication"]["external_video_id"] == "vid_pub"
    assert chain["publication"]["video_url"].endswith("vid_pub")


def test_lineage_reports_a_missing_link_instead_of_guessing_one(db, no_event_fanout):
    """A job with no candidate has an *unknown* origin, and says so.

    The alternative — matching on title, or on the nearest candidate by time — would
    manufacture provenance that reads as authoritative and is a guess.
    """
    target = make_target(db)
    job = make_run(db, state=PipelineState.PUBLISHED)
    attempt = published(db, target, video_id="vid_orphan", job=job)
    db.commit()

    chain = ContentLineageService().lineage(db, attempt)

    assert chain["complete"] is False
    assert chain["candidate"] is None
    assert chain["source"] is None
    assert chain["publication"]["external_video_id"] == "vid_orphan"


def test_lineage_never_serialises_a_credential(db, no_event_fanout):
    """The read model is built from chosen columns, not filtered afterwards."""
    target = make_target(db)
    _, _, _, attempt = full_chain(db, target)
    attempt.upload_session_uri_encrypted = "gAAAAA-encrypted-session-uri"
    db.flush()

    payload = str(ContentLineageService().lineage(db, attempt))

    assert REFRESH_TOKEN not in payload
    assert "upload_session_uri" not in payload
    assert "refresh_token" not in payload


def test_performance_read_model_keeps_the_last_real_measurement(db, no_event_fanout):
    """A video that has since gone private still has a last known figure.

    Reporting the unreturned row's NULLs as "current" would read as a collapse to zero on
    every chart drawn from it.
    """
    target = make_target(db)
    attempt = published(db, target, video_id="vid_1")
    db.commit()

    ingestion(StubProvider({"vid_1": metrics("vid_1", views=300)}), now=NOW).run(
        db, dry_run=False
    )
    ingestion(StubProvider({}), now=NOW + timedelta(hours=5)).run(db, dry_run=False)

    view = ContentLineageService().performance(db, attempt)

    assert view["snapshot_count"] == 2
    assert view["latest"]["view_count"] == 300
    assert view["series"][-1]["availability"] == NOT_RETURNED
    assert view["series"][-1]["view_count"] is None


def test_each_clip_of_a_run_keeps_its_own_series(db, no_event_fanout):
    """A run publishes several clips and each is its own video with its own audience.

    Aggregating them at ingestion would destroy exactly the comparison the data exists to
    make — which cut of the same match did better.
    """
    target = make_target(db)
    job = make_run(db, state=PipelineState.PUBLISHED)
    first = published(db, target, video_id="clip_a", job=job)
    second = published(db, target, video_id="clip_b", job=job)
    db.commit()

    ingestion(StubProvider({
        "clip_a": metrics("clip_a", views=900),
        "clip_b": metrics("clip_b", views=40),
    })).run(db, dry_run=False)

    assert snapshots_for(db, first)[0].view_count == 900
    assert snapshots_for(db, second)[0].view_count == 40


# ===========================================================================
# Independence from production
# ===========================================================================


def test_collection_does_not_touch_production(db, no_event_fanout):
    """**The point of the PR.** Measurement must not become optimization by accident.

    Every field the production path reads is captured before a full collection and compared
    afterwards. If a later change ever wires a collected view count into the relevance score,
    the selection score, the candidate status or the job state, this test is what fails.
    """
    target = make_target(db)
    topic, candidate, job, attempt = full_chain(db, target)

    # Read back from the database first: a Numeric column returns a Decimal once loaded,
    # and comparing that against the float just assigned in Python would fail on the type
    # rather than on any change to the value.
    db.expire_all()
    before = {
        "relevance_score": candidate.relevance_score,
        "trend_score": candidate.trend_score,
        "scores_json": dict(candidate.scores_json or {}),
        "candidate_status": candidate.status,
        "selected_at": candidate.selected_at,
        "job_state": job.state,
        "attempt_status": attempt.status,
        "attempt_external_id": attempt.external_id,
        "topic_last_run_at": topic.last_run_at,
    }

    report = ingestion(StubProvider({
        "vid_pub": metrics("vid_pub", views=99_999, likes=4_000, comments=250),
    })).run(db, dry_run=False)
    assert report.snapshots_created == 1

    db.refresh(candidate)
    db.refresh(job)
    db.refresh(attempt)
    db.refresh(topic)

    assert candidate.relevance_score == before["relevance_score"]
    assert candidate.trend_score == before["trend_score"]
    assert dict(candidate.scores_json or {}) == before["scores_json"]
    assert candidate.status == before["candidate_status"]
    assert candidate.selected_at == before["selected_at"]
    assert job.state == before["job_state"]
    assert attempt.status == before["attempt_status"]
    assert attempt.external_id == before["attempt_external_id"]
    assert topic.last_run_at == before["topic_last_run_at"]


def test_metrics_package_is_not_imported_by_the_production_path():
    """The boundary, checked structurally rather than by convention.

    Nothing in discovery, selection, admission, production or publishing may import
    ``app.metrics``. That absence is what makes "no feedback loop" a property of the code
    instead of a promise in a docstring — a future edge would have to show up as an import in
    a diff.
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
        root / "services" / "publishing_service.py",
        root / "services" / "autopublish_policy.py",
        root / "services" / "publication_completion.py",
    ]

    offenders: list[str] = []
    for path in watched:
        if not path.exists():
            continue
        files = path.rglob("*.py") if path.is_dir() else [path]
        for file in files:
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "app.metrics"
                ):
                    offenders.append(f"{file.name}: from {node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("app.metrics"):
                            offenders.append(f"{file.name}: import {alias.name}")

    assert offenders == []


def test_metrics_failure_does_not_stop_the_automation_loop(db, no_event_fanout,
                                                           monkeypatch):
    """A collection that raises outright must leave the production tick untouched.

    The runner shares its loop with the scheduler, so this is the test that says sharing is
    safe: collection runs strictly after the tick and its exceptions never leave the handler.
    """
    import asyncio

    from app.services.automation_runner import AutomationRunner

    class Exploding:
        def run(self, db, **kwargs):
            raise RuntimeError("youtube is down")

    runner = AutomationRunner(metrics=Exploding())
    monkeypatch.setattr(settings, "metrics_poll_interval_sec", 60, raising=False)

    # No exception escapes, and the loop is free to continue to its next tick.
    asyncio.run(runner._maybe_collect_metrics())


def test_collection_is_not_attempted_when_disabled(db, no_event_fanout, monkeypatch):
    """Off by default, like every other autonomous behaviour: collection spends quota."""
    import asyncio

    from app.services.automation_runner import AutomationRunner

    class Recording:
        def __init__(self):
            self.runs = 0

        def run(self, db, **kwargs):
            self.runs += 1
            raise AssertionError("collection ran while disabled")

    monkeypatch.setattr(settings, "metrics_collection_enabled", False, raising=False)
    runner = AutomationRunner(metrics=Recording())

    asyncio.run(runner._maybe_collect_metrics())

    assert runner._metrics.runs == 0
