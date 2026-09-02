from app.observability.artifact_tracker import ArtifactTracker
from app.observability.runtime_tracker import RuntimeTracker


def configure_logging():
    from app.observability.logging import configure_logging as _configure_logging

    return _configure_logging()


def get_logger(name: str):
    from app.observability.logging import get_logger as _get_logger

    return _get_logger(name)


def bind_context(**fields):
    from app.observability.logging import bind_context as _bind_context

    return _bind_context(**fields)


def log_context(**fields):
    from app.observability.logging import log_context as _log_context

    return _log_context(**fields)


__all__ = [
    "ArtifactTracker",
    "RuntimeTracker",
    "bind_context",
    "configure_logging",
    "get_logger",
    "log_context",
]
