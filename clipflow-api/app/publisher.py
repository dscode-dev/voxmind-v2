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
from app.publishing.temp_cleanup import sweep_stale_spools
from app.services.publish_runtime import PublisherRuntime

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Startup only. The leak this addresses comes from a process dying mid-upload, and a
    # process that has died is one that is about to be started again - so start is exactly
    # when the leftovers are there and nothing is using them. A periodic daemon would be
    # more machinery for a case that cannot arise while the process is alive.
    try:
        result = sweep_stale_spools(stale_after_sec=settings.publish_temp_stale_sec)
        if result.removed:
            logger.info("publisher_temp_cleanup", extra=result.as_dict())
    except Exception:  # noqa: BLE001
        # Housekeeping must not stop a publisher from starting.
        logger.exception("publisher_temp_cleanup_failed")

    PublisherRuntime().run()


if __name__ == "__main__":
    main()
