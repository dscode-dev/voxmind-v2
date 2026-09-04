"""What the ingestion service asks a provider for, and what it gets back.

Small on purpose. One method, three optional counters, and an availability verdict — enough
for a baseline and nothing more. There is no score here and no derived figure: a provider
reports what it observed, and inventing a number on top of that would make an interpretation
indistinguishable from a measurement.

**Absent is not zero.** Every counter is optional, and ``None`` is a real answer meaning "the
provider did not disclose this". YouTube omits ``likeCount`` when the owner hides likes and
``commentCount`` when comments are disabled, so this is a live case rather than a defensive
one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# The video was returned and measured.
OK = "ok"
# Asked for, not in the response. Deleted, private to another account, or region-blocked -
# the API does not say which. Emphatically not "zero views".
NOT_RETURNED = "not_returned"
# Returned, but in a state where counters are not meaningful.
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class VideoMetrics:
    """One video's counters, as the provider reported them."""

    external_video_id: str
    availability: str = OK
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    privacy_status: str | None = None
    # A few non-secret provider fields worth keeping for an audit. Never a token, never
    # request headers, never a whole response body.
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def measured(self) -> bool:
        return self.availability == OK


@dataclass
class MetricsFetchResult:
    """One provider call's worth of answers."""

    metrics: dict[str, VideoMetrics] = field(default_factory=dict)
    requested: int = 0
    returned: int = 0
    calls: int = 0
    # Set when the whole call failed rather than individual videos being absent. The
    # distinction matters: an auth failure is a target problem, a missing id is a video
    # problem, and treating one as the other loses the difference.
    error_code: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_code is not None


class VideoMetricsProvider(Protocol):
    """The port. Deliberately not a generic analytics abstraction."""

    provider: str
    max_batch: int

    def fetch_metrics(
        self, video_ids: list[str], *, credential: Any
    ) -> MetricsFetchResult:
        ...


class MetricsAuthError(RuntimeError):
    """The target's credential was rejected.

    Carries the provider's machine-readable code so the caller can reuse the existing
    reconnect semantics rather than inventing a second notion of a broken credential.
    """

    def __init__(self, code: str, *, recoverable: bool = True) -> None:
        self.code = code
        self.recoverable = recoverable
        super().__init__(f"metrics auth failed: {code}")
