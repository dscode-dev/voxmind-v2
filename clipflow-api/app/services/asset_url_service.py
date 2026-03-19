from __future__ import annotations

from datetime import timedelta

from minio import Minio

from app.core.settings import settings


class AssetUrlService:

    def __init__(self) -> None:
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self.bucket = settings.worker_artifacts_bucket
        self.expiry = timedelta(seconds=settings.signed_asset_url_expiry_sec)

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
            return None
