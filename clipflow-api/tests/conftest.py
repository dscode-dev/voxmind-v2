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


# ---------------------------------------------------------------------------
# In-memory database for the lifecycle tests (PR-STATE-01).
#
# The V2 models use PostgreSQL-specific column types (UUID, JSONB). Rather than require a
# live PostgreSQL for a unit test, those two types are taught to compile on SQLite and the
# schema is built in memory. This lives in the test harness only: no production model is
# altered to accommodate it, and the transition rules under test are pure Python either way.
# ---------------------------------------------------------------------------

import uuid as _uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect glue
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect glue
    return "JSON"


@pytest.fixture()
def db():
    """A throwaway database with the full schema, one per test."""
    from app.db.base import Base
    import app.models  # noqa: F401  — registers every mapper

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def no_event_fanout(monkeypatch):
    """Keep the event bus's Redis fan-out out of the tests.

    Persistence is what is under test; fan-out is best-effort by design and already
    swallows its own failures.
    """
    from app.services import event_bus

    published: list = []

    def _capture(payload):
        published.append(payload)

    monkeypatch.setattr(event_bus, "_fan_out", _capture)
    return published


def make_run(db, **overrides):
    """A PipelineJob in QUEUED, the state a producer leaves it in."""
    from app.models.enums import PipelineState
    from app.models.pipeline_job import PipelineJob

    fields = {
        "worker_job_id": str(_uuid.uuid4()),
        "source_url": "https://example.invalid/video",
        "state": PipelineState.QUEUED,
        "clip_mode": "short_serie",
        "video_ratio": "portrait",
        "pipeline_stage": "prepare",
        "retry_count": 0,
        "max_retries": 3,
    }
    fields.update(overrides)
    job = PipelineJob(**fields)
    db.add(job)
    db.flush()
    return job
