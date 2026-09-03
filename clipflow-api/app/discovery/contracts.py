"""The contract every discovery provider speaks.

Discovery answers "what content exists?". It does not answer "what should we produce?" —
that is selection, and it is deliberately not in this PR. A provider fetches, normalises and
returns; it never persists, never scores and never decides.

Two providers implement this (YouTube search and RSS) because one would not prove anything:
an interface with a single implementation is just that implementation with extra steps, and
the shape it settles into is invariably the shape of its only caller.

Fields a provider cannot fill stay ``None``. Never 0, never "": a view count of zero and an
unknown view count are different facts, and collapsing them makes the difference
unrecoverable later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

# A source that fails must fail visibly and specifically. "Discovery failed" tells an
# operator nothing about whether to wait, fix a key, or raise a quota.
QUOTA_EXCEEDED = "quota_exceeded"
RATE_LIMITED = "rate_limited"
UNAUTHORIZED = "unauthorized"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
TIMEOUT = "timeout"
UPSTREAM_ERROR = "upstream_error"
MALFORMED_RESPONSE = "malformed_response"
INVALID_REQUEST = "invalid_request"
NOT_CONFIGURED = "not_configured"

# Retrying these can plausibly succeed: the condition is transient or time-based.
RETRYABLE_ERRORS = frozenset({RATE_LIMITED, TIMEOUT, UPSTREAM_ERROR})

# Retrying these repeats the same request and gets the same answer, while spending quota.
# QUOTA_EXCEEDED belongs here rather than with the retryables: the daily allowance resets on
# a schedule, not on a backoff, so hammering it only burns the next window too.
NON_RETRYABLE_ERRORS = frozenset(
    {QUOTA_EXCEEDED, UNAUTHORIZED, FORBIDDEN, NOT_FOUND, INVALID_REQUEST,
     MALFORMED_RESPONSE, NOT_CONFIGURED}
)


class ProviderError(Exception):
    """A classified provider failure.

    Carries no response body: providers echo request parameters back in error payloads, and
    for an authenticated API that can include the key itself.
    """

    def __init__(self, error_type: str, message: str, *, status_code: int | None = None) -> None:
        self.error_type = error_type
        self.status_code = status_code
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        return self.error_type in RETRYABLE_ERRORS

    def as_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": str(self),
            "status_code": self.status_code,
            "retryable": self.retryable,
        }


class ProviderUnavailable(ProviderError):
    """The provider has no usable configuration (no credential, no feed URL).

    Distinct from a failure: nothing is wrong, the source simply cannot run. It must not be
    silently substituted with fabricated results.
    """

    def __init__(self, message: str) -> None:
        super().__init__(NOT_CONFIGURED, message)


# ---------------------------------------------------------------------------
# Normalised result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredVideo:
    """One discovered item, in provider-independent form.

    ``provider`` + ``external_id`` is the identity. Everything else is description, and all
    of it is optional because feeds vary in what they publish.
    """

    provider: str
    external_id: str
    canonical_url: str
    title: str | None = None
    description: str | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    published_at: datetime | None = None
    duration_sec: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    language: str | None = None
    thumbnail_url: str | None = None
    # "none" | "live" | "upcoming" for YouTube; None when the source does not say.
    live_status: str | None = None
    # True/False when known, None when the source gives no signal.
    is_short: bool | None = None
    available: bool = True
    unavailable_reason: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """``provider:external_id`` — the natural identity of the underlying video."""
        return f"{self.provider}:{self.external_id}"

    def normalized_fields(self) -> dict[str, Any]:
        """The descriptive fields, for storage alongside the modelled columns."""
        return {
            "description": self.description,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "view_count": self.view_count,
            "like_count": self.like_count,
            "comment_count": self.comment_count,
            "language": self.language,
            "live_status": self.live_status,
            "is_short": self.is_short,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class DiscoveryRequest:
    """What a provider is asked to look for.

    Built from the ContentTopic and the DiscoverySource config, never from constants inside a
    provider — a query hardcoded in the fetching code cannot be changed without a deploy.
    """

    queries: list[str] = field(default_factory=list)
    published_after: datetime | None = None
    published_before: datetime | None = None
    language: str | None = None
    region: str | None = None
    max_results: int = 25
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveryFetch:
    """What a provider returns: results, plus what it cost and what went wrong."""

    videos: list[DiscoveredVideo] = field(default_factory=list)
    api_calls: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "results": len(self.videos),
            "api_calls": self.api_calls,
            "errors": self.errors,
            "truncated": self.truncated,
        }


class DiscoveryProvider(Protocol):
    """Fetch and normalise. Nothing else."""

    name: str

    def is_configured(self) -> bool:
        """Whether this provider can run at all. False means unavailable, not broken."""
        ...

    def discover(self, request: DiscoveryRequest) -> DiscoveryFetch:
        """Fetch results for the request, normalised. Raises ProviderError on failure."""
        ...
