"""YouTube Data API v3 discovery.

Two calls per query, not one per video:

    search.list  -> up to 50 ids          (100 quota units)
    videos.list  -> metadata for all 50   (1 quota unit)

``search.list`` returns only snippet fields — no duration, no view count, no live status —
so the metadata has to come from ``videos.list``. Calling that once per video would cost the
same single unit fifty times over and take fifty round trips; ``videos.list`` accepts up to
50 comma-separated ids in one request, so it is called once per batch.

Quota is the binding constraint. Search costs 100 units against a default daily allowance of
10,000, so roughly 100 searches a day exist in total. That is why the query list comes from
the topic (deduplicated) rather than from a loop somewhere, why ``max_results`` is capped,
and why a quota error is classified as non-retryable: the allowance resets on a clock, not
on a backoff, and retrying only spends tomorrow's.

No scraping. This is the documented API, and nothing here parses a YouTube web page.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.discovery import identity
from app.discovery.contracts import (
    FORBIDDEN,
    INVALID_REQUEST,
    MALFORMED_RESPONSE,
    NOT_FOUND,
    QUOTA_EXCEEDED,
    RATE_LIMITED,
    TIMEOUT,
    UNAUTHORIZED,
    UPSTREAM_ERROR,
    DiscoveredVideo,
    DiscoveryFetch,
    DiscoveryRequest,
    ProviderError,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/youtube/v3"

# The API's own ceiling per call.
_MAX_PAGE = 50
# videos.list accepts up to 50 ids per request.
_BATCH = 50

# ISO-8601 durations as YouTube emits them: PT1H2M30S, PT45S, P1DT2H.
_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

# Google's reason strings for a spent allowance, distinct from ordinary rate limiting.
_QUOTA_REASONS = {"quotaexceeded", "dailylimitexceeded"}
_RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded"}


def parse_iso8601_duration(value: str | None) -> int | None:
    """Seconds, or None when absent or unparseable.

    None is not zero: a live stream has no duration, and reporting 0 would make it look like
    an empty video.
    """
    if not value:
        return None
    match = _DURATION.match(str(value).strip())
    if not match:
        return None
    parts = {key: int(number) for key, number in match.groupdict(default="0").items()}
    total = (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )
    return total or None


def parse_published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class YouTubeSearchProvider:
    """Searches YouTube for a topic's queries and returns normalised results."""

    name = identity.YOUTUBE

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_sec: float = 15.0,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.api_key = (api_key or "").strip() or None
        self.timeout_sec = timeout_sec
        self.max_attempts = max(1, max_attempts)
        self._client = client

    def is_configured(self) -> bool:
        return self.api_key is not None

    # ------------------------------------------------------------------ fetch

    def discover(self, request: DiscoveryRequest) -> DiscoveryFetch:
        if not self.is_configured():
            raise ProviderUnavailable(
                "YOUTUBE_API_KEY is not set; the YouTube provider cannot run"
            )

        fetch = DiscoveryFetch()
        # Deduplicated because two topic queries overlapping costs 100 quota units to learn
        # what the caller already knew.
        queries = list(dict.fromkeys(q.strip() for q in request.queries if q and q.strip()))
        if not queries:
            raise ProviderError(INVALID_REQUEST, "no queries configured for this source")

        per_query = max(1, min(request.max_results, _MAX_PAGE))
        collected: dict[str, dict[str, Any]] = {}

        for query in queries:
            try:
                items, calls = self._search(query, request, per_query)
            except ProviderError as exc:
                fetch.errors.append({"query": query, **exc.as_dict()})
                fetch.api_calls += 1
                # A spent allowance applies to every remaining query, so stop rather than
                # generate one identical error per query.
                if exc.error_type == QUOTA_EXCEEDED:
                    fetch.truncated = True
                    break
                continue

            fetch.api_calls += calls
            for item in items:
                video_id = (item.get("id") or {}).get("videoId")
                if video_id and video_id not in collected:
                    collected[video_id] = item

        if not collected:
            return fetch

        details, calls = self._video_details(list(collected))
        fetch.api_calls += calls

        for video_id, search_item in collected.items():
            detail = details.get(video_id)
            fetch.videos.append(self._normalize(video_id, search_item, detail))

        return fetch

    def _search(
        self,
        query: str,
        request: DiscoveryRequest,
        per_query: int,
    ) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "q": query,
            "maxResults": per_query,
            "order": "date",
            "key": self.api_key,
        }
        if request.published_after:
            params["publishedAfter"] = _rfc3339(request.published_after)
        if request.published_before:
            params["publishedBefore"] = _rfc3339(request.published_before)
        if request.language:
            params["relevanceLanguage"] = request.language
        if request.region:
            params["regionCode"] = request.region

        payload = self._get("search", params)
        items = payload.get("items")
        if not isinstance(items, list):
            raise ProviderError(MALFORMED_RESPONSE, "search response has no items array")
        return items, 1

    def _video_details(self, video_ids: list[str]) -> tuple[dict[str, dict[str, Any]], int]:
        """One call per 50 ids. Failure here degrades the result, it does not lose it."""
        details: dict[str, dict[str, Any]] = {}
        calls = 0

        for start in range(0, len(video_ids), _BATCH):
            batch = video_ids[start : start + _BATCH]
            try:
                payload = self._get(
                    "videos",
                    {
                        "part": "snippet,contentDetails,statistics,status,liveStreamingDetails",
                        "id": ",".join(batch),
                        "maxResults": _BATCH,
                        "key": self.api_key,
                    },
                )
            except ProviderError:
                # The search results are still real. Losing enrichment costs duration and
                # view counts, which stay None — losing the candidates costs the discovery.
                logger.warning(
                    "youtube_video_details_failed",
                    extra={"batch_size": len(batch)},
                )
                calls += 1
                continue

            calls += 1
            for item in payload.get("items") or []:
                if isinstance(item, dict) and item.get("id"):
                    details[str(item["id"])] = item

        return details, calls

    # ------------------------------------------------------------- normalise

    def _normalize(
        self,
        video_id: str,
        search_item: dict[str, Any],
        detail: dict[str, Any] | None,
    ) -> DiscoveredVideo:
        search_snippet = search_item.get("snippet") or {}
        detail = detail or {}
        snippet = detail.get("snippet") or search_snippet
        content = detail.get("contentDetails") or {}
        stats = detail.get("statistics") or {}
        status = detail.get("status") or {}

        duration_sec = parse_iso8601_duration(content.get("duration"))
        live_status = snippet.get("liveBroadcastContent") or None

        # A video the search returned but videos.list did not is deleted, private or
        # region-blocked. It is recorded as unavailable, never dropped: the history is what
        # makes a later cooldown or trend signal possible.
        available = bool(detail) and status.get("privacyStatus") != "private"
        unavailable_reason = None
        if not detail:
            unavailable_reason = "not_returned_by_videos_list"
        elif status.get("privacyStatus") == "private":
            unavailable_reason = "private"

        return DiscoveredVideo(
            provider=self.name,
            external_id=video_id,
            canonical_url=identity.canonical_youtube_url(video_id),
            title=snippet.get("title"),
            description=snippet.get("description"),
            channel_id=snippet.get("channelId"),
            channel_name=snippet.get("channelTitle"),
            published_at=parse_published_at(snippet.get("publishedAt")),
            duration_sec=duration_sec,
            view_count=_as_int(stats.get("viewCount")),
            like_count=_as_int(stats.get("likeCount")),
            comment_count=_as_int(stats.get("commentCount")),
            language=snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage"),
            thumbnail_url=_best_thumbnail(snippet.get("thumbnails")),
            live_status=live_status,
            # YouTube exposes no "is a Short" flag. Duration is the only signal available, and
            # it is a heuristic: <= 60s is the Shorts ceiling, but a short landscape video is
            # not a Short. Recorded as a hint for selection, not as a fact to filter on.
            is_short=(duration_sec is not None and duration_sec <= 60) or None,
            available=available,
            unavailable_reason=unavailable_reason,
            raw_metadata=_trimmed_raw(snippet, content, stats, status),
        )

    # ------------------------------------------------------------------ http

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """One GET with bounded retries.

        Retries only what a retry can fix. An invalid key or a spent quota returns the same
        answer every time, and re-asking costs quota to learn nothing.
        """
        url = f"{API_ROOT}/{path}"
        last: ProviderError | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._request(url, params)
            except httpx.TimeoutException as exc:
                last = ProviderError(TIMEOUT, f"youtube {path} timed out")
                logger.warning("youtube_timeout", extra={"path": path, "attempt": attempt})
                if attempt >= self.max_attempts:
                    raise last from exc
                continue
            except httpx.HTTPError as exc:
                last = ProviderError(UPSTREAM_ERROR, f"youtube {path} transport error")
                if attempt >= self.max_attempts:
                    raise last from exc
                continue

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ProviderError(
                        MALFORMED_RESPONSE, f"youtube {path} returned invalid JSON"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ProviderError(
                        MALFORMED_RESPONSE, f"youtube {path} returned a non-object body"
                    )
                return payload

            error = self._classify(response, path)
            if not error.retryable or attempt >= self.max_attempts:
                raise error
            last = error

        raise last or ProviderError(UPSTREAM_ERROR, f"youtube {path} failed")

    def _request(self, url: str, params: dict[str, Any]) -> httpx.Response:
        if self._client is not None:
            return self._client.get(url, params=params, timeout=self.timeout_sec)
        with httpx.Client(timeout=self.timeout_sec) as client:
            return client.get(url, params=params)

    def _classify(self, response: httpx.Response, path: str) -> ProviderError:
        """Map an HTTP failure onto a specific, actionable error type.

        The response body is read for Google's ``reason`` field and then discarded. It is
        never included in the message: Google echoes request parameters into error payloads,
        and for this API those include the key.
        """
        status = response.status_code
        reason = ""
        try:
            body = response.json()
            errors = ((body.get("error") or {}).get("errors") or [{}])
            reason = str(errors[0].get("reason") or "").lower()
        except Exception:
            reason = ""

        if status == 403:
            if reason in _QUOTA_REASONS:
                return ProviderError(
                    QUOTA_EXCEEDED,
                    "youtube daily quota exhausted; resets on Google's schedule",
                    status_code=status,
                )
            if reason in _RATE_REASONS:
                return ProviderError(RATE_LIMITED, "youtube rate limit hit", status_code=status)
            return ProviderError(FORBIDDEN, f"youtube {path} forbidden", status_code=status)
        if status == 401:
            return ProviderError(UNAUTHORIZED, "youtube rejected the API key", status_code=status)
        if status == 429:
            return ProviderError(RATE_LIMITED, "youtube rate limit hit", status_code=status)
        if status == 404:
            return ProviderError(NOT_FOUND, f"youtube {path} not found", status_code=status)
        if status == 400:
            return ProviderError(
                INVALID_REQUEST, f"youtube rejected the {path} request", status_code=status
            )
        if status >= 500:
            return ProviderError(
                UPSTREAM_ERROR, f"youtube {path} returned {status}", status_code=status
            )
        return ProviderError(
            UPSTREAM_ERROR, f"youtube {path} returned {status}", status_code=status
        )


def _rfc3339(value: datetime) -> str:
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _best_thumbnail(thumbnails: Any) -> str | None:
    if not isinstance(thumbnails, dict):
        return None
    for size in ("maxres", "standard", "high", "medium", "default"):
        entry = thumbnails.get(size)
        if isinstance(entry, dict) and entry.get("url"):
            return str(entry["url"])
    return None


def _trimmed_raw(*sections: dict[str, Any]) -> dict[str, Any]:
    """A small, fixed projection of the provider payload.

    Kept for debugging and for fields a later PR may need. Bounded on purpose: storing whole
    API responses turns a metadata column into unbounded growth, and the description alone
    can run to thousands of characters.
    """
    snippet, content, stats, status = sections
    return {
        "categoryId": snippet.get("categoryId"),
        "tags": (snippet.get("tags") or [])[:15],
        "definition": content.get("definition"),
        "caption": content.get("caption"),
        "licensedContent": content.get("licensedContent"),
        "privacyStatus": status.get("privacyStatus"),
        "uploadStatus": status.get("uploadStatus"),
        "statistics_present": bool(stats),
    }
