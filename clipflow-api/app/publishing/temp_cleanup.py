"""Reclaiming publication spool files a killed process left behind.

``MinioMediaSource.download`` spools the media to a temporary file and removes it in a
``finally``. That covers every ordinary ending — success, failure, an ambiguous outcome — but
``finally`` does not run on ``SIGKILL``, and a publication spool is the size of a video. A few
of those and a container fills its disk with files nothing will ever look at again.

**Only our own files, only old ones.** Two rules, and both matter:

* the name must match the prefix this codebase writes (``clipflow-publish-*.mp4``), so a sweep
  can never touch an unrelated file that happens to share a directory;
* it must be older than a threshold comfortably longer than any single upload, so a file a
  *live* publisher is streaming from right now is never removed.

The second rule is what makes this safe when several publisher processes share a directory.
The deployed topology does not — each container has its own ``/tmp`` and no volume is mounted
— but the sweep does not depend on that being true, because it is the kind of fact that
changes in a later compose edit without anyone rereading this file.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Must match MinioMediaSource's NamedTemporaryFile(prefix=..., suffix=...).
SPOOL_PREFIX = "clipflow-publish-"
SPOOL_SUFFIX = ".mp4"
SPOOL_GLOB = f"{SPOOL_PREFIX}*{SPOOL_SUFFIX}"

# A ceiling on the sweep itself. A directory with a pathological number of matches must not
# turn startup into an unbounded scan; whatever is left over is found by the next start.
MAX_SCAN = 5_000


@dataclass
class CleanupResult:
    scanned: int = 0
    removed: int = 0
    bytes_reclaimed: int = 0
    oldest_age_sec: int = 0
    skipped_recent: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "files_removed": self.removed,
            "bytes_reclaimed": self.bytes_reclaimed,
            "oldest_age_sec": self.oldest_age_sec,
            "skipped_recent": self.skipped_recent,
            "errors": self.errors,
        }


def sweep_stale_spools(
    directory: str | os.PathLike | None = None,
    *,
    stale_after_sec: int,
    now: float | None = None,
    max_scan: int = MAX_SCAN,
) -> CleanupResult:
    """Delete our own spool files older than the threshold. Never raises.

    Cleanup failing must not stop a publisher from starting: the process still works with a
    cluttered temp directory, and refusing to boot over housekeeping would turn a disk-space
    problem into an outage.
    """
    result = CleanupResult()
    root = Path(directory) if directory else Path(tempfile.gettempdir())
    moment = now if now is not None else time.time()

    try:
        candidates = root.glob(SPOOL_GLOB)
    except OSError as exc:
        logger.warning("publish_temp_sweep_unavailable",
                       extra={"error_type": type(exc).__name__})
        return result

    for path in candidates:
        if result.scanned >= max_scan:
            logger.info("publish_temp_sweep_truncated", extra={"max_scan": max_scan})
            break
        result.scanned += 1

        try:
            if not path.is_file():
                continue
            stat = path.stat()
            age = int(moment - stat.st_mtime)
            result.oldest_age_sec = max(result.oldest_age_sec, age)

            if age < stale_after_sec:
                # Young enough that a live upload could still be reading it.
                result.skipped_recent += 1
                continue

            size = stat.st_size
            path.unlink()
            result.removed += 1
            result.bytes_reclaimed += size
        except FileNotFoundError:
            # Someone else got there first. Not an error.
            continue
        except OSError as exc:
            result.errors += 1
            logger.warning(
                "publish_temp_sweep_failed",
                # The name only, never the full path: it carries no secret, but there is no
                # reason to write filesystem layout into logs either.
                extra={"file": path.name, "error_type": type(exc).__name__},
            )

    if result.removed or result.errors:
        logger.info("publish_temp_sweep", extra=result.as_dict())
    else:
        logger.debug("publish_temp_sweep_clean", extra=result.as_dict())
    return result
