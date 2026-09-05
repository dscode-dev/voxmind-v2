"""The two read models the operations console could not assemble for itself.

Both exist because of the same failure mode: a question an operator asks every day had no
endpoint behind it, so the Studio either inferred the answer or showed nothing. What is
asserted here is therefore mostly about *what the answer says*, not about plumbing —
that a configured deployment is not reported as a working one, that a production run is
identified by what it is about rather than by its id, and that neither endpoint leaks a
secret on its way out.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import api_router
from app.core.settings import settings
from app.db.session import get_db
from app.models.ai_execution import AIExecution
from app.models.content_topic import ContentTopic
from app.models.enums import (
    AIExecutionStatus,
    PipelineState,
    PublishAttemptStatus,
    PublishPlatform,
    PublishTargetConnectionStatus,
    UserRole,
    UserStatus,
    VideoCandidateStatus,
)
from app.models.publish_attempt import PublishAttempt
from app.models.publish_target import PublishTarget
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin
from tests.conftest import make_run

FAKE_OPENAI_KEY = "sk-proj-DUMMY-should-never-appear-anywhere"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def admin_user(db):
    user = User(
        phone_number="+5511999999999",
        full_name="Admin",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
        credits=100,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture()
def client(db, admin_user, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    with TestClient(app) as test_client:
        yield test_client


def execution(db, job=None, *, status=AIExecutionStatus.SUCCEEDED, created_at=None, **kw):
    row = AIExecution(
        pipeline_job_id=job.id if job else None,
        provider=kw.pop("provider", "openai"),
        model=kw.pop("model", "gpt-4o-mini"),
        purpose=kw.pop("purpose", "publication_metadata"),
        status=status,
        latency_ms=kw.pop("latency_ms", 820),
        error_message=kw.pop("error_message", None),
        payload_json=kw.pop("payload_json", {"video_index": 1}),
    )
    db.add(row)
    db.flush()
    if created_at is not None:
        row.created_at = created_at
        db.flush()
    return row


# ===========================================================================
# AI status
# ===========================================================================


def test_a_configured_deployment_is_not_reported_as_a_working_one(client, monkeypatch):
    """The distinction the endpoint exists for.

    A key being present says this deployment *could* call a provider. Only a recorded call
    says it does. Collapsing the two into one green dot is exactly the lie an operator opens
    this panel to avoid.
    """
    monkeypatch.setattr(settings, "openai_api_key", FAKE_OPENAI_KEY, raising=False)

    body = client.get("/admin/ai/status").json()

    assert body["configured"] is True
    assert body["provider"] == "openai"
    assert body["last_execution"] is None
    assert body["last_success_at"] is None


def test_an_unconfigured_deployment_names_no_provider_and_no_model(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None, raising=False)

    body = client.get("/admin/ai/status").json()

    assert body["configured"] is False
    assert body["provider"] is None
    assert body["model"] is None


def test_the_last_execution_is_the_evidence_the_panel_shows(client, db, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", FAKE_OPENAI_KEY, raising=False)
    job = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    execution(db, job, created_at=NOW - timedelta(hours=2))
    execution(
        db,
        job,
        status=AIExecutionStatus.FAILED,
        error_message="ConnectTimeout",
        created_at=NOW - timedelta(minutes=5),
    )
    db.commit()

    body = client.get("/admin/ai/status").json()

    # Newest first: the last thing that happened was a failure...
    assert body["last_execution"]["status"] == "failed"
    assert body["last_execution"]["error"] == "ConnectTimeout"
    assert body["last_execution"]["model"] == "gpt-4o-mini"
    # ...but it has worked before, and an operator needs both facts to read the situation.
    assert body["last_success_at"] is not None


def test_the_api_key_is_absent_from_the_response_not_masked(client, db, monkeypatch):
    """Not `sk-…abcd`, not a length, not a boolean derived per character. Absent.

    A masked key is still a key shape on a screen someone will screenshot into a ticket.
    """
    monkeypatch.setattr(settings, "openai_api_key", FAKE_OPENAI_KEY, raising=False)
    execution(db, make_run(db, state=PipelineState.READY_TO_PUBLISH))
    db.commit()

    raw = client.get("/admin/ai/status").text

    assert FAKE_OPENAI_KEY not in raw
    assert "sk-" not in raw
    assert "api_key" not in raw


def test_recent_counts_are_grouped_by_outcome(client, db, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", FAKE_OPENAI_KEY, raising=False)
    job = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    execution(db, job)
    execution(db, job)
    execution(db, job, status=AIExecutionStatus.FAILED, error_message="http_401")
    db.commit()

    counts = client.get("/admin/ai/status").json()["executions_last_7d"]

    assert counts == {"succeeded": 2, "failed": 1}


def test_ai_status_is_admin_only(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        assert anonymous.get("/admin/ai/status").status_code in (401, 403)


# ===========================================================================
# Production runs
# ===========================================================================


_topics = itertools.count(1)


def _target(db):
    """A channel row, only so an attempt has something to point at.

    No credential: nothing here connects to a provider, and a target with no refresh token is
    exactly what an unconnected one looks like.
    """
    target = PublishTarget(
        platform=PublishPlatform.YOUTUBE,
        name="Voxmind FC",
        is_active=True,
        connection_status=PublishTargetConnectionStatus.CONNECTED,
        config_json={},
    )
    db.add(target)
    db.flush()
    return target


def make_production(db, *, title="Milan 3 x 1 Inter | Melhores Momentos", **overrides):
    # Topic names are unique; each production gets its own so a test can build several.
    topic = ContentTopic(
        name=f"Serie A {next(_topics)}", is_active=True, keywords_json=["serie a"]
    )
    db.add(topic)
    db.flush()
    candidate = VideoCandidate(
        topic_id=topic.id,
        url="https://youtu.be/abc",
        title=title,
        channel="Serie A",
        thumbnail_url="https://i.ytimg.com/vi/abc/hq.jpg",
        duration_sec=612,
        status=VideoCandidateStatus.CONSUMED,
    )
    db.add(candidate)
    db.flush()
    return make_run(
        db, topic_id=topic.id, candidate_id=candidate.id, **overrides
    )


def test_a_run_is_identified_by_what_it_is_about(client, db):
    """The rule this endpoint was added to make possible.

    A console that can only say `PipelineJob 55e7d…` is not showing the product; it is
    showing the schema. The id stays in the payload — it is what gets pasted into a ticket —
    but the run also carries the thing a person recognises.
    """
    make_production(db, state=PipelineState.RENDERING)
    db.commit()

    item = client.get("/admin/pipeline-jobs").json()["items"][0]

    assert item["title"] == "Milan 3 x 1 Inter | Melhores Momentos"
    assert item["topic"]["name"].startswith("Serie A")
    assert item["candidate"]["thumbnail_url"].startswith("https://")
    assert item["candidate"]["channel"] == "Serie A"
    assert item["id"]


def test_a_run_with_no_candidate_falls_back_to_its_source_rather_than_to_nothing(client, db):
    """Studio-originated runs have no candidate. A blank title would be worse than the URL."""
    make_run(db, state=PipelineState.QUEUED, source_url="https://youtu.be/manual")
    db.commit()

    item = client.get("/admin/pipeline-jobs").json()["items"][0]

    assert item["title"] == "https://youtu.be/manual"
    assert item["candidate"] is None


def test_the_active_filter_excludes_finished_and_waiting_runs(client, db):
    make_production(db, state=PipelineState.RENDERING)
    make_production(db, state=PipelineState.PUBLISHED)
    make_production(db, state=PipelineState.REVIEW_REQUIRED)
    db.commit()

    body = client.get("/admin/pipeline-jobs", params={"active": True}).json()

    assert body["total"] == 1
    assert body["items"][0]["state"] == "rendering"


def test_one_state_can_be_asked_for_by_name(client, db):
    make_production(db, state=PipelineState.RENDERING)
    make_production(db, state=PipelineState.PUBLISHED)
    db.commit()

    body = client.get("/admin/pipeline-jobs", params={"state": "published"}).json()

    assert [item["state"] for item in body["items"]] == ["published"]
    assert body["total"] == 1


def test_the_page_is_bounded_and_says_how_many_there_are(client, db):
    for _ in range(4):
        make_production(db, state=PipelineState.QUEUED)
    db.commit()

    body = client.get("/admin/pipeline-jobs", params={"limit": 2}).json()

    assert len(body["items"]) == 2
    assert body["total"] == 4
    assert body["limit"] == 2

    assert client.get("/admin/pipeline-jobs", params={"limit": 500}).status_code == 422


def test_publication_counts_come_from_the_run_not_from_its_attempts_alone(client, db):
    """A four-clip run with one upload is 1/4, and the console must be able to say so."""
    job = make_production(db, state=PipelineState.PUBLISHING)
    job.metadata_json = {
        "publication_status": "partial",
        "publication_summary": {"required": 4, "succeeded": 1, "outstanding": 3},
    }
    db.add(
        PublishAttempt(
            pipeline_job_id=job.id,
            target_id=_target(db).id,
            media_identity="final_clips/final_clip_01.mp4",
            media_storage_key="jobs/x/final_clips/final_clip_01.mp4",
            status=PublishAttemptStatus.SUCCEEDED,
            attempt_no=1,
            max_attempts=3,
            initiator="automatic",
            external_id="vid_1",
        )
    )
    db.commit()

    item = client.get("/admin/pipeline-jobs").json()["items"][0]

    assert item["publication"]["required"] == 4
    assert item["publication"]["succeeded"] == 1
    assert item["publication"]["outstanding"] == 3
    assert item["publication"]["attempts"] == {"succeeded": 1}


def test_the_detail_adds_provenance_without_dumping_the_whole_metadata_blob(client, db):
    """`metadata_json` is a working area. Serialising it wholesale would publish whatever a
    future stage decides to put there — including, one day, something that should not leave
    the server."""
    job = make_production(db, state=PipelineState.PUBLISHED)
    job.metadata_json = {
        "provenance": {"admitted_by": "selection"},
        "snapshot": {"clip_mode": "short_serie"},
        "editorial_metadata": {"1": {"title": "Gol de Leao"}},
        "internal_scratch": "should not be echoed",
    }
    db.commit()

    body = client.get(f"/admin/pipeline-jobs/{job.id}").json()

    assert body["provenance"] == {"admitted_by": "selection"}
    assert body["frozen_inputs"] == {"clip_mode": "short_serie"}
    assert body["editorial_metadata"]["1"]["title"] == "Gol de Leao"
    assert "internal_scratch" not in str(body)


def test_an_unknown_run_is_a_404_not_an_empty_object(client, db):
    import uuid

    assert client.get(f"/admin/pipeline-jobs/{uuid.uuid4()}").status_code == 404


def test_pipeline_jobs_are_admin_only(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        assert anonymous.get("/admin/pipeline-jobs").status_code in (401, 403)
