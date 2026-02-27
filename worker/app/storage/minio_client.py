import os
import time
from minio import Minio
from minio.error import S3Error
from typing import Optional


class MinioStorage:

    def __init__(self):
        self.endpoint = os.getenv(
            "MINIO_ENDPOINT",
            "minio.voxmind-v2.svc.cluster.local:9000"
        )
        self.access_key = os.getenv("MINIO_ROOT_USER")
        self.secret_key = os.getenv("MINIO_ROOT_PASSWORD")
        self.bucket = os.getenv("MINIO_BUCKET", "voxmind-artifacts")

        if not self.access_key or not self.secret_key:
            raise RuntimeError("MinIO credentials not configured")

        self.client = Minio(
            self.endpoint,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=False
        )

        self._ensure_bucket()

    def _ensure_bucket(self):
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
        except Exception as e:
            raise RuntimeError(f"MinIO bucket check failed: {e}")

    def upload_file(self, local_path: str, object_name: str):
        try:
            self.client.fput_object(
                self.bucket,
                object_name,
                local_path
            )
        except S3Error as e:
            raise RuntimeError(f"MinIO upload failed: {e}")

    def upload_with_retry(
        self,
        local_path: str,
        object_name: str,
        retries: int = 3,
        delay: int = 2
    ):
        last_error: Optional[Exception] = None

        for attempt in range(retries):
            try:
                self.upload_file(local_path, object_name)
                return
            except Exception as e:
                last_error = e
                time.sleep(delay)

        raise RuntimeError(f"Upload failed after retries: {last_error}")