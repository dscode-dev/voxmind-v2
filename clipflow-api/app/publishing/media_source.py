"""Getting the final MP4 from MinIO to the publisher without holding it in memory.

A final clip is tens to hundreds of megabytes. ``get_object().read()`` would put all of it in
the API process's heap, and several concurrent publishes would put several copies there.

**Why a temporary file rather than piping the MinIO stream straight to httpx.** The resumable
protocol needs to *seek*: a chunk that fails is re-sent from its offset, and a resumed session
starts from wherever the server says it got to. A MinIO response is a forward-only HTTP body,
so a seek would mean re-requesting the object and skipping bytes. Spooling to disk once, then
seeking freely, is both simpler and cheaper — and disk is the resource this box has most of.

The file is streamed in fixed-size blocks on the way down too, so the object never exists in
memory at either end. It is deleted when the context exits, including on failure.
"""
from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from minio import Minio

from app.core.settings import settings

logger = logging.getLogger(__name__)

DOWNLOAD_BLOCK_BYTES = 4 * 1024 * 1024
CONTENT_TYPE = "video/mp4"


class MediaUnavailableError(RuntimeError):
    """The artifact this publication needs is not in storage.

    Fail-closed and non-retryable at the domain level: a missing final render is not a
    transient condition that another upload attempt would fix.
    """


@dataclass(frozen=True)
class LocalMedia:
    path: Path
    size_bytes: int
    content_type: str = CONTENT_TYPE


class MinioMediaSource:
    """Reads final artifacts out of the worker artifacts bucket."""

    def __init__(self, client: Minio | None = None, bucket: str | None = None) -> None:
        self._client = client
        self._bucket = bucket or settings.worker_artifacts_bucket

    def stat(self, storage_key: str) -> int:
        """Size in bytes, or raise. Used by dry-run to prove the artifact exists cheaply."""
        try:
            info = self._minio().stat_object(self._bucket, storage_key)
        except Exception as exc:  # noqa: BLE001 - minio raises S3Error and urllib3 errors
            raise MediaUnavailableError(
                f"final media not readable at {storage_key} ({type(exc).__name__})"
            ) from exc
        size = int(info.size or 0)
        if size <= 0:
            raise MediaUnavailableError(f"final media at {storage_key} is empty")
        return size

    @contextlib.contextmanager
    def download(self, storage_key: str) -> Iterator[LocalMedia]:
        """Spool the object to a temporary file for the duration of the block."""
        size = self.stat(storage_key)
        handle = tempfile.NamedTemporaryFile(prefix="clipflow-publish-", suffix=".mp4",
                                             delete=False)
        path = Path(handle.name)
        response = None
        try:
            try:
                response = self._minio().get_object(self._bucket, storage_key)
                written = 0
                for block in response.stream(DOWNLOAD_BLOCK_BYTES):
                    handle.write(block)
                    written += len(block)
                handle.flush()
            finally:
                handle.close()
                if response is not None:
                    response.close()
                    response.release_conn()

            if written != size:
                # Publishing a truncated file would produce a broken video that looks like a
                # successful publication.
                raise MediaUnavailableError(
                    f"final media at {storage_key} downloaded {written} of {size} bytes"
                )

            yield LocalMedia(path=path, size_bytes=size)
        finally:
            try:
                os.unlink(path)
            except OSError:
                logger.warning("publish_media_temp_cleanup_failed", extra={"path": str(path)})

    def _minio(self) -> Minio:
        if self._client is not None:
            return self._client
        # Constructed lazily: MinIO is not needed to import this module, and an API process
        # that never publishes should not require the bucket to exist at startup.
        return Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
