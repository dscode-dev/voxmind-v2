"""Canonical identity, deduplication and idempotent persistence (PR-DISCOVERY-01).

The question this file answers: run the same discovery twice, and do you get one row or two?
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.discovery import identity
from app.discovery.contracts import DiscoveredVideo, DiscoveryFetch, DiscoveryRequest
from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.enums import DiscoverySourceKind, VideoCandidateStatus
from app.models.video_candidate import VideoCandidate
from app.services.discovery_service import DiscoveryService

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


# ==========================================================================
# Canonical identity
# ==========================================================================


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?t=42",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=2",
        "www.youtube.com/watch?v=dQw4w9WgXcQ",
        "dQw4w9WgXcQ",
    ],
)
def test_every_youtube_url_shape_yields_one_id(url):
    """Eleven strings, one video. Comparing text would produce eleven rows."""
    assert identity.extract_youtube_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/channel/UCabcdefghij",
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/playlist?list=PL123",
        "https://vimeo.com/123456",
        "https://portal.example/noticias/x",
        "",
        None,
        "not a url at all",
    ],
)
def test_a_non_video_url_yields_no_id(url):
    """None, not a guess: fabricating an id from a channel URL fabricates identity."""
    assert identity.extract_youtube_id(url) is None


def test_a_short_and_a_watch_url_share_one_dedup_hash():
    left = identity.extract_youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ")
    right = identity.extract_youtube_id("https://youtu.be/dQw4w9WgXcQ")
    assert identity.dedup_hash("youtube", left) == identity.dedup_hash("youtube", right)


def test_different_videos_have_different_identities():
    assert identity.dedup_hash("youtube", "aaaaaaaaaaa") != identity.dedup_hash("youtube", "bbbbbbbbbbb")


def test_the_same_id_on_different_providers_is_not_the_same_thing():
    assert identity.dedup_hash("youtube", "abc") != identity.dedup_hash("rss", "abc")


def test_the_readable_key_is_kept_alongside_the_hash():
    assert identity.dedup_key("youtube", "dQw4w9WgXcQ") == "youtube:dQw4w9WgXcQ"
    assert len(identity.dedup_hash("youtube", "dQw4w9WgXcQ")) == 64


def test_a_derived_identity_is_stable_and_marked():
    first = identity.derived_external_id("https://portal.example/a")
    second = identity.derived_external_id("https://portal.example/a")
    assert first == second
    assert first.startswith("d_"), "a derived id must not be mistakable for a provider's"
    assert first != identity.derived_external_id("https://portal.example/b")


def test_a_derived_identity_needs_material():
    assert identity.derived_external_id(None, "", "  ") == ""


def test_canonical_url_normalises_youtube_and_passes_others_through():
    assert identity.canonical_url("youtube", "abc") == "https://www.youtube.com/watch?v=abc"
    assert identity.canonical_url("rss", "d_x", "https://p/a") == "https://p/a"


# ==========================================================================
# Persistence fixtures
# ==========================================================================


@pytest.fixture()
def topic(db):
    topic = ContentTopic(
        name="Futebol brasileiro",
        keywords_json=["futebol entrevista", "futebol coletiva"],
        metadata_json={"language": "pt", "region": "BR"},
    )
    db.add(topic)
    db.flush()
    return topic


@pytest.fixture()
def source(db, topic):
    source = DiscoverySource(
        topic_id=topic.id,
        kind=DiscoverySourceKind.YOUTUBE_SEARCH,
        name="YouTube search",
        is_active=True,
        config_json={"queries": ["futebol entrevista"], "max_results": 10},
    )
    db.add(source)
    db.flush()
    return source


class StubProvider:
    """Returns whatever it is handed. The provider is not under test here."""

    name = "youtube"

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    def is_configured(self):
        return True

    def discover(self, request):
        self.calls += 1
        videos = self.batches[min(self.calls - 1, len(self.batches) - 1)]
        return DiscoveryFetch(videos=list(videos), api_calls=2)


def video(video_id: str, **overrides) -> DiscoveredVideo:
    fields = {
        "provider": "youtube",
        "external_id": video_id,
        "canonical_url": identity.canonical_youtube_url(video_id),
        "title": "Entrevista completa",
        "channel_name": "Canal Esportivo",
        "published_at": NOW,
        "duration_sec": 750,
        "view_count": 100,
    }
    fields.update(overrides)
    return DiscoveredVideo(**fields)


def service(batches) -> tuple[DiscoveryService, StubProvider]:
    provider = StubProvider(batches)
    return DiscoveryService(youtube_provider=provider), provider


def candidates(db):
    return db.query(VideoCandidate).order_by(VideoCandidate.created_at).all()


# ==========================================================================
# Dedup — the four mandated cases
# ==========================================================================


def test_the_same_video_twice_in_one_run_yields_one_row(db, topic, source, no_event_fanout):
    svc, _ = service([[video("aaaaaaaaaaa"), video("aaaaaaaaaaa")]])

    result = svc.run_source(db, topic=topic, source=source, commit=False)

    assert len(candidates(db)) == 1
    assert result.results_received == 2
    assert result.new_candidates == 1
    assert result.existing_candidates == 1


def test_running_the_same_discovery_again_updates_rather_than_duplicates(db, topic, source, no_event_fanout):
    svc, _ = service([[video("aaaaaaaaaaa", view_count=100)],
                      [video("aaaaaaaaaaa", view_count=500)]])

    first = svc.run_source(db, topic=topic, source=source, commit=False)
    second = svc.run_source(db, topic=topic, source=source, commit=False)

    rows = candidates(db)
    assert len(rows) == 1
    assert first.new_candidates == 1 and first.existing_candidates == 0
    assert second.new_candidates == 0 and second.existing_candidates == 1
    assert rows[0].metadata_json["normalized"]["view_count"] == 500, "mutable metadata refreshes"


def test_a_different_url_for_the_same_youtube_id_is_the_same_row(db, topic, source, no_event_fanout):
    """A shorts URL and a watch URL are one video."""
    svc, _ = service([
        [video("aaaaaaaaaaa", canonical_url="https://www.youtube.com/shorts/aaaaaaaaaaa")],
        [video("aaaaaaaaaaa", canonical_url="https://youtu.be/aaaaaaaaaaa")],
    ])

    svc.run_source(db, topic=topic, source=source, commit=False)
    svc.run_source(db, topic=topic, source=source, commit=False)

    assert len(candidates(db)) == 1


def test_two_different_videos_with_the_same_title_are_two_rows(db, topic, source, no_event_fanout):
    """Titles are not identity: two channels cover the same match with the same words."""
    svc, _ = service([[
        video("aaaaaaaaaaa", title="Entrevista completa do tecnico"),
        video("bbbbbbbbbbb", title="Entrevista completa do tecnico"),
    ]])

    svc.run_source(db, topic=topic, source=source, commit=False)

    assert len(candidates(db)) == 2


def test_a_video_found_by_two_different_sources_is_one_row(db, topic, source, no_event_fanout):
    """The YouTube channel feed and YouTube search return the same video."""
    feed = DiscoverySource(
        topic_id=topic.id, kind=DiscoverySourceKind.RSS, name="Channel feed",
        is_active=True, config_json={"feed_url": "https://example/feed"},
    )
    db.add(feed)
    db.flush()

    svc = DiscoveryService(
        youtube_provider=StubProvider([[video("aaaaaaaaaaa")]]),
        rss_provider=StubProvider([[video("aaaaaaaaaaa")]]),
    )
    svc.run_source(db, topic=topic, source=source, commit=False)
    svc.run_source(db, topic=topic, source=feed, commit=False)

    rows = candidates(db)
    assert len(rows) == 1
    # Both sources are recorded: which feeds surface a video is itself a signal.
    assert set(rows[0].metadata_json["seen_via"]) == {"youtube_search", "rss"}


# ==========================================================================
# Idempotency details
# ==========================================================================


def test_first_discovery_time_is_never_rewritten(db, topic, source, no_event_fanout):
    """A video rediscovered on its fifth day is not new."""
    svc, _ = service([[video("aaaaaaaaaaa")], [video("aaaaaaaaaaa")]])

    svc.run_source(db, topic=topic, source=source, commit=False)
    original = candidates(db)[0].created_at

    svc.run_source(db, topic=topic, source=source, commit=False)

    assert candidates(db)[0].created_at == original


def test_last_seen_moves_on_every_sighting(db, topic, source, no_event_fanout):
    svc, _ = service([[video("aaaaaaaaaaa")], [video("aaaaaaaaaaa")]])

    svc.run_source(db, topic=topic, source=source, commit=False)
    first_seen = candidates(db)[0].last_seen_at
    assert first_seen is not None

    svc.run_source(db, topic=topic, source=source, commit=False)

    assert candidates(db)[0].last_seen_at >= first_seen


def test_a_rediscovery_does_not_un_reject_a_candidate(db, topic, source, no_event_fanout):
    """A human's decision is not undone because a feed repeated itself."""
    svc, _ = service([[video("aaaaaaaaaaa")], [video("aaaaaaaaaaa")]])
    svc.run_source(db, topic=topic, source=source, commit=False)

    row = candidates(db)[0]
    row.status = VideoCandidateStatus.REJECTED
    db.flush()

    svc.run_source(db, topic=topic, source=source, commit=False)

    assert candidates(db)[0].status == VideoCandidateStatus.REJECTED


def test_a_rediscovery_does_not_un_select_a_candidate(db, topic, source, no_event_fanout):
    svc, _ = service([[video("aaaaaaaaaaa")], [video("aaaaaaaaaaa")]])
    svc.run_source(db, topic=topic, source=source, commit=False)

    row = candidates(db)[0]
    row.status = VideoCandidateStatus.SELECTED
    db.flush()

    svc.run_source(db, topic=topic, source=source, commit=False)

    assert candidates(db)[0].status == VideoCandidateStatus.SELECTED


def test_metadata_refresh_updates_title_and_duration(db, topic, source, no_event_fanout):
    svc, _ = service([
        [video("aaaaaaaaaaa", title="Titulo antigo", duration_sec=None)],
        [video("aaaaaaaaaaa", title="Titulo corrigido", duration_sec=900)],
    ])

    svc.run_source(db, topic=topic, source=source, commit=False)
    svc.run_source(db, topic=topic, source=source, commit=False)

    row = candidates(db)[0]
    assert row.title == "Titulo corrigido"
    assert row.duration_sec == 900


def test_a_refresh_never_blanks_a_field_it_no_longer_knows(db, topic, source, no_event_fanout):
    """A degraded second lookup must not erase what the first one learned."""
    svc, _ = service([
        [video("aaaaaaaaaaa", duration_sec=750)],
        [video("aaaaaaaaaaa", duration_sec=None, title=None)],
    ])

    svc.run_source(db, topic=topic, source=source, commit=False)
    svc.run_source(db, topic=topic, source=source, commit=False)

    row = candidates(db)[0]
    assert row.duration_sec == 750
    assert row.title == "Entrevista completa"


# ==========================================================================
# The discovery/selection boundary
# ==========================================================================


def test_every_new_candidate_is_discovered_not_selected(db, topic, source, no_event_fanout):
    svc, _ = service([[video("aaaaaaaaaaa"), video("bbbbbbbbbbb")]])

    svc.run_source(db, topic=topic, source=source, commit=False)

    assert {row.status for row in candidates(db)} == {VideoCandidateStatus.DISCOVERED}
    assert all(row.selected_at is None for row in candidates(db))


def test_discovery_creates_no_pipeline_job(db, topic, source, no_event_fanout):
    """The boundary this PR exists to preserve."""
    from app.models.pipeline_job import PipelineJob

    svc, _ = service([[video("aaaaaaaaaaa")]])
    svc.run_source(db, topic=topic, source=source, commit=False)

    assert db.query(PipelineJob).count() == 0


def test_no_scores_are_written(db, topic, source, no_event_fanout):
    """Scoring is the next PR. Empty columns are not an invitation."""
    svc, _ = service([[video("aaaaaaaaaaa")]])
    svc.run_source(db, topic=topic, source=source, commit=False)

    row = candidates(db)[0]
    assert row.relevance_score is None
    assert row.trend_score is None
    assert row.quality_score is None
    assert row.scores_json is None


# ==========================================================================
# Storage of normalised fields
# ==========================================================================


def test_unmodelled_fields_are_stored_without_inventing_columns(db, topic, source, no_event_fanout):
    svc, _ = service([[video(
        "aaaaaaaaaaa", description="Descricao", channel_id="UC1",
        language="pt-BR", live_status="none", view_count=15234, is_short=False,
    )]])

    svc.run_source(db, topic=topic, source=source, commit=False)

    normalized = candidates(db)[0].metadata_json["normalized"]
    assert normalized["description"] == "Descricao"
    assert normalized["channel_id"] == "UC1"
    assert normalized["language"] == "pt-BR"
    assert normalized["view_count"] == 15234


def test_an_unknown_value_is_stored_as_null_not_zero(db, topic, source, no_event_fanout):
    svc, _ = service([[video("aaaaaaaaaaa", view_count=None, duration_sec=None)]])

    svc.run_source(db, topic=topic, source=source, commit=False)

    row = candidates(db)[0]
    assert row.duration_sec is None
    assert row.metadata_json["normalized"]["view_count"] is None


def test_an_unavailable_video_is_recorded_not_dropped(db, topic, source, no_event_fanout):
    svc, _ = service([[video("aaaaaaaaaaa", available=False, unavailable_reason="private")]])

    result = svc.run_source(db, topic=topic, source=source, commit=False)

    rows = candidates(db)
    assert len(rows) == 1
    assert rows[0].metadata_json["normalized"]["available"] is False
    assert result.unavailable_candidates == 1


def test_a_long_title_is_truncated_to_the_column(db, topic, source, no_event_fanout):
    svc, _ = service([[video("aaaaaaaaaaa", title="x" * 900)]])
    svc.run_source(db, topic=topic, source=source, commit=False)
    assert len(candidates(db)[0].title) == 500


# ==========================================================================
# Request construction
# ==========================================================================


def test_queries_come_from_configuration_not_from_the_provider(db, topic, source):
    request = DiscoveryService().build_request(topic, source)
    assert request.queries == ["futebol entrevista"]


def test_a_source_without_queries_falls_back_to_the_topic_keywords(db, topic):
    bare = DiscoverySource(
        topic_id=topic.id, kind=DiscoverySourceKind.YOUTUBE_SEARCH, config_json={}
    )
    request = DiscoveryService().build_request(topic, bare)
    assert request.queries == ["futebol entrevista", "futebol coletiva"]


def test_a_curated_feed_can_declare_that_it_wants_everything(db, topic):
    """An empty `queries` list is a decision, not an omission.

    A channel feed is already curated — the channel IS the filter — so inheriting the topic's
    keywords there would throw away most of what it publishes. Found by the live smoke, where
    a real channel feed returned 0 of its 15 entries.
    """
    feed = DiscoverySource(
        topic_id=topic.id,
        kind=DiscoverySourceKind.RSS,
        config_json={"feed_url": "https://example/feed", "queries": []},
    )
    request = DiscoveryService().build_request(topic, feed)
    assert request.queries == []


def test_freshness_defaults_to_a_recent_window(db, topic, source):
    request = DiscoveryService(default_freshness_days=7).build_request(topic, source)
    assert request.published_after is not None
    age = datetime.now(timezone.utc) - request.published_after
    assert timedelta(days=6, hours=23) < age < timedelta(days=7, hours=1)


def test_a_source_can_override_freshness(db, topic, source):
    source.config_json = {**source.config_json, "freshness_days": 1}
    request = DiscoveryService().build_request(topic, source)
    age = datetime.now(timezone.utc) - request.published_after
    assert age < timedelta(days=1, hours=1)


def test_language_and_region_come_from_the_topic(db, topic, source):
    request = DiscoveryService().build_request(topic, source)
    assert request.language == "pt"
    assert request.region == "BR"


# ==========================================================================
# Source failure handling
# ==========================================================================


class FailingProvider:
    name = "youtube"

    def __init__(self, error):
        self.error = error

    def is_configured(self):
        return True

    def discover(self, request):
        raise self.error


def test_a_provider_error_is_reported_not_raised(db, topic, source, no_event_fanout):
    from app.discovery.contracts import QUOTA_EXCEEDED, ProviderError

    svc = DiscoveryService(
        youtube_provider=FailingProvider(ProviderError(QUOTA_EXCEEDED, "spent"))
    )
    result = svc.run_source(db, topic=topic, source=source, commit=False)

    assert result.status == "failed"
    assert result.errors[0]["error_type"] == QUOTA_EXCEEDED
    assert result.errors[0]["retryable"] is False


def test_an_unconfigured_provider_is_unavailable_not_failed(db, topic, source, no_event_fanout):
    from app.discovery.contracts import ProviderUnavailable

    svc = DiscoveryService(youtube_provider=FailingProvider(ProviderUnavailable("no key")))
    result = svc.run_source(db, topic=topic, source=source, commit=False)

    assert result.status == "unavailable"
    assert result.errors[0]["error_type"] == "not_configured"
    assert candidates(db) == []


def test_an_unexpected_provider_crash_does_not_leak_its_message(db, topic, source, no_event_fanout):
    """A provider that interpolates its request into an exception would leak the key."""
    svc = DiscoveryService(
        youtube_provider=FailingProvider(RuntimeError("boom key=SECRET-KEY-VALUE"))
    )
    result = svc.run_source(db, topic=topic, source=source, commit=False)

    assert result.status == "failed"
    assert "SECRET-KEY-VALUE" not in str(result.errors)


def test_a_source_kind_with_no_provider_is_reported(db, topic, no_event_fanout):
    news = DiscoverySource(topic_id=topic.id, kind=DiscoverySourceKind.NEWS, config_json={})
    db.add(news)
    db.flush()

    result = DiscoveryService().run_source(db, topic=topic, source=news, commit=False)

    assert result.status == "unsupported"


def test_one_failing_source_does_not_stop_the_others(db, topic, source, no_event_fanout):
    from app.discovery.contracts import ProviderError, UPSTREAM_ERROR

    feed = DiscoverySource(
        topic_id=topic.id, kind=DiscoverySourceKind.RSS, is_active=True,
        config_json={"feed_url": "https://example/feed"},
    )
    db.add(feed)
    db.flush()
    db.refresh(topic)

    svc = DiscoveryService(
        youtube_provider=FailingProvider(ProviderError(UPSTREAM_ERROR, "down")),
        rss_provider=StubProvider([[video("bbbbbbbbbbb")]]),
    )
    results = svc.run_topic(db, topic=topic, commit=False)

    statuses = {r.source_kind: r.status for r in results}
    assert statuses["youtube_search"] == "failed"
    assert statuses["rss"] == "completed"
    assert len(candidates(db)) == 1


def test_an_inactive_source_is_skipped(db, topic, source, no_event_fanout):
    source.is_active = False
    db.flush()
    db.refresh(topic)

    results = DiscoveryService(youtube_provider=StubProvider([[video("a" * 11)]])).run_topic(
        db, topic=topic, commit=False
    )

    assert results == []


# ==========================================================================
# Run reporting
# ==========================================================================


def test_a_run_reports_the_counters_an_operator_asks_for(db, topic, source, no_event_fanout):
    svc, _ = service([[video("aaaaaaaaaaa"), video("bbbbbbbbbbb"), video("aaaaaaaaaaa")]])

    result = svc.run_source(db, topic=topic, source=source, commit=False)
    payload = result.as_dict()

    assert payload["results_received"] == 3
    assert payload["new_candidates"] == 2
    assert payload["existing_candidates"] == 1
    assert payload["api_calls"] == 2
    assert payload["provider"] == "youtube"
    assert payload["duration_ms"] >= 0
    assert payload["discovery_run_id"]


def test_a_run_emits_one_event_per_run_not_one_per_candidate(db, topic, source, no_event_fanout):
    """Fifty candidate events would bury the feed to say what the counters already say."""
    from app.models.pipeline_event import PipelineEvent

    svc, _ = service([[video(f"vid{index:07d}") for index in range(20)]])
    svc.run_source(db, topic=topic, source=source, commit=False)

    events = db.query(PipelineEvent).filter(PipelineEvent.service == "discovery").all()
    assert len(events) == 2, "started + completed"
    assert {event.stage for event in events} == {"discovery.started", "discovery.completed"}


def test_discovery_events_are_not_attached_to_a_pipeline_job(db, topic, source, no_event_fanout):
    from app.models.pipeline_event import PipelineEvent

    svc, _ = service([[video("aaaaaaaaaaa")]])
    svc.run_source(db, topic=topic, source=source, commit=False)

    events = db.query(PipelineEvent).filter(PipelineEvent.service == "discovery").all()
    assert all(event.pipeline_job_id is None for event in events)


# ==========================================================================
# Concurrency
# ==========================================================================


def test_a_concurrent_insert_of_the_same_video_yields_one_row(db, topic, source, no_event_fanout, monkeypatch):
    """Two runs finding the same video at the same moment.

    Both look up the identity, both see nothing, both insert. Only a database constraint can
    settle that — a SELECT-then-INSERT in the service cannot, because the window between the
    two statements is exactly where the other run commits. This forces that window open: the
    pre-check is blinded, so the insert reaches the unique index and takes the recovery path.
    """
    svc, _ = service([[video("aaaaaaaaaaa")]])
    svc.run_source(db, topic=topic, source=source, commit=False)
    assert len(candidates(db)) == 1

    real_query = db.query
    calls = {"n": 0}

    def blinded(*args, **kwargs):
        query = real_query(*args, **kwargs)
        if args and args[0] is VideoCandidate:
            calls["n"] += 1
            if calls["n"] == 1:
                # The first lookup is the pre-check: pretend the row is not there yet.
                class Blind:
                    def filter(self, *a, **k):
                        return self

                    def first(self):
                        return None

                return Blind()
        return query

    monkeypatch.setattr(db, "query", blinded)

    svc2, _ = service([[video("aaaaaaaaaaa", view_count=999)]])
    result = svc2.run_source(db, topic=topic, source=source, commit=False)

    monkeypatch.undo()
    rows = candidates(db)
    assert len(rows) == 1, "the unique index must collapse the race, not the service"
    assert result.new_candidates == 0
    assert result.existing_candidates == 1
    assert rows[0].metadata_json["normalized"]["view_count"] == 999, "the loser refreshes the winner"


def test_the_dedup_index_is_unique():
    """The constraint is declared on the model, not only in a migration."""
    from app.models.video_candidate import VideoCandidate as VC

    dedup = next(
        index for index in VC.__table__.indexes if index.name == "uq_video_candidates_dedup_hash"
    )
    assert dedup.unique is True
    assert [column.name for column in dedup.columns] == ["dedup_hash"]


def test_two_rows_cannot_share_a_dedup_hash(db, topic, source):
    """The database refuses it, whatever the service does."""
    from sqlalchemy.exc import IntegrityError

    shared = identity.dedup_hash("youtube", "aaaaaaaaaaa")
    for suffix in ("a", "b"):
        db.add(VideoCandidate(
            topic_id=topic.id, source_id=source.id, external_id=f"x{suffix}",
            url=f"https://example/{suffix}", dedup_hash=shared,
            status=VideoCandidateStatus.DISCOVERED,
        ))

    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
