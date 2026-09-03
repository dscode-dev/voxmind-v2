"""The admin discovery API (PR-DISCOVERY-01).

Exercised through FastAPI with the database and the provider substituted, so the routing,
auth, filtering and pagination are real while nothing reaches YouTube.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import discovery as discovery_api
from app.api.router import api_router
from app.db.session import get_db
from app.discovery.contracts import (
    QUOTA_EXCEEDED,
    DiscoveredVideo,
    DiscoveryFetch,
    ProviderError,
    ProviderUnavailable,
)
from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.enums import DiscoverySourceKind, UserRole, UserStatus, VideoCandidateStatus
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin
from app.services.discovery_service import DiscoveryService

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class StubProvider:
    name = "youtube"

    def __init__(self, videos=None, error=None):
        self.videos = videos or []
        self.error = error

    def is_configured(self):
        return self.error is None or not isinstance(self.error, ProviderUnavailable)

    def discover(self, request):
        if self.error:
            raise self.error
        return DiscoveryFetch(videos=list(self.videos), api_calls=2)


def video(video_id: str, **overrides) -> DiscoveredVideo:
    fields = {
        "provider": "youtube",
        "external_id": video_id,
        "canonical_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "Entrevista completa",
        "channel_name": "Canal Esportivo",
        "published_at": NOW,
        "duration_sec": 750,
        "view_count": 100,
        "description": "Descricao",
    }
    fields.update(overrides)
    return DiscoveredVideo(**fields)


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


@pytest.fixture()
def stub_service(monkeypatch):
    """Install a provider stub in place of the real YouTube client."""
    holder = {"provider": StubProvider()}

    def factory():
        return DiscoveryService(youtube_provider=holder["provider"])

    monkeypatch.setattr(discovery_api, "_service", factory)
    return holder


@pytest.fixture()
def topic(db):
    topic = ContentTopic(name="Futebol brasileiro", keywords_json=["futebol entrevista"])
    db.add(topic)
    db.flush()
    return topic


@pytest.fixture()
def source(db, topic):
    source = DiscoverySource(
        topic_id=topic.id,
        kind=DiscoverySourceKind.YOUTUBE_SEARCH,
        name="YouTube search",
        is_active=True,
        config_json={"queries": ["futebol entrevista"]},
    )
    db.add(source)
    db.flush()
    return source


# ==========================================================================
# Topics and sources
# ==========================================================================


def test_a_topic_can_be_created_with_its_queries(client, db):
    response = client.post("/admin/content-topics", json={
        "name": "Futebol quente",
        "keywords": ["futebol entrevista", "futebol polemica", "futebol coletiva"],
        "language": "pt-BR",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["keywords"] == ["futebol entrevista", "futebol polemica", "futebol coletiva"]
    assert body["metadata"]["language"] == "pt-BR"


def test_a_source_carries_its_own_query_configuration(client, topic):
    response = client.post("/admin/discovery-sources", json={
        "topic_id": str(topic.id),
        "kind": "youtube_search",
        "name": "Buscas de futebol",
        "config": {"queries": ["coletiva pos jogo"], "max_results": 10},
    })

    assert response.status_code == 200
    assert response.json()["config"]["queries"] == ["coletiva pos jogo"]


def test_a_source_for_an_unknown_topic_is_rejected(client):
    response = client.post("/admin/discovery-sources", json={
        "topic_id": str(uuid.uuid4()), "kind": "youtube_search", "config": {},
    })
    assert response.status_code == 404


# ==========================================================================
# Running discovery
# ==========================================================================


def test_running_discovery_persists_candidates(client, db, topic, source, stub_service):
    stub_service["provider"] = StubProvider([video("aaaaaaaaaaa"), video("bbbbbbbbbbb")])

    response = client.post("/admin/discovery/run", json={"topic_id": str(topic.id)})

    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["new_candidates"] == 2
    assert db.query(VideoCandidate).count() == 2


def test_running_discovery_twice_creates_nothing_new(client, db, topic, source, stub_service):
    stub_service["provider"] = StubProvider([video("aaaaaaaaaaa")])

    first = client.post("/admin/discovery/run", json={"topic_id": str(topic.id)}).json()
    second = client.post("/admin/discovery/run", json={"topic_id": str(topic.id)}).json()

    assert first["totals"]["new_candidates"] == 1
    assert second["totals"]["new_candidates"] == 0
    assert second["totals"]["existing_candidates"] == 1
    assert db.query(VideoCandidate).count() == 1


def test_a_single_source_can_be_run(client, db, topic, source, stub_service):
    stub_service["provider"] = StubProvider([video("aaaaaaaaaaa")])

    response = client.post("/admin/discovery/run", json={
        "topic_id": str(topic.id), "source_id": str(source.id),
    })

    assert response.status_code == 200
    assert len(response.json()["runs"]) == 1


def test_an_unconfigured_provider_answers_explicitly(client, db, topic, source, stub_service):
    """Configuration required is a reportable state, not a crash and not fake data."""
    stub_service["provider"] = StubProvider(error=ProviderUnavailable("YOUTUBE_API_KEY is not set"))

    response = client.post("/admin/discovery/run", json={"topic_id": str(topic.id)})

    assert response.status_code == 200
    run = response.json()["runs"][0]
    assert run["status"] == "unavailable"
    assert run["errors"][0]["error_type"] == "not_configured"
    assert db.query(VideoCandidate).count() == 0


def test_a_quota_error_is_visible_in_the_response(client, topic, source, stub_service):
    stub_service["provider"] = StubProvider(error=ProviderError(QUOTA_EXCEEDED, "spent"))

    run = client.post("/admin/discovery/run", json={"topic_id": str(topic.id)}).json()["runs"][0]

    assert run["status"] == "failed"
    assert run["errors"][0]["error_type"] == QUOTA_EXCEEDED
    assert run["errors"][0]["retryable"] is False


def test_running_an_unknown_topic_is_a_404(client, stub_service):
    response = client.post("/admin/discovery/run", json={"topic_id": str(uuid.uuid4())})
    assert response.status_code == 404


def test_running_a_source_from_another_topic_is_a_404(client, db, topic, source, stub_service):
    other = ContentTopic(name="Outro tema")
    db.add(other)
    db.flush()

    response = client.post("/admin/discovery/run", json={
        "topic_id": str(other.id), "source_id": str(source.id),
    })
    assert response.status_code == 404


# ==========================================================================
# Listing
# ==========================================================================


@pytest.fixture()
def populated(client, db, topic, source, stub_service):
    stub_service["provider"] = StubProvider([
        video("aaaaaaaaaaa", title="Mais recente", published_at=NOW),
        video("bbbbbbbbbbb", title="Do meio", published_at=NOW - timedelta(days=2)),
        video("ccccccccccc", title="Mais antigo", published_at=NOW - timedelta(days=5)),
    ])
    client.post("/admin/discovery/run", json={"topic_id": str(topic.id)})
    return topic


def test_candidates_are_listed_newest_first(client, populated):
    body = client.get("/admin/video-candidates").json()

    assert body["total"] == 3
    assert [item["title"] for item in body["items"]] == ["Mais recente", "Do meio", "Mais antigo"]


def test_the_list_is_paginated(client, populated):
    first = client.get("/admin/video-candidates?limit=2&offset=0").json()
    second = client.get("/admin/video-candidates?limit=2&offset=2").json()

    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert first["total"] == second["total"] == 3
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})


def test_the_page_size_is_capped(client, populated):
    assert client.get("/admin/video-candidates?limit=5000").status_code == 422


def test_candidates_can_be_filtered_by_topic(client, db, populated):
    other = ContentTopic(name="Outro")
    db.add(other)
    db.flush()

    assert client.get(f"/admin/video-candidates?topic_id={populated.id}").json()["total"] == 3
    assert client.get(f"/admin/video-candidates?topic_id={other.id}").json()["total"] == 0


def test_candidates_can_be_filtered_by_status(client, db, populated):
    row = db.query(VideoCandidate).first()
    row.status = VideoCandidateStatus.REJECTED
    db.flush()

    assert client.get("/admin/video-candidates?status=discovered").json()["total"] == 2
    assert client.get("/admin/video-candidates?status=rejected").json()["total"] == 1


def test_candidates_can_be_filtered_by_publish_window(client, populated):
    # Passed as params, not interpolated: an ISO timestamp ends in "+00:00", and a raw "+"
    # in a query string decodes to a space.
    body = client.get(
        "/admin/video-candidates",
        params={"published_after": (NOW - timedelta(days=3)).isoformat()},
    ).json()
    assert body["total"] == 2


def test_candidates_can_be_filtered_by_discovery_window(client, populated):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    body = client.get("/admin/video-candidates", params={"discovered_after": future}).json()
    assert body["total"] == 0


def test_candidates_can_be_filtered_by_source(client, populated, source):
    assert client.get(f"/admin/video-candidates?source_id={source.id}").json()["total"] == 3


# ==========================================================================
# Detail
# ==========================================================================


def test_candidate_detail_includes_the_normalized_metadata(client, db, populated):
    candidate_id = db.query(VideoCandidate).first().id

    body = client.get(f"/admin/video-candidates/{candidate_id}").json()

    assert body["dedup_key"].startswith("youtube:")
    assert body["description"] == "Descricao"
    assert body["provider"] == "youtube"
    assert body["first_discovered_at"] is not None
    assert body["last_seen_at"] is not None


def test_an_unknown_candidate_is_a_404(client):
    assert client.get(f"/admin/video-candidates/{uuid.uuid4()}").status_code == 404


def test_the_list_view_stays_compact(client, populated):
    """Descriptions and raw payloads belong on the detail route, not in every list row."""
    item = client.get("/admin/video-candidates").json()["items"][0]
    assert "description" not in item
    assert "raw_metadata" not in item


# ==========================================================================
# Manual promotion — the discovery/production boundary
# ==========================================================================


def test_discovery_alone_creates_no_pipeline_job(client, db, populated):
    from app.models.pipeline_job import PipelineJob

    assert db.query(PipelineJob).count() == 0


def test_a_human_can_select_a_candidate(client, db, populated):
    """PR-ADMISSION-01 split this route.

    It used to mark the candidate SELECTED *and* create a PipelineJob — which it then never
    enqueued, so every manual selection left a run no worker would ever claim. Selection and
    admission are separate decisions; starting production is now
    POST /admin/video-candidates/{id}/admit.
    """
    from app.models.pipeline_job import PipelineJob

    candidate_id = db.query(VideoCandidate).first().id

    response = client.post(f"/admin/video-candidates/{candidate_id}/select")

    assert response.status_code == 200
    body = response.json()
    assert body["candidate"]["status"] == "selected"
    assert body["admitted"] is False
    assert db.query(PipelineJob).count() == 0, "selecting is not starting production"


def test_selection_is_not_a_selection_engine(client, db, populated):
    """It applies no policy: nothing is selected unless a person asks for that row."""
    candidate_id = db.query(VideoCandidate).first().id
    client.post(f"/admin/video-candidates/{candidate_id}/select")

    remaining = db.query(VideoCandidate).filter(VideoCandidate.id != candidate_id).all()
    assert all(row.status == VideoCandidateStatus.DISCOVERED for row in remaining)


def test_a_candidate_cannot_be_promoted_twice(client, db, populated):
    candidate_id = db.query(VideoCandidate).first().id
    client.post(f"/admin/video-candidates/{candidate_id}/select")

    assert client.post(f"/admin/video-candidates/{candidate_id}/select").status_code == 409


def test_a_rejected_candidate_cannot_be_promoted(client, db, populated):
    row = db.query(VideoCandidate).first()
    row.status = VideoCandidateStatus.REJECTED
    db.flush()

    assert client.post(f"/admin/video-candidates/{row.id}/select").status_code == 409


def test_promotion_is_audited(client, db, populated):
    from app.models.audit_log import AuditLog

    candidate_id = db.query(VideoCandidate).first().id
    client.post(f"/admin/video-candidates/{candidate_id}/select")

    actions = {entry.action for entry in db.query(AuditLog).all()}
    assert "admin.discovery.candidate.select" in actions


def test_a_manual_discovery_run_is_audited(client, db, topic, source, stub_service):
    from app.models.audit_log import AuditLog

    stub_service["provider"] = StubProvider([video("aaaaaaaaaaa")])
    client.post("/admin/discovery/run", json={"topic_id": str(topic.id)})

    actions = {entry.action for entry in db.query(AuditLog).all()}
    assert "admin.discovery.run" in actions


# ==========================================================================
# Authorization
# ==========================================================================


@pytest.fixture()
def anonymous_client(db, no_event_fanout):
    """No admin override: the real dependency runs and finds no credentials."""
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/admin/video-candidates"),
        ("get", "/admin/content-topics"),
        ("get", "/admin/discovery-sources"),
        ("post", "/admin/discovery/run"),
    ],
)
def test_discovery_routes_require_authentication(anonymous_client, method, path):
    call = getattr(anonymous_client, method)
    response = (
        call(path, json={"topic_id": str(uuid.uuid4())}) if method == "post" else call(path)
    )
    assert response.status_code in (401, 403), f"{path} answered {response.status_code}"
