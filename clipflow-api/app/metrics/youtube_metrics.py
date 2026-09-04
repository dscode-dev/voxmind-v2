"""Reading a published video's counters from the YouTube Data API.

**Data API, not the Analytics API.** ``videos.list?part=statistics`` returns ``viewCount``,
``likeCount`` and ``commentCount``, which is the baseline this PR needs, and it works with
``youtube.readonly`` — a scope every connected target already granted in PR-PUBLISH-01. The
Analytics API offers far richer figures (watch time, retention, traffic sources) but requires
``yt-analytics.readonly``: a new scope, which every existing target would have to be
disconnected and reconnected to obtain. That is a real cost to pay before anyone has looked at
a single view count, so it is deliberately not paid here. The scope is named in the report as
the thing to add when the richer metrics are actually wanted.

**OAuth, not an API key.** The channel's own uploads start out ``private``, and an API key
sees nothing private. The same refresh token the publisher uses is what makes them readable,
which is also why collection is grouped by target: one credential per channel, and calling
channel A with channel B's token would be both wrong and a way to leak nothing useful.

**Batched.** ``videos.list`` accepts 50 comma-separated ids for one quota unit, where 50
separate calls would cost 50. The discovery provider already relies on this; so does this.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.metrics.contracts import (
    NOT_RETURNED,
    OK,
    UNAVAILABLE,
    MetricsAuthError,
    MetricsFetchResult,
    VideoMetrics,
)
from app.publishing.contracts import PublishCredential
from app.publishing.youtube_oauth import OAuthError, YouTubeOAuthClient

logger = logging.getLogger(__name__)

PROVIDER = "youtube"
VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"

# videos.list accepts 50 ids per request, for one quota unit. The same limit the discovery
# provider works to.
MAX_BATCH = 50

# Privacy states in which counters exist and mean something. A video still processing or
# rejected by YouTube has statistics that are not yet about an audience.
MEANINGFUL_UPLOAD_STATES = ("processed", "uploaded")


class YouTubeVideoMetricsProvider:
    """Implements ``VideoMetricsProvider`` against the Data API."""

    provider = PROVIDER
    max_batch = MAX_BATCH

    def __init__(
        self,
        *,
        oauth: YouTubeOAuthClient | None = None,
        client: httpx.Client | None = None,
        timeout_sec: float = 30.0,
    ) -> None:
        self._oauth = oauth
        self._client = client
        self._timeout = timeout_sec

    def fetch_metrics(
        self, video_ids: list[str], *, credential: PublishCredential
    ) -> MetricsFetchResult:
        """One or more batched calls for the ids given. Never partial-fails silently."""
        result = MetricsFetchResult(requested=len(video_ids))
        if not video_ids:
            return result

        try:
            token = self._oauth_client(credential).refresh_access_token(
                credential.refresh_token
            )
        except OAuthError as exc:
            # A credential problem, not a video problem. Raised so the caller can apply the
            # existing reconnect semantics rather than recording 50 missing videos.
            raise MetricsAuthError(exc.code, recoverable=exc.recoverable) from exc

        for batch in _batches(video_ids, MAX_BATCH):
            self._fetch_batch(batch, token.access_token, result)

        # Everything asked for but not answered. Recorded as not_returned rather than left
        # out, so a video that quietly disappeared is visible instead of simply absent.
        for video_id in video_ids:
            result.metrics.setdefault(
                video_id, VideoMetrics(external_video_id=video_id,
                                       availability=NOT_RETURNED)
            )
        return result

    # ------------------------------------------------------------------ internals

    def _fetch_batch(self, batch: list[str], access_token: str,
                     result: MetricsFetchResult) -> None:
        result.calls += 1
        try:
            response = self._http().get(
                VIDEOS_ENDPOINT,
                params={"part": "statistics,status", "id": ",".join(batch),
                        "maxResults": len(batch)},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # One batch failed. Its videos stay unanswered and are recorded as not_returned;
            # the other batches are unaffected, because losing 50 observations should not
            # cost the other 450.
            logger.warning(
                "youtube_metrics_batch_failed",
                extra={"error_type": type(exc).__name__, "batch_size": len(batch)},
            )
            return

        if response.status_code == 401 or _reason(response) == "invalid_grant":
            raise MetricsAuthError(_reason(response), recoverable=False)
        if response.status_code != 200:
            reason = _reason(response)
            logger.warning(
                "youtube_metrics_rejected",
                extra={"status": response.status_code, "reason": reason,
                       "batch_size": len(batch)},
            )
            # Only the code, never the body: Google echoes request parameters into some of
            # its error messages.
            result.error_code = result.error_code or reason
            return

        for item in (response.json() or {}).get("items") or []:
            metrics = _parse(item)
            if metrics is not None:
                result.metrics[metrics.external_video_id] = metrics
                result.returned += 1

    def _oauth_client(self, credential: PublishCredential) -> YouTubeOAuthClient:
        if self._oauth is not None:
            return self._oauth
        return YouTubeOAuthClient(
            client_id=credential.client_id,
            client_secret=credential.client_secret,
            # Not used by a refresh, but the client requires a complete configuration
            # rather than half of one.
            redirect_uri="https://clipflow.invalid/unused",
            client=self._client,
        )

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=self._timeout)


def _parse(item: dict[str, Any]) -> VideoMetrics | None:
    video_id = str(item.get("id") or "").strip()
    if not video_id:
        return None

    stats = item.get("statistics") or {}
    status = item.get("status") or {}
    upload_status = status.get("uploadStatus")

    availability = OK
    if upload_status and upload_status not in MEANINGFUL_UPLOAD_STATES:
        # Still processing, or rejected. The counters exist but are not yet about an
        # audience, so they are recorded with a verdict rather than as a clean measurement.
        availability = UNAVAILABLE

    return VideoMetrics(
        external_video_id=video_id,
        availability=availability,
        # Absent keys stay None. YouTube omits likeCount when the owner hides likes and
        # commentCount when comments are disabled - reading either as 0 would invent data.
        view_count=_count(stats.get("viewCount")),
        like_count=_count(stats.get("likeCount")),
        comment_count=_count(stats.get("commentCount")),
        privacy_status=status.get("privacyStatus"),
        provider_metadata={
            "upload_status": upload_status,
            "privacy_status": status.get("privacyStatus"),
            "made_for_kids": status.get("madeForKids"),
        },
    )


def _count(value: Any) -> int | None:
    """A provider counter, or None.

    YouTube sends counters as strings. A malformed or negative one is dropped rather than
    stored: a negative view count is not an observation, and letting it through would put a
    value in the series that no later reader could interpret.
    """
    if value is None:
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _reason(response: httpx.Response) -> str:
    """The machine-readable error only."""
    try:
        body = response.json() or {}
    except ValueError:
        return f"http_{response.status_code}"
    error = body.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        errors = error.get("errors") or []
        if errors and isinstance(errors[0], dict) and errors[0].get("reason"):
            return str(errors[0]["reason"])
    return f"http_{response.status_code}"


def _batches(items: list[str], size: int) -> list[list[str]]:
    return [items[index:index + size] for index in range(0, len(items), size)]
