import logging
import sys

from app.settings import settings

try:
    from pythonjsonlogger import jsonlogger
except ModuleNotFoundError:  # pragma: no cover
    jsonlogger = None


def configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level.upper())

    handler = logging.StreamHandler(sys.stdout)

    if settings.log_json and jsonlogger is not None:
        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s "
            "%(job_id)s %(pipeline_stage)s %(step)s %(status)s"
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
    return logging.LoggerAdapter(
        logger,
        {
            "job_id": None,
            "pipeline_stage": None,
            "step": None,
            "status": None,
        },
    )
