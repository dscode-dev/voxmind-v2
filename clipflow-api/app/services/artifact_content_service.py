from __future__ import annotations

import json
from typing import Any

from minio import Minio

from app.core.settings import settings


class ArtifactContentService:

    def __init__(self) -> None:
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self.bucket = settings.worker_artifacts_bucket

    def load_json(self, storage_key: str | None) -> dict[str, Any] | list[Any] | None:
        if not storage_key:
            return None

        response = None
        try:
            response = self.client.get_object(self.bucket, storage_key)
            return json.loads(response.read().decode("utf-8"))
        except Exception:
            return None
        finally:
            if response is not None:
                response.close()
                response.release_conn()
