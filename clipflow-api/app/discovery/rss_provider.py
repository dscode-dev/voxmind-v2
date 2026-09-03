"""RSS / Atom feed discovery.

A second real provider, and the reason there is an interface at all: with one implementation
the abstraction would simply be YouTube's shape wearing a Protocol, and the first genuinely
different source would break it.

It is genuinely different. RSS has no search — a feed is a fixed list, so ``queries`` are
applied as a client-side filter over titles rather than sent upstream. Most feeds carry no
duration, no view count and no language. Those stay ``None``.

It also covers YouTube's own channel feed, which is a public Atom document at
``/feeds/videos.xml?channel_id=…`` and costs no quota. When an entry is a YouTube video the
provider extracts the real video id, so a video found through a channel feed and the same
video found through search deduplicate onto one row — which is the whole point of putting
identity in one place.

Parsed with the standard library's ``ElementTree``. External entities are not resolved, so a
hostile feed cannot use XXE to read local files.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import httpx

from app.discovery import identity
from app.discovery.contracts import (
    FORBIDDEN,
    INVALID_REQUEST,
    MALFORMED_RESPONSE,
    NOT_FOUND,
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

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

# A feed body far past this is not a feed we should be parsing into memory.
_MAX_BYTES = 5 * 1024 * 1024


class RssDiscoveryProvider:
    """Reads an RSS or Atom feed and normalises its entries."""

    name = identity.RSS

    def __init__(
        self,
        *,
        timeout_sec: float = 15.0,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.max_attempts = max(1, max_attempts)
        self._client = client

    def is_configured(self) -> bool:
        """Always available: configuration is the feed URL, which arrives per request."""
        return True

    def discover(self, request: DiscoveryRequest) -> DiscoveryFetch:
        feed_url = str(request.config.get("feed_url") or "").strip()
        if not feed_url:
            raise ProviderUnavailable("this RSS source has no feed_url configured")

        fetch = DiscoveryFetch()
        body = self._get(feed_url)
        fetch.api_calls = 1

        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise ProviderError(MALFORMED_RESPONSE, "feed is not well-formed XML") from exc

        entries = root.findall(".//atom:entry", _NS) or root.findall(".//item")
        terms = [q.strip().lower() for q in request.queries if q and q.strip()]

        for entry in entries:
            video = self._normalize(entry, feed_url)
            if video is None:
                continue
            if request.published_after and video.published_at:
                if video.published_at < request.published_after:
                    continue
            if request.published_before and video.published_at:
                if video.published_at > request.published_before:
                    continue
            # A feed cannot be searched, so terms filter what it returned. No terms means the
            # whole feed, which is the normal case for a curated channel.
            if terms and not _matches(video, terms):
                continue
            fetch.videos.append(video)
            if len(fetch.videos) >= max(1, request.max_results):
                fetch.truncated = len(entries) > len(fetch.videos)
                break

        return fetch

    # ------------------------------------------------------------- normalise

    def _normalize(self, entry: Any, feed_url: str) -> DiscoveredVideo | None:
        link = _entry_link(entry)
        title = _text(entry, ("atom:title", "title"))
        published = _entry_published(entry)

        # A YouTube entry keeps its real video id, so the same video found here and through
        # search collapses onto one row instead of two.
        youtube_id = _text(entry, ("yt:videoId",)) or (identity.extract_youtube_id(link or "") if link else None)
        if youtube_id:
            return DiscoveredVideo(
                provider=identity.YOUTUBE,
                external_id=youtube_id,
                canonical_url=identity.canonical_youtube_url(youtube_id),
                title=title,
                description=_media_description(entry),
                channel_id=_text(entry, ("yt:channelId",)),
                channel_name=_text(entry, ("atom:author/atom:name", "author/name")),
                published_at=published,
                thumbnail_url=_media_thumbnail(entry),
                raw_metadata={"discovered_via": "rss", "feed_url": feed_url},
            )

        # A non-YouTube entry has no provider-assigned id, so identity is derived from its
        # permalink — the one attribute that identifies the item rather than describing it.
        if not link:
            return None
        external_id = identity.derived_external_id(link)
        if not external_id:
            return None

        return DiscoveredVideo(
            provider=identity.RSS,
            external_id=external_id,
            canonical_url=link,
            title=title,
            description=_text(entry, ("atom:summary", "description")),
            channel_name=_text(entry, ("atom:author/atom:name", "author/name")),
            published_at=published,
            thumbnail_url=_media_thumbnail(entry),
            raw_metadata={"discovered_via": "rss", "feed_url": feed_url},
        )

    # ------------------------------------------------------------------ http

    def _get(self, url: str) -> bytes:
        last: ProviderError | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._request(url)
            except httpx.TimeoutException as exc:
                last = ProviderError(TIMEOUT, "feed request timed out")
                if attempt >= self.max_attempts:
                    raise last from exc
                continue
            except httpx.HTTPError as exc:
                last = ProviderError(UPSTREAM_ERROR, "feed transport error")
                if attempt >= self.max_attempts:
                    raise last from exc
                continue

            if response.status_code == 200:
                content = response.content or b""
                if len(content) > _MAX_BYTES:
                    raise ProviderError(
                        MALFORMED_RESPONSE, f"feed exceeds {_MAX_BYTES} bytes"
                    )
                return content

            error = _classify(response.status_code)
            if not error.retryable or attempt >= self.max_attempts:
                raise error
            last = error

        raise last or ProviderError(UPSTREAM_ERROR, "feed request failed")

    def _request(self, url: str) -> httpx.Response:
        headers = {"User-Agent": "ClipFlow-Discovery/1.0"}
        if self._client is not None:
            return self._client.get(url, timeout=self.timeout_sec, headers=headers)
        with httpx.Client(timeout=self.timeout_sec, follow_redirects=True) as client:
            return client.get(url, headers=headers)


def _classify(status: int) -> ProviderError:
    if status == 401:
        return ProviderError(UNAUTHORIZED, "feed requires authentication", status_code=status)
    if status == 403:
        return ProviderError(FORBIDDEN, "feed access forbidden", status_code=status)
    if status == 404:
        return ProviderError(NOT_FOUND, "feed not found", status_code=status)
    if status == 429:
        return ProviderError(RATE_LIMITED, "feed rate limited", status_code=status)
    if status == 400:
        return ProviderError(INVALID_REQUEST, "feed rejected the request", status_code=status)
    if status >= 500:
        return ProviderError(UPSTREAM_ERROR, f"feed returned {status}", status_code=status)
    return ProviderError(UPSTREAM_ERROR, f"feed returned {status}", status_code=status)


def _matches(video: DiscoveredVideo, terms: list[str]) -> bool:
    haystack = f"{video.title or ''} {video.description or ''}".lower()
    return any(term in haystack for term in terms)


def _text(entry: Any, paths: tuple[str, ...]) -> str | None:
    for path in paths:
        found = entry.find(path, _NS) if ":" in path else entry.find(path)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return None


def _entry_link(entry: Any) -> str | None:
    link = entry.find("atom:link", _NS)
    if link is not None and link.get("href"):
        return str(link.get("href")).strip()
    plain = entry.find("link")
    if plain is not None:
        if plain.get("href"):
            return str(plain.get("href")).strip()
        if (plain.text or "").strip():
            return plain.text.strip()
    guid = entry.find("guid")
    if guid is not None and (guid.text or "").strip():
        value = guid.text.strip()
        return value if value.startswith("http") else None
    return None


def _entry_published(entry: Any) -> datetime | None:
    raw = _text(entry, ("atom:published", "atom:updated", "pubDate"))
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _media_thumbnail(entry: Any) -> str | None:
    node = entry.find(".//media:thumbnail", _NS)
    if node is not None and node.get("url"):
        return str(node.get("url"))
    return None


def _media_description(entry: Any) -> str | None:
    node = entry.find(".//media:description", _NS)
    if node is not None and (node.text or "").strip():
        return node.text.strip()
    return _text(entry, ("atom:summary", "description"))
