"""Test bootstrap for the worker suite.

`MinioStorage` reads its endpoint straight from the environment and connects in its
constructor, and several modules build one at import/construction time. Setting a value here
lets the suite be collected without a live MinIO; tests that would actually talk to storage
are unaffected because they do not reach that far.
"""

import os

_TEST_ENV = {
    "MINIO_ENDPOINT": "minio:9000",
    "MINIO_ROOT_USER": "test-minio-user",
    "MINIO_ROOT_PASSWORD": "test-minio-password",
    "MINIO_BUCKET": "voxmind-test",
    "LOG_JSON": "false",
}

for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)
