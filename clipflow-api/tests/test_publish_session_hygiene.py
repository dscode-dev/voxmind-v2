"""The spent resumable session must not outlive the publication it belonged to.

Found by the first real upload to a live channel, not by a unit test — which is why this file
exists.

A resumable session URI is a bearer credential: anyone holding it can write to that upload.
The settle path has always intended to drop it on success ("the session is spent and it is a
credential; there is no reason to keep it"), and on a real 54 MB upload it survived anyway.

The reason is an ORM ordering trap rather than a missing line. The progress recorder writes
that column from its *own* short-lived session while the upload runs, so by the time the
settle path assigns `None` the row on disk holds a session URI the settling session has never
seen — it still believes the value is the NULL it loaded. Assigning None is then not a change,
no UPDATE is emitted, and the credential stays.

**Why these tests assert on the SQL rather than on the stored value.** The test harness runs
SQLite through a StaticPool, so every "session" shares one connection and one transaction —
the cross-session staleness that causes the bug cannot be staged there, and a test asserting
the column is NULL passes with or without the fix. Asserting that the settle path *emits* an
explicit UPDATE pins the mechanism instead, and holds on any backend.

The behaviour itself was verified against real PostgreSQL, where the two sessions are genuinely
independent: without the explicit UPDATE the session URI survives the publication, with it the
column is cleared.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import event, select, update
from sqlalchemy.orm import sessionmaker

from app.models.enums import PublishAttemptStatus, PipelineState
from app.models.publish_attempt import PublishAttempt
from app.publishing.contracts import PublishOutcome, PublishResult
from app.services.publishing_service import (
    MediaItem,
    PublishingService,
    idempotency_key,
)
from tests.conftest import make_run
from tests.test_publishing import (  # noqa: F401 - publishing_config is autouse
    StubPublisher,
    make_target,
    publishing_config,
)


def _attempt(db, job, target) -> PublishAttempt:
    identity = "final_clips/final_clip_01.mp4"
    attempt = PublishAttempt(
        pipeline_job_id=job.id,
        target_id=target.id,
        idempotency_key=idempotency_key(job.id, target.id, identity),
        media_identity=identity,
        media_storage_key=f"jobs/{job.worker_job_id}/{identity}",
        media_bytes=54_136_741,
        status=PublishAttemptStatus.IN_PROGRESS,
        attempt_no=1,
        max_attempts=3,
        initiator="manual",
        started_at=datetime.now(timezone.utc),
        publisher_worker_id="worker-1",
        payload_json={
            "metadata": {"title": "t", "description": "", "tags": [], "privacy": "private"},
            "video_index": 1,
        },
    )
    db.add(attempt)
    db.flush()
    return attempt


def _write_session_from_another_session(db, attempt_id, value: str) -> None:
    """What the progress recorder does: its own session, its own commit, mid-upload."""
    factory = sessionmaker(bind=db.get_bind(), future=True, expire_on_commit=False)
    other = factory()
    try:
        other.execute(
            update(PublishAttempt)
            .where(PublishAttempt.id == attempt_id)
            .values(upload_session_uri_encrypted=value, bytes_uploaded=50_331_648)
        )
        other.commit()
    finally:
        other.close()


def _read_session_from_another_session(db, attempt_id) -> str | None:
    """What is really stored, independent of any identity map."""
    factory = sessionmaker(bind=db.get_bind(), future=True, expire_on_commit=False)
    other = factory()
    try:
        return other.execute(
            select(PublishAttempt.upload_session_uri_encrypted).where(
                PublishAttempt.id == attempt_id
            )
        ).scalar_one()
    finally:
        other.close()


def test_a_successful_publication_clears_the_session_it_never_loaded(db, no_event_fanout):
    """The regression: settle must null the column even when this session loaded it as NULL.

    Without the explicit UPDATE the assignment is a no-op for the unit of work and the spent
    credential survives — which is what a real upload produced.
    """
    target = make_target(db)
    job = make_run(db, state=PipelineState.PUBLISHING)
    attempt = _attempt(db, job, target)
    db.commit()

    # The upload runs and the progress recorder persists the session from its own session.
    _write_session_from_another_session(db, attempt.id, "gAAAAA-encrypted-session-uri")

    # This session still believes the column is NULL, exactly as the publisher's does.
    assert attempt.upload_session_uri_encrypted is None

    statements = _capture_sql(db)
    service = PublishingService(publisher=StubPublisher())
    service._record(
        db,
        job=job,
        target=target,
        attempt=attempt,
        result=PublishResult(
            provider="youtube",
            outcome=PublishOutcome.SUCCEEDED,
            external_id="eo0AiGWxKzw",
            external_url="https://www.youtube.com/watch?v=eo0AiGWxKzw",
            published_at=datetime.now(timezone.utc),
            privacy="private",
            bytes_uploaded=54_136_741,
        ),
        duration_ms=1200,
        item=MediaItem(
            identity=attempt.media_identity,
            storage_key=attempt.media_storage_key,
            video_index=1,
            video={},
        ),
        worker_id="worker-1",
    )
    db.commit()

    # An UPDATE that nulls the column must actually have been issued. Relying on the ORM
    # assignment alone emits nothing, because this session's committed value is already NULL.
    cleared = [
        text
        for text, params in statements
        if "UPDATE publish_attempts" in text
        and "upload_session_uri_encrypted" in text
        and _nulls_the_session(text, params)
    ]
    assert cleared, "settle did not issue an UPDATE clearing the spent session URI"

    assert attempt.status == PublishAttemptStatus.SUCCEEDED
    assert attempt.external_id == "eo0AiGWxKzw"


def _capture_sql(db) -> list[tuple[str, object]]:
    """Every statement this session emits from now on, so the fix can be asserted directly."""
    seen: list[tuple[str, object]] = []

    @event.listens_for(db.get_bind(), "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        seen.append((statement, parameters))

    return seen


def _nulls_the_session(statement: str, parameters: object) -> bool:
    """Whether this UPDATE sets the session column, and only it, to NULL.

    The parameter style differs by driver — a dict on psycopg, a tuple on SQLite — so the
    check is on the SET clause plus the presence of a NULL bind rather than on a named key.
    """
    normalized = " ".join(statement.split())
    if "SET upload_session_uri_encrypted=" not in normalized:
        return False
    if isinstance(parameters, dict):
        values = list(parameters.values())
    elif isinstance(parameters, (list, tuple)):
        values = list(parameters)
    else:
        return False
    return any(value is None for value in values)


def test_an_unknown_outcome_keeps_its_session(db, no_event_fanout):
    """The other half of the rule, and the reason the clear is not unconditional.

    An UNKNOWN publication may already exist at the provider. Its session is the evidence a
    later probe uses to find out, so dropping it would destroy the only cheap way to settle
    the one state that must never be retried blindly.
    """
    target = make_target(db)
    job = make_run(db, state=PipelineState.PUBLISHING)
    attempt = _attempt(db, job, target)
    db.commit()

    service = PublishingService(publisher=StubPublisher())
    service._record(
        db,
        job=job,
        target=target,
        attempt=attempt,
        result=PublishResult(
            provider="youtube",
            outcome=PublishOutcome.UNKNOWN,
            error_code="deadline_exceeded",
            error_message="the response to the final chunk was lost",
            bytes_uploaded=54_136_741,
            session_uri="https://upload.youtube.example/resumable/abc",
        ),
        duration_ms=1200,
        item=MediaItem(
            identity=attempt.media_identity,
            storage_key=attempt.media_storage_key,
            video_index=1,
            video={},
        ),
        worker_id="worker-1",
    )
    db.commit()

    assert attempt.status == PublishAttemptStatus.UNKNOWN
    assert attempt.upload_session_uri_encrypted is not None
