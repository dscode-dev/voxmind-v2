"""Entrypoint for the publication worker container.

    python -m app.publisher

Deliberately tiny. It configures logging, builds the runtime, and blocks — everything that
can be decided lives in :mod:`app.services.publish_runtime`, so this file has no behaviour to
test and no reason to change.

The process shares the API image because the publishing code is already there. It shares
nothing else: no HTTP server, no port, no GPU, and its own restart lifecycle.
"""
from __future__ import annotations

import logging

from app.core.settings import settings
from app.services.publish_runtime import PublisherRuntime


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    PublisherRuntime().run()


if __name__ == "__main__":
    main()
