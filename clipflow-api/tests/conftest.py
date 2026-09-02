"""Test bootstrap.

`app.core.settings` builds a module-level `Settings()` at import time, and several fields are
required with no default (JWT_SECRET, INTERNAL_API_TOKEN, MINIO_*). These values are set
before any test module is imported so importing the app never depends on a developer's local
environment. Individual tests that exercise validation build their own `Settings(...)`.
"""

import os

_TEST_ENV = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+psycopg://clipflow:clipflow@localhost:5432/clipflow_test",
    "JWT_SECRET": "test-jwt-secret-0123456789abcdef",
    "INTERNAL_API_TOKEN": "test-internal-token-0123456789",
    "MINIO_ENDPOINT": "minio:9000",
    "MINIO_ACCESS_KEY": "test-minio-access-key",
    "MINIO_SECRET_KEY": "test-minio-secret-key",
}

for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)
