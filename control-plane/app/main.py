from .logging_setup import setup_logging
from .settings import settings

setup_logging(settings.log_level)

from .api import app  # noqa: E402
