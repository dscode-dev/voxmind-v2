from __future__ import annotations

import logging
import sys
import orjson


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
            "time": self.formatTime(record, self.datefmt),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return orjson.dumps(payload).decode("utf-8")


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
