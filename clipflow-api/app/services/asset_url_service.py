from __future__ import annotations

import logging
from datetime import timedelta

from minio import Minio

from app.core.settings import settings

logger = logging.getLogger(__name__)


class AssetUrlService:
    """Builds browser-reachable presigned URLs for stored artifacts.

    Presigned URLs are signed with SigV4, which covers the Host header — so the URL must be
    signed against the endpoint the browser will actually call. Signing against the internal
    Compose hostname (``minio:9000``) and rewriting the host afterwards would invalidate the
    signature, so the client below is built from the *public* endpoint instead.
    """

    def __init__(self) -> None:
        self.endpoint = settings.resolved_minio_public_endpoint
        self.client = Minio(
            self.endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.resolved_minio_public_secure,
        )
        self.bucket = settings.worker_artifacts_bucket
        self.expiry = timedelta(seconds=settings.signed_asset_url_expiry_sec)

        if not settings.minio_public_endpoint:
            logger.warning(
                "MINIO_PUBLIC_ENDPOINT is not set; presigning against the internal endpoint "
                "%s. Signed asset URLs will not be reachable from a browser.",
                self.endpoint,
            )

    def build_signed_url(self, storage_key: str | None) -> str | None:
        if not storage_key:
            return None

        try:
            return self.client.presigned_get_object(
                self.bucket,
                storage_key,
                expires=self.expiry,
            )
        except Exception:
            logger.warning(
                "Failed to presign %s on %s", storage_key, self.endpoint, exc_info=True
            )
            return None
