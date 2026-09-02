"""Minimal failure classification.

Only two outcomes matter to the queue: retry the job, or send it to the dead-letter queue.
The goal is to avoid burning three attempts on a deterministic failure (a malformed AI
response will fail identically every time), while still retrying anything that looks
transient (network, storage, upstream API).

Anything unrecognised is treated as **retryable**. A transient fault misclassified as fatal
loses work permanently; a deterministic fault misclassified as transient merely costs two
extra attempts before landing in the DLQ. The cheaper mistake is the default.
"""
from __future__ import annotations

import json
import subprocess

RETRYABLE = "retryable"
NON_RETRYABLE = "non_retryable"


# Deterministic failures raised by the existing pipeline. Retrying these re-runs the same
# input through the same code and fails the same way.
_NON_RETRYABLE_MESSAGE_MARKERS = (
    "invalid json received from ai",
    "invalid response: shorts_content missing",
    "shorts_content is empty",
    "no valid cuts after filtering",
    "manual response missing",
    "invalid pipeline_stage",
    "transcription returned no segments",
    "all yt-dlp download strategies failed",
)

_NON_RETRYABLE_EXCEPTION_NAMES = (
    "AIResponseValidationError",
    "JSONDecodeError",
)

# Transient markers that win even when the exception type looks generic.
_RETRYABLE_MESSAGE_MARKERS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "temporarily unavailable",
    "service unavailable",
    "too many requests",
    "rate limit",
    "bad gateway",
    "gateway timeout",
    "no route to host",
    "name or service not known",
)


def classify(error: BaseException) -> str:
    """Return RETRYABLE or NON_RETRYABLE for a pipeline failure."""
    name = type(error).__name__
    message = str(error).lower()

    # A subprocess that timed out is transient by construction.
    if isinstance(error, subprocess.TimeoutExpired):
        return RETRYABLE

    if any(marker in message for marker in _RETRYABLE_MESSAGE_MARKERS):
        return RETRYABLE

    if name in _NON_RETRYABLE_EXCEPTION_NAMES:
        return NON_RETRYABLE

    if isinstance(error, json.JSONDecodeError):
        return NON_RETRYABLE

    # ValueError/TypeError/KeyError signal a payload or contract problem, not a blip.
    if isinstance(error, (ValueError, TypeError, KeyError)) and not isinstance(
        error, json.JSONDecodeError
    ):
        return NON_RETRYABLE

    if any(marker in message for marker in _NON_RETRYABLE_MESSAGE_MARKERS):
        return NON_RETRYABLE

    return RETRYABLE


def is_retryable(error: BaseException) -> bool:
    return classify(error) == RETRYABLE
