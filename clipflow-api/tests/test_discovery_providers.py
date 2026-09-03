"""Provider contract and normalisation (PR-DISCOVERY-01).

Every response is a fixture served through an httpx transport. Nothing here reaches the
network — a test that depends on YouTube being up and a query still returning the same videos
is not a test.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.discovery import identity
from app.discovery.contracts import (
    FORBIDDEN,
    INVALID_REQUEST,
    MALFORMED_RESPONSE,
    QUOTA_EXCEEDED,
    RATE_LIMITED,
    TIMEOUT,
    UNAUTHORIZED,
    UPSTREAM_ERROR,
    DiscoveryRequest,
    ProviderError,
    ProviderUnavailable,
)
from app.discovery.rss_provider import RssDiscoveryProvider
from app.discovery.youtube_provider import (
    YouTubeSearchProvider,
    parse_iso8601_duration,
    parse_published_at,
)

API_KEY = "test-key-not-a-real-one"


# ==========================================================================
# Fixtures
# ==========================================================================


def search_item(video_id: str, title: str = "Entrevista completa") -> dict:
    return {
        "id": {"kind": "youtube#video", "videoId": video_id},
        "snippet": {
            "title": title,
            "description": "Descricao do video",
            "channelId": "UC_channel_1",
            "channelTitle": "Canal Esportivo",
            "publishedAt": "2026-08-30T12:00:00Z",
            "liveBroadcastContent": "none",
            "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{video_id}/hq.jpg"}},
        },
    }


def video_item(video_id: str, *, duration: str = "PT12M30S", **overrides) -> dict:
    item = {
        "id": video_id,
        "snippet": {
            "title": "Entrevista completa",
            "description": "Descricao do video",
            "channelId": "UC_channel_1",
            "channelTitle": "Canal Esportivo",
            "publishedAt": "2026-08-30T12:00:00Z",
            "liveBroadcastContent": "none",
            "defaultAudioLanguage": "pt-BR",
            "tags": ["futebol", "entrevista"],
            "categoryId": "17",
            "thumbnails": {"maxres": {"url": f"https://i.ytimg.com/vi/{video_id}/max.jpg"}},
        },
        "contentDetails": {"duration": duration, "definition": "hd", "caption": "false"},
        "statistics": {"viewCount": "15234", "likeCount": "870", "commentCount": "45"},
        "status": {"privacyStatus": "public", "uploadStatus": "processed"},
    }
    for key, value in overrides.items():
        item[key] = value
    return item


def transport(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def youtube(handler, **kwargs) -> YouTubeSearchProvider:
    return YouTubeSearchProvider(API_KEY, client=transport(handler), **kwargs)


def ok_handler(search_items, video_items):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": search_items})
        return httpx.Response(200, json={"items": video_items})

    handler.calls = calls
    return handler


# ==========================================================================
# YouTube — success
# ==========================================================================


def test_a_successful_search_returns_normalized_videos():
    handler = ok_handler([search_item("aaaaaaaaaaa")], [video_item("aaaaaaaaaaa")])
    fetch = youtube(handler).discover(DiscoveryRequest(queries=["futebol entrevista"]))

    assert len(fetch.videos) == 1
    video = fetch.videos[0]
    assert video.provider == "youtube"
    assert video.external_id == "aaaaaaaaaaa"
    assert video.canonical_url == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert video.title == "Entrevista completa"
    assert video.channel_name == "Canal Esportivo"
    assert video.duration_sec == 750
    assert video.view_count == 15234
    assert video.language == "pt-BR"
    assert video.available is True


def test_an_empty_result_is_not_an_error():
    handler = ok_handler([], [])
    fetch = youtube(handler).discover(DiscoveryRequest(queries=["nada disso existe"]))

    assert fetch.videos == []
    assert fetch.errors == []
    # Only the search ran: there were no ids to look up.
    assert fetch.api_calls == 1


def test_metadata_is_fetched_in_one_batch_not_one_call_per_video():
    """Quota discipline: 50 videos cost one videos.list call, not fifty."""
    ids = [f"vid{index:07d}" for index in range(50)]
    handler = ok_handler(
        [search_item(video_id) for video_id in ids],
        [video_item(video_id) for video_id in ids],
    )
    fetch = youtube(handler).discover(DiscoveryRequest(queries=["futebol"], max_results=50))

    assert len(fetch.videos) == 50
    assert fetch.api_calls == 2, "one search + one batched lookup"
    video_calls = [c for c in handler.calls if c.url.path.endswith("/videos")]
    assert len(video_calls) == 1


def test_more_than_fifty_ids_are_split_into_batches():
    ids = [f"vid{index:07d}" for index in range(50)] + [f"alt{index:07d}" for index in range(20)]
    search_items = [search_item(video_id) for video_id in ids[:50]]
    search_items_2 = [search_item(video_id) for video_id in ids[50:]]
    seen = {"search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen["search"] += 1
            items = search_items if seen["search"] == 1 else search_items_2
            return httpx.Response(200, json={"items": items})
        requested = request.url.params.get("id", "").split(",")
        return httpx.Response(200, json={"items": [video_item(v) for v in requested]})

    fetch = youtube(handler).discover(
        DiscoveryRequest(queries=["a", "b"], max_results=50)
    )
    assert len(fetch.videos) == 70
    # 2 searches + 2 batches (50 then 20).
    assert fetch.api_calls == 4


def test_duplicate_ids_across_queries_are_collapsed_before_lookup():
    """Two overlapping queries returning the same video must not be looked up twice."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [search_item("aaaaaaaaaaa")]})
        requested = request.url.params.get("id", "").split(",")
        assert requested == ["aaaaaaaaaaa"]
        return httpx.Response(200, json={"items": [video_item("aaaaaaaaaaa")]})

    fetch = youtube(handler).discover(DiscoveryRequest(queries=["a", "b"]))
    assert len(fetch.videos) == 1


def test_identical_queries_are_not_searched_twice():
    handler = ok_handler([search_item("aaaaaaaaaaa")], [video_item("aaaaaaaaaaa")])
    youtube(handler).discover(DiscoveryRequest(queries=["futebol", "futebol", " futebol "]))

    searches = [c for c in handler.calls if c.url.path.endswith("/search")]
    assert len(searches) == 1, "a repeated query costs 100 quota units to learn nothing"


def test_freshness_is_sent_upstream():
    handler = ok_handler([], [])
    after = datetime(2026, 8, 1, tzinfo=timezone.utc)
    youtube(handler).discover(DiscoveryRequest(queries=["futebol"], published_after=after))

    search = handler.calls[0]
    assert search.url.params.get("publishedAfter") == "2026-08-01T00:00:00Z"


def test_max_results_is_capped_at_the_api_ceiling():
    handler = ok_handler([], [])
    youtube(handler).discover(DiscoveryRequest(queries=["futebol"], max_results=500))

    assert int(handler.calls[0].url.params.get("maxResults")) == 50


# ==========================================================================
# YouTube — failures
# ==========================================================================


def quota_body(reason: str) -> dict:
    return {"error": {"code": 403, "errors": [{"reason": reason, "message": "..."}]}}


def test_quota_exhaustion_is_classified_and_not_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(403, json=quota_body("quotaExceeded"))

    fetch = youtube(handler).discover(DiscoveryRequest(queries=["futebol"]))

    assert fetch.errors[0]["error_type"] == QUOTA_EXCEEDED
    assert fetch.errors[0]["retryable"] is False
    assert attempts["n"] == 1, "retrying a spent allowance only burns the next window"


def test_quota_exhaustion_stops_the_remaining_queries():
    """The allowance is gone for every query, not just this one."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(403, json=quota_body("dailyLimitExceeded"))

    fetch = youtube(handler).discover(DiscoveryRequest(queries=["a", "b", "c", "d"]))

    assert attempts["n"] == 1
    assert fetch.truncated is True
    assert len(fetch.errors) == 1


def test_rate_limiting_is_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, json={"error": {"code": 429}})
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [search_item("aaaaaaaaaaa")]})
        return httpx.Response(200, json={"items": [video_item("aaaaaaaaaaa")]})

    fetch = youtube(handler).discover(DiscoveryRequest(queries=["futebol"]))

    assert len(fetch.videos) == 1
    assert attempts["n"] >= 3


def test_rate_limiting_gives_up_after_the_attempt_budget():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    fetch = youtube(handler, max_attempts=2).discover(DiscoveryRequest(queries=["futebol"]))
    assert fetch.errors[0]["error_type"] == RATE_LIMITED
    assert fetch.errors[0]["retryable"] is True


def test_a_bad_key_is_not_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, json={})

    fetch = youtube(handler).discover(DiscoveryRequest(queries=["futebol"]))

    assert fetch.errors[0]["error_type"] == UNAUTHORIZED
    assert attempts["n"] == 1, "an invalid key returns the same answer every time"


def test_a_server_error_is_retried_then_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    fetch = youtube(handler, max_attempts=2).discover(DiscoveryRequest(queries=["futebol"]))
    assert fetch.errors[0]["error_type"] == UPSTREAM_ERROR


def test_forbidden_without_a_quota_reason_is_not_quota():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=quota_body("accessNotConfigured"))

    fetch = youtube(handler).discover(DiscoveryRequest(queries=["futebol"]))
    assert fetch.errors[0]["error_type"] == FORBIDDEN


def test_a_malformed_body_is_classified():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    fetch = youtube(handler).discover(DiscoveryRequest(queries=["futebol"]))
    assert fetch.errors[0]["error_type"] == MALFORMED_RESPONSE


def test_a_response_without_an_items_array_is_malformed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"kind": "youtube#searchListResponse"})

    fetch = youtube(handler).discover(DiscoveryRequest(queries=["futebol"]))
    assert fetch.errors[0]["error_type"] == MALFORMED_RESPONSE


def test_a_timeout_is_classified():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    fetch = youtube(handler, max_attempts=2).discover(DiscoveryRequest(queries=["futebol"]))
    assert fetch.errors[0]["error_type"] == TIMEOUT


def test_no_queries_is_an_invalid_request():
    with pytest.raises(ProviderError) as excinfo:
        youtube(ok_handler([], [])).discover(DiscoveryRequest(queries=[]))
    assert excinfo.value.error_type == INVALID_REQUEST


def test_no_api_key_means_unavailable_not_broken():
    provider = YouTubeSearchProvider(None)
    assert provider.is_configured() is False
    with pytest.raises(ProviderUnavailable):
        provider.discover(DiscoveryRequest(queries=["futebol"]))


def test_an_error_never_carries_the_api_key():
    """Google echoes request parameters into error payloads, and one of them is the key."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"message": f"Bad key {API_KEY}", "errors": [{"reason": "forbidden"}]}},
        )

    fetch = youtube(handler).discover(DiscoveryRequest(queries=["futebol"]))
    assert API_KEY not in json.dumps(fetch.errors)


# ==========================================================================
# YouTube — degraded metadata
# ==========================================================================


def test_a_failed_metadata_lookup_still_yields_candidates():
    """Losing enrichment costs fields; losing the candidates costs the discovery."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [search_item("aaaaaaaaaaa")]})
        return httpx.Response(500, json={})

    fetch = youtube(handler, max_attempts=1).discover(DiscoveryRequest(queries=["futebol"]))

    assert len(fetch.videos) == 1
    video = fetch.videos[0]
    assert video.title == "Entrevista completa", "the search snippet survives"
    assert video.duration_sec is None, "an unknown duration is None, never 0"
    assert video.view_count is None


def test_a_video_missing_from_the_lookup_is_marked_unavailable():
    """Deleted, private or region-blocked between search and lookup."""
    handler = ok_handler([search_item("aaaaaaaaaaa")], [])
    fetch = youtube(handler).discover(DiscoveryRequest(queries=["futebol"]))

    video = fetch.videos[0]
    assert video.available is False
    assert video.unavailable_reason == "not_returned_by_videos_list"


def test_a_private_video_is_marked_unavailable():
    item = video_item("aaaaaaaaaaa")
    item["status"]["privacyStatus"] = "private"
    handler = ok_handler([search_item("aaaaaaaaaaa")], [item])

    video = youtube(handler).discover(DiscoveryRequest(queries=["f"])).videos[0]
    assert video.available is False
    assert video.unavailable_reason == "private"


def test_optional_metadata_absent_stays_none():
    bare = {"id": "aaaaaaaaaaa", "snippet": {"title": "Sem metadados"}}
    handler = ok_handler([search_item("aaaaaaaaaaa")], [bare])

    video = youtube(handler).discover(DiscoveryRequest(queries=["f"])).videos[0]
    assert video.duration_sec is None
    assert video.view_count is None
    assert video.like_count is None
    assert video.language is None
    assert video.published_at is None


# ==========================================================================
# YouTube — live and shorts
# ==========================================================================


def test_a_live_broadcast_is_captured_not_dropped():
    item = video_item("aaaaaaaaaaa", duration="P0D")
    item["snippet"]["liveBroadcastContent"] = "live"
    handler = ok_handler([search_item("aaaaaaaaaaa")], [item])

    video = youtube(handler).discover(DiscoveryRequest(queries=["f"])).videos[0]
    assert video.live_status == "live"
    assert video.duration_sec is None, "a live stream has no duration; it is not zero"


def test_an_upcoming_premiere_is_captured():
    item = video_item("aaaaaaaaaaa")
    item["snippet"]["liveBroadcastContent"] = "upcoming"
    handler = ok_handler([search_item("aaaaaaaaaaa")], [item])

    assert youtube(handler).discover(DiscoveryRequest(queries=["f"])).videos[0].live_status == "upcoming"


def test_a_short_is_flagged_but_kept():
    handler = ok_handler([search_item("aaaaaaaaaaa")], [video_item("aaaaaaaaaaa", duration="PT45S")])

    video = youtube(handler).discover(DiscoveryRequest(queries=["f"])).videos[0]
    assert video.is_short is True
    assert video.duration_sec == 45


def test_a_longform_video_is_not_flagged_as_short():
    handler = ok_handler([search_item("aaaaaaaaaaa")], [video_item("aaaaaaaaaaa", duration="PT25M")])
    assert youtube(handler).discover(DiscoveryRequest(queries=["f"])).videos[0].is_short is None


# ==========================================================================
# Parsing
# ==========================================================================


@pytest.mark.parametrize(
    "value,expected",
    [
        ("PT12M30S", 750),
        ("PT45S", 45),
        ("PT1H2M30S", 3750),
        ("PT2H", 7200),
        ("P1DT2H", 93600),
        ("P0D", None),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_duration_parsing(value, expected):
    assert parse_iso8601_duration(value) == expected


def test_published_at_is_timezone_aware():
    parsed = parse_published_at("2026-08-30T12:00:00Z")
    assert parsed == datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    assert parsed.tzinfo is not None


def test_unparseable_published_at_is_none():
    assert parse_published_at("last tuesday") is None
    assert parse_published_at(None) is None


# ==========================================================================
# RSS provider
# ==========================================================================

YOUTUBE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/">
  <entry>
    <yt:videoId>aaaaaaaaaaa</yt:videoId>
    <yt:channelId>UC_channel_1</yt:channelId>
    <title>Coletiva completa do tecnico</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=aaaaaaaaaaa"/>
    <author><name>Canal Esportivo</name></author>
    <published>2026-08-30T12:00:00+00:00</published>
    <media:group>
      <media:thumbnail url="https://i.ytimg.com/vi/aaaaaaaaaaa/hq.jpg"/>
      <media:description>Resumo da coletiva</media:description>
    </media:group>
  </entry>
</feed>
"""

GENERIC_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Portal</title>
  <item>
    <title>Analise da rodada</title>
    <link>https://portal.example/noticias/analise-da-rodada</link>
    <description>Texto</description>
    <pubDate>Sat, 30 Aug 2026 12:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Outra materia</title>
    <link>https://portal.example/noticias/outra</link>
    <pubDate>Fri, 29 Aug 2026 09:00:00 +0000</pubDate>
  </item>
</channel></rss>
"""


def rss(body: str, status: int = 200) -> RssDiscoveryProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode("utf-8"))

    return RssDiscoveryProvider(client=transport(handler))


def test_a_youtube_channel_feed_yields_youtube_identity():
    """The key result: a video found via feed and via search is ONE candidate."""
    fetch = rss(YOUTUBE_FEED).discover(
        DiscoveryRequest(queries=[], config={"feed_url": "https://example/feed"})
    )

    video = fetch.videos[0]
    assert video.provider == "youtube"
    assert video.external_id == "aaaaaaaaaaa"
    assert video.canonical_url == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert video.dedup_key == "youtube:aaaaaaaaaaa"


def test_a_generic_feed_derives_identity_from_the_permalink():
    fetch = rss(GENERIC_RSS).discover(
        DiscoveryRequest(queries=[], config={"feed_url": "https://example/feed"})
    )

    assert len(fetch.videos) == 2
    first = fetch.videos[0]
    assert first.provider == "rss"
    assert first.external_id.startswith("d_")
    assert first.canonical_url == "https://portal.example/noticias/analise-da-rodada"
    assert first.title == "Analise da rodada"


def test_feed_entries_are_filtered_by_query_terms():
    fetch = rss(GENERIC_RSS).discover(
        DiscoveryRequest(queries=["analise"], config={"feed_url": "https://example/feed"})
    )
    assert [v.title for v in fetch.videos] == ["Analise da rodada"]


def test_feed_entries_are_filtered_by_freshness():
    after = datetime(2026, 8, 30, tzinfo=timezone.utc)
    fetch = rss(GENERIC_RSS).discover(
        DiscoveryRequest(
            queries=[], published_after=after, config={"feed_url": "https://example/feed"}
        )
    )
    assert [v.title for v in fetch.videos] == ["Analise da rodada"]


def test_a_feed_without_a_url_is_unavailable():
    with pytest.raises(ProviderUnavailable):
        rss(GENERIC_RSS).discover(DiscoveryRequest(queries=[], config={}))


def test_a_malformed_feed_is_classified():
    with pytest.raises(ProviderError) as excinfo:
        rss("<rss><channel><item>").discover(
            DiscoveryRequest(queries=[], config={"feed_url": "https://example/feed"})
        )
    assert excinfo.value.error_type == MALFORMED_RESPONSE


def test_a_missing_feed_is_classified():
    with pytest.raises(ProviderError) as excinfo:
        rss("", status=404).discover(
            DiscoveryRequest(queries=[], config={"feed_url": "https://example/feed"})
        )
    assert excinfo.value.error_type == "not_found"
    assert excinfo.value.retryable is False


def test_max_results_truncates_a_long_feed():
    fetch = rss(GENERIC_RSS).discover(
        DiscoveryRequest(queries=[], max_results=1, config={"feed_url": "https://example/feed"})
    )
    assert len(fetch.videos) == 1
    assert fetch.truncated is True
