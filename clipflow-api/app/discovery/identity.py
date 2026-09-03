"""Canonical identity for discovered videos.

Deduplication cannot be based on the URL string. YouTube alone serves the same video under
at least five shapes:

    https://www.youtube.com/watch?v=ABC123
    https://youtu.be/ABC123
    https://www.youtube.com/shorts/ABC123
    https://m.youtube.com/watch?v=ABC123&t=42s
    https://www.youtube.com/embed/ABC123

They are five different strings and one video. Comparing text produces five rows; extracting
the id produces one. So identity is always ``provider:external_id`` where an external id
exists — a value the provider assigns, that survives a retitle, a re-share and a URL rewrite.

Titles are never part of identity. Two channels covering the same match publish near-identical
titles for genuinely different videos, and one channel re-uploads the same video under a new
title. A title collides both ways.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlparse

YOUTUBE = "youtube"
RSS = "rss"

# YouTube ids are 11 characters of URL-safe base64.
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com",
}
_YOUTU_BE_HOSTS = {"youtu.be", "www.youtu.be"}

# Path prefixes that carry the id as the final segment.
_PATH_PREFIXES = ("/shorts/", "/embed/", "/v/", "/live/")


def extract_youtube_id(url: str) -> str | None:
    """The video id from any YouTube URL shape, or None if this is not one.

    Returns None rather than guessing: a channel or playlist URL has no video id, and
    inventing one from a path segment would fabricate identity.
    """
    text = str(url or "").strip()
    if not text:
        return None
    if _YOUTUBE_ID.match(text):
        # Already a bare id.
        return text

    if "//" not in text:
        text = f"https://{text}"

    try:
        parsed = urlparse(text)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host in _YOUTU_BE_HOSTS:
        candidate = path.lstrip("/").split("/")[0]
        return candidate if _YOUTUBE_ID.match(candidate) else None

    if host not in _YOUTUBE_HOSTS:
        return None

    if path in ("/watch", "/watch/"):
        values = parse_qs(parsed.query or "").get("v") or []
        candidate = values[0] if values else ""
        return candidate if _YOUTUBE_ID.match(candidate) else None

    for prefix in _PATH_PREFIXES:
        if path.startswith(prefix):
            candidate = path[len(prefix):].split("/")[0]
            return candidate if _YOUTUBE_ID.match(candidate) else None

    # Some share links carry ?v= on an unexpected path.
    values = parse_qs(parsed.query or "").get("v") or []
    if values and _YOUTUBE_ID.match(values[0]):
        return values[0]

    return None


def canonical_youtube_url(video_id: str) -> str:
    """One URL shape per video, so stored URLs are comparable by eye as well as by id."""
    return f"https://www.youtube.com/watch?v={video_id}"


def canonical_url(provider: str, external_id: str, fallback_url: str | None = None) -> str:
    if provider == YOUTUBE and external_id:
        return canonical_youtube_url(external_id)
    return str(fallback_url or external_id or "")


def dedup_key(provider: str, external_id: str) -> str:
    """The human-readable identity: ``youtube:dQw4w9WgXcQ``."""
    return f"{provider}:{external_id}"


def dedup_hash(provider: str, external_id: str) -> str:
    """A fixed-width hash of the identity, for the unique column.

    Hashed rather than stored raw so that identities from providers with long or awkward ids
    still fit one bounded, indexable column. The readable key is kept in metadata; this is
    what the database enforces uniqueness on.
    """
    return hashlib.sha256(dedup_key(provider, external_id).encode("utf-8")).hexdigest()


def derived_external_id(*parts: str | None) -> str:
    """An identity for a source that publishes no stable id of its own.

    Derived from attributes that identify the *item*, typically its permalink — never from a
    title alone. Falls back to a hash so the result is a bounded, opaque token that cannot be
    mistaken for a provider-assigned id.
    """
    material = "|".join(part.strip() for part in parts if part and part.strip())
    if not material:
        return ""
    return "d_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:22]
