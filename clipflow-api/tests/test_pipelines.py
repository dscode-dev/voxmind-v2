"""The pipeline CRUD.

The object an operator creates and configures. What is asserted here is mostly about it being
operable: that identity and policy are edited from one payload, that the counts say whether it
is doing anything, that a name collision is a sentence rather than a constraint error, and
that deleting a configuration does not erase the record of videos that were really published.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import api_router
from app.db.session import get_db
from app.models.automation_state import AutomationState
from app.models.discovery_source import DiscoverySource
from app.models.enums import (
    DiscoverySourceKind,
    PipelineState,
    PublishPlatform,
    PublishTargetConnectionStatus,
    UserRole,
    UserStatus,
    VideoCandidateStatus,
)
from app.models.pipeline import Pipeline
from app.models.publish_target import PublishTarget
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin
from tests.conftest import make_run


@pytest.fixture()
def admin_user(db):
    user = User(
        phone_number="+5511999999999",
        full_name="Admin",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
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
def target(db):
    row = PublishTarget(
        platform=PublishPlatform.YOUTUBE,
        name="Voxmind FC",
        channel_title="Voxmind FC",
        is_active=True,
        connection_status=PublishTargetConnectionStatus.CONNECTED,
        config_json={},
    )
    db.add(row)
    db.flush()
    return row


def make(db, name="Serie A", **overrides):
    pipeline = Pipeline(name=name, keywords_json=["serie a"], is_active=True, **overrides)
    db.add(pipeline)
    db.flush()
    return pipeline


# ===========================================================================
# Create
# ===========================================================================


def test_a_pipeline_is_created_with_its_identity_and_its_policy_at_once(client, target):
    """One payload. The two used to live on different endpoints, and an operator had to know
    which half of the object each field belonged to."""
    response = client.post("/admin/pipelines", json={
        "name": "Serie A — canal principal",
        "theme": "futebol italiano",
        "keywords": ["serie a", "milan"],
        "telegram_chat_id": "-1001234567890",
        "automation": {
            "enabled": True,
            "interval_minutes": 30,
            "publish_target_id": str(target.id),
        },
    })

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Serie A — canal principal"
    assert body["theme"] == "futebol italiano"
    assert body["telegram_chat_id"] == "-1001234567890"
    assert body["automation"]["enabled"] is True
    assert body["automation"]["interval_minutes"] == 30
    assert body["channel"]["name"] == "Voxmind FC"


def test_the_subject_falls_back_to_the_name_but_the_theme_wins(client):
    """An operational name is a fine label and a terrible thing to tell a model about."""
    named_only = client.post("/admin/pipelines", json={"name": "Serie A — canal principal"})
    themed = client.post("/admin/pipelines", json={
        "name": "Serie B — canal secundario", "theme": "futebol italiano segunda divisao",
    })

    assert named_only.json()["subject"] == "Serie A — canal principal"
    assert themed.json()["subject"] == "futebol italiano segunda divisao"


def test_a_blank_telegram_chat_is_stored_as_absent_not_as_empty(client):
    """`""` on a screen looks configured. It is not."""
    body = client.post("/admin/pipelines", json={
        "name": "Sem telegram", "telegram_chat_id": "   ",
    }).json()

    assert body["telegram_chat_id"] is None


def test_a_duplicate_name_is_a_sentence_not_a_constraint_error(client):
    client.post("/admin/pipelines", json={"name": "Serie A"})
    response = client.post("/admin/pipelines", json={"name": "  serie a  "})

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_an_unknown_channel_is_refused_rather_than_stored(client):
    """A target id that resolves to nothing would publish nowhere, silently."""
    response = client.post("/admin/pipelines", json={
        "name": "Serie A",
        "automation": {"publish_target_id": str(uuid.uuid4())},
    })

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown publish target"


def test_policy_is_bounded_by_the_schema(client):
    response = client.post("/admin/pipelines", json={
        "name": "Serie A", "automation": {"selection_limit": 10_000},
    })

    assert response.status_code == 422


# ===========================================================================
# Read
# ===========================================================================


def test_the_list_says_whether_a_pipeline_is_doing_anything(client, db):
    """Counts, not just names: a list of configurations that all look identical is not a
    console, it is a settings file with a nicer font."""
    pipeline = make(db)
    db.add(DiscoverySource(
        pipeline_id=pipeline.id, kind=DiscoverySourceKind.YOUTUBE_SEARCH,
        name="Buscas", is_active=True, config_json={"queries": ["serie a"]},
    ))
    for index, status_value in enumerate(
        [VideoCandidateStatus.DISCOVERED, VideoCandidateStatus.SELECTED,
         VideoCandidateStatus.SELECTED]
    ):
        db.add(VideoCandidate(
            pipeline_id=pipeline.id, url=f"https://youtu.be/v{index}",
            title=f"video {index}", status=status_value,
        ))
    make_run(db, pipeline_id=pipeline.id, state=PipelineState.PUBLISHED)
    make_run(db, pipeline_id=pipeline.id, state=PipelineState.RENDERING)
    db.commit()

    counts = client.get("/admin/pipelines").json()[0]["counts"]

    assert counts == {
        "sources": 1, "candidates": 3, "selected": 2, "productions": 2, "published": 1,
    }


def test_the_detail_carries_the_schedule_state_when_there_is_one(client, db):
    pipeline = make(db)
    db.add(AutomationState(pipeline_id=pipeline.id, last_status="completed",
                           consecutive_failures=0))
    db.commit()

    body = client.get(f"/admin/pipelines/{pipeline.id}").json()

    assert body["state"]["last_status"] == "completed"
    assert body["state"]["consecutive_failures"] == 0


def test_an_unknown_pipeline_is_a_404(client):
    assert client.get(f"/admin/pipelines/{uuid.uuid4()}").status_code == 404


# ===========================================================================
# Update
# ===========================================================================


def test_an_update_changes_what_it_names_and_nothing_else(client, db):
    pipeline = make(db, name="Serie A", theme="futebol")
    db.commit()

    body = client.put(f"/admin/pipelines/{pipeline.id}", json={"is_active": False}).json()

    assert body["is_active"] is False
    assert body["name"] == "Serie A"
    assert body["theme"] == "futebol"
    assert body["keywords"] == ["serie a"]


def test_policy_merges_rather_than_replacing(client, db):
    """Sending one switch must not reset the rest of the configuration to defaults."""
    pipeline = make(db)
    client.put(f"/admin/pipelines/{pipeline.id}",
               json={"automation": {"enabled": True, "interval_minutes": 15}})

    body = client.put(f"/admin/pipelines/{pipeline.id}",
                      json={"automation": {"selection_limit": 5}}).json()

    assert body["automation"]["enabled"] is True
    assert body["automation"]["interval_minutes"] == 15
    assert body["automation"]["selection_limit"] == 5


def test_renaming_onto_another_pipeline_is_refused(client, db):
    make(db, name="Serie A")
    other = make(db, name="Premier League")
    db.commit()

    assert client.put(f"/admin/pipelines/{other.id}",
                      json={"name": "Serie A"}).status_code == 409


def test_a_pipeline_can_keep_its_own_name(client, db):
    """The duplicate check must not fire on the row being edited."""
    pipeline = make(db, name="Serie A")
    db.commit()

    response = client.put(f"/admin/pipelines/{pipeline.id}",
                          json={"name": "Serie A", "description": "cortes"})

    assert response.status_code == 200
    assert response.json()["description"] == "cortes"


# ===========================================================================
# Delete
# ===========================================================================


def test_deleting_takes_the_candidates_and_leaves_the_published_videos(client, db):
    """A candidate only ever meant "a video this pipeline found". A production is a video
    that was really made, and erasing it to tidy up a configuration change would destroy the
    only record that it happened."""
    pipeline = make(db)
    db.add(VideoCandidate(pipeline_id=pipeline.id, url="https://youtu.be/v",
                          title="v", status=VideoCandidateStatus.DISCOVERED))
    job = make_run(db, pipeline_id=pipeline.id, state=PipelineState.PUBLISHED)
    job_id = job.id
    db.commit()

    response = client.delete(f"/admin/pipelines/{pipeline.id}")

    assert response.status_code == 200
    assert response.json()["kept_productions"] == 1
    assert db.query(VideoCandidate).count() == 0

    from app.models.pipeline_job import PipelineJob
    survivor = db.query(PipelineJob).filter(PipelineJob.id == job_id).first()
    assert survivor is not None
    assert survivor.pipeline_id is None


def test_deleting_an_unknown_pipeline_is_a_404(client):
    assert client.delete(f"/admin/pipelines/{uuid.uuid4()}").status_code == 404


# ===========================================================================
# Access
# ===========================================================================


@pytest.mark.parametrize("method,path", [
    ("get", "/admin/pipelines"),
    ("post", "/admin/pipelines"),
    ("get", "/admin/pipelines/{id}"),
    ("put", "/admin/pipelines/{id}"),
    ("delete", "/admin/pipelines/{id}"),
])
def test_every_route_requires_the_operator(db, no_event_fanout, method, path):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        url = path.format(id=uuid.uuid4())
        # `get` and `delete` take no body in this client.
        kwargs = {"json": {"name": "x"}} if method in {"post", "put"} else {}
        response = getattr(anonymous, method)(url, **kwargs)
        assert response.status_code in (401, 403), f"{path} answered {response.status_code}"


def test_no_credential_reaches_the_channel_block(client, db, target):
    pipeline = make(db, metadata_json={"automation": {"publish_target_id": str(target.id)}})
    db.commit()

    raw = client.get(f"/admin/pipelines/{pipeline.id}").text

    for forbidden in ("refresh_token", "encrypted", "access_token"):
        assert forbidden not in raw


# ===========================================================================
# Telegram
# ===========================================================================


def test_the_run_carries_the_pipelines_own_chat_to_the_worker(db, no_event_fanout):
    """Per pipeline, not per deployment.

    Two pipelines feeding two channels are watched by two different people, and one global
    chat turns both streams into one unreadable one.
    """
    from app.services.admission_service import ProductionAdmissionService

    pipeline = make(db, telegram_chat_id="-1001234567890")
    run = make_run(db, pipeline_id=pipeline.id, state=PipelineState.QUEUED)
    db.commit()

    payload = ProductionAdmissionService._payload(run, {"frozen": {}})

    assert payload["telegram_chat_id"] == "-1001234567890"


def test_a_pipeline_with_no_chat_says_nothing_rather_than_borrowing_one(db, no_event_fanout):
    """Absent, not a fallback. The deployment default is some other pipeline's chat."""
    from app.services.admission_service import ProductionAdmissionService

    pipeline = make(db)
    run = make_run(db, pipeline_id=pipeline.id, state=PipelineState.QUEUED)
    db.commit()

    assert "telegram_chat_id" not in ProductionAdmissionService._payload(run, {"frozen": {}})


# ===========================================================================
# Catálogo de temas
# ===========================================================================


def test_the_theme_catalogue_is_served_with_its_keywords(client):
    """Escolher o tema e ter de inventar os termos de busca na mão é escolher metade."""
    groups = client.get("/admin/pipeline-themes").json()["groups"]

    assert len(groups) >= 2
    items = [item for group in groups for item in group["items"]]
    assert all(item["id"] and item["label"] and item["keywords"] for item in items)
    assert any(item["id"] == "serie_a_italiana" for item in items)


def test_outros_is_not_a_theme_in_the_catalogue(client):
    """`Outros` é uma escolha da interface, não um tema.

    Se viesse daqui, um pipeline poderia acabar gravado com o assunto literal "Outros".
    """
    groups = client.get("/admin/pipeline-themes").json()["groups"]
    labels = {item["label"].lower() for group in groups for item in group["items"]}

    assert "outros" not in labels


def test_a_theme_typed_by_hand_is_stored_as_written(client):
    """O catálogo sugere; ele não restringe. O campo continua sendo texto."""
    body = client.post("/admin/pipelines", json={
        "name": "Xadrez", "theme": "torneios de xadrez rápido",
    }).json()

    assert body["theme"] == "torneios de xadrez rápido"
    assert body["subject"] == "torneios de xadrez rápido"


def test_the_theme_catalogue_requires_the_operator(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        assert anonymous.get("/admin/pipeline-themes").status_code in (401, 403)
