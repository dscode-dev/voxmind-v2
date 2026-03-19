from app.observability.artifact_tracker import ArtifactTracker
from app.observability.runtime_tracker import RuntimeTracker


def configure_logging():
    from app.observability.logging import configure_logging as _configure_logging

    return _configure_logging()


def get_logger(name: str):
    from app.observability.logging import get_logger as _get_logger

    return _get_logger(name)


__all__ = ["ArtifactTracker", "RuntimeTracker", "configure_logging", "get_logger"]
