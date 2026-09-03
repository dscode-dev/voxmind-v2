"""Structured logging for the worker.

`logging.LoggerAdapter.process` *replaces* ``kwargs["extra"]`` with the adapter's own
mapping, so every ``logger.info(..., extra={"job_id": ...})`` call in this codebase was
silently discarded and the JSON formatter emitted ``job_id=None`` for every line. Python
3.13 added ``merge_extra=True``; we target 3.11, so the merge is implemented here.

Three sources are merged, lowest precedence first:

1. the adapter defaults (every correlation field, so the JSON formatter never KeyErrors);
2. the ambient context bound with :func:`bind_context` (worker_id, job_id, attempt …);
3. the ``extra`` passed at the call site.

The ambient context is a ContextVar, which lets the queue runner bind ``worker_id`` and
``attempt`` once per claimed job instead of threading them through the pipeline.
"""

import contextvars
import logging
import sys
from contextlib import contextmanager
from typing import Any

from app.settings import settings

try:
    from pythonjsonlogger import jsonlogger
except ModuleNotFoundError:  # pragma: no cover
    jsonlogger = None


# The canonical correlation fields. Every log record carries all of them so operators can
# filter on any one, and so the JSON formatter always has the attributes it names.
LOG_CONTEXT_FIELDS = (
    "job_id",
    # The authoritative run this execution belongs to (PR-STATE-01). job_id identifies where
    # the artifacts live; pipeline_job_id identifies the run whose state is being changed.
    # Both are needed to join a log line to a state transition.
    "pipeline_job_id",
    "pipeline_stage",
    "step",
    "status",
    "attempt",
    "worker_id",
)

_log_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "clipflow_log_context",
    default={},
)


def bind_context(**fields: Any) -> contextvars.Token:
    """Bind correlation fields for every subsequent log record in this context."""
    merged = {**_log_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    return _log_context.set(merged)


def reset_context(token: contextvars.Token) -> None:
    _log_context.reset(token)


@contextmanager
def log_context(**fields: Any):
    token = bind_context(**fields)
    try:
        yield
    finally:
        reset_context(token)


def get_context() -> dict[str, Any]:
    return dict(_log_context.get())


class ContextLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter that merges `extra` instead of replacing it."""

    def process(self, msg, kwargs):
        merged: dict[str, Any] = {field: None for field in LOG_CONTEXT_FIELDS}
        merged.update(self.extra or {})
        merged.update(_log_context.get())

        call_site = kwargs.get("extra") or {}
        merged.update({key: value for key, value in call_site.items() if value is not None})

        kwargs["extra"] = merged
        return msg, kwargs


def configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level.upper())

    handler = logging.StreamHandler(sys.stdout)

    if settings.log_json and jsonlogger is not None:
        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s "
            + " ".join(f"%({field})s" for field in LOG_CONTEXT_FIELDS)
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        )

    handler.setFormatter(formatter)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)


def get_logger(name: str) -> logging.LoggerAdapter:
    logger = logging.getLogger(name)
    return ContextLoggerAdapter(logger, {field: None for field in LOG_CONTEXT_FIELDS})
