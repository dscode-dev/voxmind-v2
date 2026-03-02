from minio import Minio
from minio.error import S3Error
import os
from pathlib import Path


class MinioStorage:

    def __init__(self):

        endpoint = os.getenv("MINIO_ENDPOINT")
        access_key = os.getenv("MINIO_ROOT_USER")
        secret_key = os.getenv("MINIO_ROOT_PASSWORD")
        bucket = os.getenv("MINIO_BUCKET", "voxmind")

        if not endpoint:
            raise RuntimeError("MINIO_ENDPOINT not configured")

        self.bucket = bucket

        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=False
        )

        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def upload(self, local_path: str, object_name: str):
        self.client.fput_object(
            self.bucket,
            object_name,
            local_path
        )

    def upload_with_retry(self, local_path: str, object_name: str):
        self.upload(local_path, object_name)