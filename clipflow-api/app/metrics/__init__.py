"""Reading what happened to videos that were already published.

The boundary is one-directional by design: this package imports the publishing contracts to
know *which* videos exist and how to authenticate to read them, and nothing in discovery,
selection, admission, production or publishing imports anything from here. That is what makes
"measurement, not optimization" a structural property rather than a promise — there is no
edge for a feedback loop to travel along yet, and adding one has to be a deliberate act
someone can see in a diff.
"""
from app.metrics.contracts import (
    NOT_RETURNED,
    OK,
    UNAVAILABLE,
    MetricsAuthError,
    MetricsFetchResult,
    VideoMetrics,
    VideoMetricsProvider,
)
from app.metrics.youtube_metrics import YouTubeVideoMetricsProvider

__all__ = [
    "OK",
    "NOT_RETURNED",
    "UNAVAILABLE",
    "VideoMetrics",
    "MetricsFetchResult",
    "VideoMetricsProvider",
    "MetricsAuthError",
    "YouTubeVideoMetricsProvider",
]
