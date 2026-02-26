import logging
from pythonjsonlogger import jsonlogger

def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s %(process)d %(threadName)s"
    )
    handler.setFormatter(formatter)
    root.handlers = [handler]
