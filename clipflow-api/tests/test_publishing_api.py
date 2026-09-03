"""PR-PUBLISH-01 — the admin routes.

Separate from ``test_publishing`` because these test a different thing: not whether the rules
are right, but whether the HTTP surface enforces them and whether anything secret escapes
through a response body.

Two properties are checked on nearly every route: an anonymous caller cannot reach it, and the
serialised response does not contain a refresh token or an upload session URI.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import publishing as publishing_api
from app.api.router import api_router
from app.db.session import get_db
from app.models.audit_log import AuditLog
from app.models.enums import UserRole, UserStatus
from app.models.oauth_state import OAuthState
from app.models.publish_attempt import PublishAttempt
from app.models.user import User
from app.security.auth_middleware import get_current_admin
from app.security.secret_box import SecretBox
from app.services.publish_resolution_service import PublishResolutionService
from app.services.publish_target_service import PublishTargetService
from tests.test_publishing import (  # noqa: F401 - publishing_config is an autouse fixture
    REFRESH_TOKEN,
    TEST_KEY,
    StubPublisher,
    _unknown_result,
    google_ok,
    make_publishable_run,
    make_target,
    oauth_client,
    publishing_config,
    service,
)


def body_text(payload) -> str:
    """The whole response as one string, for "does this leak" assertions."""
    return json.dumps(payload, default=str)


@pytest.fixture()
def admin_user(db):
    user = User(
        phone_number="+5511977776666", full_name="Publisher Admin",
        role=UserRole.ADMIN, status=UserStatus.ACTIVE, credits=100,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture()
def client(db, admin_user, no_event_fanout, monkeypatch):
    """The real router, with the provider socket replaced and auth satisfied."""
    monkeypatch.setattr(
        publishing_api, "_targets",
        lambda: PublishTargetService(oauth=oauth_client(google_ok), box=SecretBox(TEST_KEY)),
    )
    monkeypatch.setattr(publishing_api, "_publishing", service)
    monkeypatch.setattr(publishing_api, "_resolution", PublishResolutionService)

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    with TestClient(app) as test_client:
        yield test_client


# ===========================================================================
# Authorisation
# ===========================================================================


def test_every_publishing_route_requires_an_admin(db, no_event_fanout):
    """There is no public trigger anywhere in the publishing surface."""
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db

    unknown = uuid.uuid4()
    with TestClient(app) as anonymous:
        for method, path in (
            ("get", "/admin/publish-targets"),
            ("post", "/admin/publish-targets/youtube/connect"),
            ("put", f"/admin/publish-targets/{unknown}"),
            ("post", f"/admin/publish-targets/{unknown}/disconnect"),
            ("post", f"/admin/pipeline-jobs/{unknown}/publish"),
            ("get", f"/admin/pipeline-jobs/{unknown}/publish-attempts"),
            ("get", "/admin/publish-attempts/unresolved"),
            ("post", f"/admin/publish-attempts/{unknown}/reconcile"),
            ("post", f"/admin/publish-attempts/{unknown}/resolve"),
            ("post", f"/admin/publish-attempts/{unknown}/mark-not-published"),
            ("post", f"/admin/publish-attempts/{unknown}/cancel"),
        ):
            kwargs = {} if method == "get" else {"json": {}}
            response = getattr(anonymous, method)(path, **kwargs)
            assert response.status_code in (401, 403), f"{method} {path} was reachable"


# ===========================================================================
# Targets
# ===========================================================================


def test_the_target_listing_leaks_no_credential(client, db):
    make_target(db)
    body = client.get("/admin/publish-targets").json()

    assert body["publishing_enabled"] is True
    assert body["targets"][0]["channel_id"] == "UC_channel_123"
    assert REFRESH_TOKEN not in body_text(body)
    assert "refresh_token" not in body_text(body)


def test_connect_returns_an_authorization_url(client, db):
    body = client.post("/admin/publish-targets/youtube/connect").json()

    assert body["authorization_url"].startswith("https://accounts.google.com/")
    assert db.query(OAuthState).count() == 1


def test_the_callback_refuses_an_unknown_state(client):
    response = client.get("/auth/youtube/callback", params={"code": "c", "state": "nope"})
    assert response.status_code == 400


def test_the_callback_reports_a_denied_consent_without_echoing_details(client):
    response = client.get(
        "/auth/youtube/callback", params={"error": "access_denied", "state": "s"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "access_denied"


def test_the_callback_creates_a_disabled_target_and_leaks_nothing(client, db):
    client.post("/admin/publish-targets/youtube/connect")
    state = db.query(OAuthState).one().state

    body = client.get(
        "/auth/youtube/callback", params={"code": "auth-code", "state": state}
    ).json()

    assert body["status"] == "connected"
    assert body["target"]["is_active"] is False, "enabling is a separate deliberate act"
    assert body["target"]["channel_title"] == "Voxmind FC"
    assert REFRESH_TOKEN not in body_text(body)


def test_a_replayed_callback_is_refused(client, db):
    client.post("/admin/publish-targets/youtube/connect")
    state = db.query(OAuthState).one().state
    params = {"code": "auth-code", "state": state}

    assert client.get("/auth/youtube/callback", params=params).status_code == 200
    assert client.get("/auth/youtube/callback", params=params).status_code == 400


def test_a_target_cannot_be_enabled_without_a_credential(client, db):
    target = make_target(db, refresh_token_encrypted=None, is_active=False)
    response = client.put(f"/admin/publish-targets/{target.id}", json={"is_active": True})
    assert response.status_code == 409


def test_defaults_can_be_set_on_a_target(client, db):
    target = make_target(db)
    body = client.put(
        f"/admin/publish-targets/{target.id}",
        json={"default_privacy": "unlisted", "default_category_id": "17"},
    ).json()

    assert body["defaults"]["default_privacy"] == "unlisted"
    assert body["defaults"]["default_category_id"] == "17"


def test_an_invalid_privacy_default_is_rejected_at_the_edge(client, db):
    target = make_target(db)
    response = client.put(
        f"/admin/publish-targets/{target.id}", json={"default_privacy": "everyone"}
    )
    assert response.status_code == 422


def test_disconnect_drops_the_credential(client, db):
    target = make_target(db)
    body = client.post(f"/admin/publish-targets/{target.id}/disconnect").json()

    assert body["connection_status"] == "disconnected"
    assert body["is_active"] is False
    assert target.refresh_token_encrypted is None


# ===========================================================================
# Publishing
# ===========================================================================


def test_publishing_defaults_to_a_dry_run(client, db):
    """The safe operation is the one you get by forgetting a field."""
    job = make_publishable_run(db)
    target = make_target(db)

    body = client.post(
        f"/admin/pipeline-jobs/{job.id}/publish", json={"target_id": str(target.id)}
    ).json()

    assert body["dry_run"] is True
    assert body["status"] == "validated"
    assert db.query(PublishAttempt).count() == 0


def test_the_publish_route_blocks_an_ineligible_run(client, db):
    job = make_publishable_run(
        db,
        metadata_json={
            "publication_eligibility": {"eligible": False,
                                        "blocked_by": ["final_media_qa_fail"]}
        },
    )
    target = make_target(db)

    body = client.post(
        f"/admin/pipeline-jobs/{job.id}/publish",
        json={"target_id": str(target.id), "dry_run": False},
    ).json()

    assert "publication_not_eligible" in body["blocked_by"]
    assert db.query(PublishAttempt).count() == 0


def test_a_real_publish_returns_the_external_url(client, db):
    job = make_publishable_run(db)
    target = make_target(db)

    body = client.post(
        f"/admin/pipeline-jobs/{job.id}/publish",
        json={"target_id": str(target.id), "dry_run": False, "privacy": "private"},
    ).json()

    assert body["publication_status"] == "published"
    assert body["job_state"] == "published"
    assert body["items"][0]["external_url"].startswith("https://www.youtube.com/watch?v=")


def test_the_publish_route_records_an_audit_entry(client, db):
    job = make_publishable_run(db)
    target = make_target(db)
    client.post(
        f"/admin/pipeline-jobs/{job.id}/publish",
        json={"target_id": str(target.id), "dry_run": False},
    )

    entry = db.query(AuditLog).filter(AuditLog.action == "admin.publish.requested").one()
    assert entry.target_id == str(job.id)
    assert entry.metadata_json["dry_run"] is False


def test_an_unknown_job_is_a_404(client, db):
    target = make_target(db)
    response = client.post(
        f"/admin/pipeline-jobs/{uuid.uuid4()}/publish", json={"target_id": str(target.id)}
    )
    assert response.status_code == 404


def test_an_unknown_target_is_a_404(client, db):
    job = make_publishable_run(db)
    response = client.post(
        f"/admin/pipeline-jobs/{job.id}/publish", json={"target_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


def test_a_non_youtube_target_is_refused(client, db):
    from app.models.enums import PublishPlatform

    job = make_publishable_run(db)
    target = make_target(db, platform=PublishPlatform.TIKTOK, channel_id=None)

    response = client.post(
        f"/admin/pipeline-jobs/{job.id}/publish", json={"target_id": str(target.id)}
    )
    assert response.status_code == 400
    assert "not implemented" in response.json()["detail"]


# ===========================================================================
# History and resolution
# ===========================================================================


def test_the_history_route_shows_attempts_without_secrets(client, db):
    job = make_publishable_run(db)
    target = make_target(db)
    client.post(
        f"/admin/pipeline-jobs/{job.id}/publish",
        json={"target_id": str(target.id), "dry_run": False},
    )

    body = client.get(f"/admin/pipeline-jobs/{job.id}/publish-attempts").json()

    assert body["publication_status"] == "published"
    assert len(body["attempts"]) == 1
    assert body["attempts"][0]["external_id"]
    assert body["attempts"][0]["metadata_snapshot"]["title"]
    assert REFRESH_TOKEN not in body_text(body)
    assert "upload_session_uri" not in body_text(body)


def test_the_unresolved_queue_lists_only_what_needs_a_human(client, db):
    job = make_publishable_run(db)
    target = make_target(db)
    service(publisher=StubPublisher([_unknown_result()])).publish(
        db, job=job, target=target, dry_run=False
    )

    body = client.get("/admin/publish-attempts/unresolved").json()

    assert body["count"] == 1
    assert body["attempts"][0]["status"] == "unknown"
    assert body["attempts"][0]["retryability"] == "requires_manual_resolution"
    # The session URI is a bearer credential that looks like an ordinary URL.
    assert "upload.googleapis.com" not in body_text(body)


def test_resolving_a_settled_attempt_is_refused(client, db):
    job = make_publishable_run(db)
    target = make_target(db)
    service().publish(db, job=job, target=target, dry_run=False)
    attempt = db.query(PublishAttempt).one()

    response = client.post(
        f"/admin/publish-attempts/{attempt.id}/resolve", json={"external_id": "vid_x"}
    )
    assert response.status_code == 409


def test_marking_not_published_settles_the_attempt(client, db):
    job = make_publishable_run(db)
    target = make_target(db)
    service(publisher=StubPublisher([_unknown_result()])).publish(
        db, job=job, target=target, dry_run=False
    )
    attempt = db.query(PublishAttempt).one()

    body = client.post(
        f"/admin/publish-attempts/{attempt.id}/mark-not-published",
        json={"note": "checked the channel, nothing there"},
    ).json()

    assert body["status"] == "failed_final"
    assert body["external_id"] is None

# ===========================================================================
# The asynchronous contract (PR-PUBLISH-QUEUE-01)
# ===========================================================================


@pytest.fixture()
def async_client(db, admin_user, no_event_fanout, monkeypatch):
    """A client wired to the real (non-draining) service, so nothing executes inline."""
    import fakeredis

    from app.publishing.publish_queue import PublishQueue
    from app.services.publishing_service import PublishingService
    from tests.test_publishing import StubArtifacts, StubMediaSource

    queue = PublishQueue(
        fakeredis.FakeRedis(decode_responses=True), "test_publish_jobs",
        worker_id="test-publisher",
    )
    publisher = StubPublisher()

    monkeypatch.setattr(
        publishing_api, "_publishing",
        lambda: PublishingService(
            publisher=publisher, artifacts=StubArtifacts(),
            media_source=StubMediaSource(), queue=queue,
        ),
    )
    monkeypatch.setattr(
        publishing_api, "_targets",
        lambda: PublishTargetService(oauth=oauth_client(google_ok), box=SecretBox(TEST_KEY)),
    )

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    with TestClient(app) as client:
        client.queue = queue
        client.publisher = publisher
        yield client


def test_a_real_publish_is_accepted_not_completed(async_client, db):
    """The upload no longer happens inside the request."""
    job = make_publishable_run(db)
    target = make_target(db)

    response = async_client.post(
        f"/admin/pipeline-jobs/{job.id}/publish",
        json={"target_id": str(target.id), "dry_run": False},
    )

    assert response.status_code == 202, "accepted, not completed"
    body = response.json()
    assert body["status"] == "accepted"
    assert body["items"][0]["status"] == "queued"
    assert body["job_state"] == "publishing"
    assert async_client.publisher.calls == [], "the request must not wait on an upload"
    assert async_client.queue.depths()["ready"] == 1
    assert db.query(PublishAttempt).count() == 1


def test_a_dry_run_still_answers_200(async_client, db):
    job = make_publishable_run(db)
    target = make_target(db)

    response = async_client.post(
        f"/admin/pipeline-jobs/{job.id}/publish", json={"target_id": str(target.id)}
    )

    assert response.status_code == 200, "a validation that already finished is not 202"
    assert response.json()["status"] == "validated"
    assert async_client.queue.depths()["ready"] == 0


def test_a_blocked_publication_answers_200_not_202(async_client, db):
    """Nothing was accepted, so nothing is pending."""
    job = make_publishable_run(
        db,
        metadata_json={"publication_eligibility": {"eligible": False,
                                                   "blocked_by": ["final_media_qa_fail"]}},
    )
    target = make_target(db)

    response = async_client.post(
        f"/admin/pipeline-jobs/{job.id}/publish",
        json={"target_id": str(target.id), "dry_run": False},
    )

    assert response.status_code == 200
    assert "publication_not_eligible" in response.json()["blocked_by"]
    assert async_client.queue.depths()["ready"] == 0


def test_two_requests_queue_one_command(async_client, db):
    job = make_publishable_run(db)
    target = make_target(db)
    body = {"target_id": str(target.id), "dry_run": False}

    async_client.post(f"/admin/pipeline-jobs/{job.id}/publish", json=body)
    async_client.post(f"/admin/pipeline-jobs/{job.id}/publish", json=body)

    assert db.query(PublishAttempt).count() == 1
    assert async_client.queue.depths()["ready"] == 1


def test_the_runtime_endpoint_reports_the_queue_and_workers(client, db, monkeypatch):
    import fakeredis

    from app.publishing.identity import PublisherHeartbeat
    from app.publishing.publish_queue import PublishQueue, command_payload

    fake = fakeredis.FakeRedis(decode_responses=True)
    queue = PublishQueue(fake, "test_publish_jobs", worker_id="publisher-1")
    queue.enqueue(command_payload(publish_attempt_id="a", pipeline_job_id="j",
                                  target_id="t", media_identity="m"))
    PublisherHeartbeat("publisher-1", fake).beat(state="idle")

    monkeypatch.setattr(
        "app.services.publish_runtime.PublishQueue", lambda *a, **k: queue, raising=False
    )
    monkeypatch.setattr(
        "app.publishing.identity._default_redis", lambda: fake, raising=False
    )

    body = client.get("/admin/publishing/runtime").json()

    assert body["ready"] == 1
    assert body["workers_alive"] == 1
    assert body["workers"][0]["worker_id"] == "publisher-1"
    assert "pending_enqueue" in body and "unresolved" in body


def test_the_runtime_endpoint_requires_an_admin(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        assert anonymous.get("/admin/publishing/runtime").status_code in (401, 403)
