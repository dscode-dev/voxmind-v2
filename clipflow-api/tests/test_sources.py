"""As fontes de um pipeline.

Elas eram um par create-and-list sem nenhum verbo de edição, então um `feed_url` digitado
errado era permanente a menos que alguém abrisse o psql. O que se afirma aqui é sobre isso:
que dá para corrigir e remover, que a configuração é conferida na hora em que é escrita — e
não uma hora depois, dentro de uma execução agendada que ninguém está olhando — e que apagar
uma fonte não retira os vídeos que ela já encontrou.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import api_router
from app.db.session import get_db
from app.models.discovery_source import DiscoverySource
from app.models.enums import (
    DiscoverySourceKind,
    UserRole,
    UserStatus,
    VideoCandidateStatus,
)
from app.models.pipeline import Pipeline
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin


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
def pipeline(db):
    row = Pipeline(name="Serie A", keywords_json=["serie a"], is_active=True)
    db.add(row)
    db.flush()
    return row


def make_source(db, pipeline, **overrides):
    fields = dict(
        pipeline_id=pipeline.id,
        kind=DiscoverySourceKind.YOUTUBE_SEARCH,
        name="Buscas",
        is_active=True,
        config_json={"queries": ["serie a"]},
    )
    fields.update(overrides)
    source = DiscoverySource(**fields)
    db.add(source)
    db.flush()
    return source


# ===========================================================================
# Create
# ===========================================================================


def test_a_source_is_created_inside_its_pipeline(client, pipeline):
    response = client.post(f"/admin/pipelines/{pipeline.id}/sources", json={
        "kind": "youtube_search",
        "name": "Coletivas",
        "config": {"queries": ["coletiva pos jogo"], "max_results": 10},
    })

    assert response.status_code == 201
    body = response.json()
    assert body["pipeline_id"] == str(pipeline.id)
    assert body["config"]["queries"] == ["coletiva pos jogo"]
    assert body["config"]["max_results"] == 10


def test_an_rss_source_without_a_feed_is_refused_now_not_at_the_next_run(client, pipeline):
    """O erro tem que chegar em quem digitou.

    Aceita em silêncio, a fonte quebra uma hora depois dentro de uma execução agendada, longe
    de quem poderia corrigi-la.
    """
    response = client.post(f"/admin/pipelines/{pipeline.id}/sources", json={
        "kind": "rss", "config": {},
    })

    assert response.status_code == 422
    assert "feed_url" in response.json()["detail"]


def test_an_rss_feed_is_stored_trimmed(client, pipeline):
    body = client.post(f"/admin/pipelines/{pipeline.id}/sources", json={
        "kind": "rss", "config": {"feed_url": "  https://example.invalid/feed  "},
    }).json()

    assert body["config"]["feed_url"] == "https://example.invalid/feed"


def test_an_empty_query_list_survives_because_it_means_something(client, pipeline):
    """Lista vazia é "pegue tudo que este feed publicar" — o canal é o filtro.

    Se fosse podida junto com os campos em branco, a busca herdaria as palavras-chave do
    pipeline e descartaria a maior parte do que o canal publica.
    """
    body = client.post(f"/admin/pipelines/{pipeline.id}/sources", json={
        "kind": "youtube_search", "config": {"queries": []},
    }).json()

    assert body["config"]["queries"] == []


def test_blank_configuration_values_are_dropped(client, pipeline):
    body = client.post(f"/admin/pipelines/{pipeline.id}/sources", json={
        "kind": "youtube_search", "config": {"language": "", "region": "BR"},
    }).json()

    assert "language" not in body["config"]
    assert body["config"]["region"] == "BR"


def test_a_non_numeric_limit_is_refused(client, pipeline):
    response = client.post(f"/admin/pipelines/{pipeline.id}/sources", json={
        "kind": "youtube_search", "config": {"max_results": "muitos"},
    })

    assert response.status_code == 422


def test_a_source_for_an_unknown_pipeline_is_a_404(client):
    response = client.post(f"/admin/pipelines/{uuid.uuid4()}/sources", json={
        "kind": "youtube_search", "config": {},
    })

    assert response.status_code == 404


# ===========================================================================
# Read
# ===========================================================================


def test_the_list_is_scoped_to_the_pipeline(client, db, pipeline):
    other = Pipeline(name="Premier League", keywords_json=[], is_active=True)
    db.add(other)
    db.flush()
    make_source(db, pipeline, name="Minha")
    make_source(db, other, name="Da outra")
    db.commit()

    body = client.get(f"/admin/pipelines/{pipeline.id}/sources").json()

    assert [source["name"] for source in body] == ["Minha"]


# ===========================================================================
# Update
# ===========================================================================


def test_a_mistyped_feed_can_be_corrected(client, db, pipeline):
    """A razão de existir deste PR: antes isto exigia abrir o banco."""
    source = make_source(
        db, pipeline, kind=DiscoverySourceKind.RSS,
        config_json={"feed_url": "https://exemplo.invalid/fed"},
    )
    db.commit()

    body = client.put(
        f"/admin/pipelines/{pipeline.id}/sources/{source.id}",
        json={"config": {"feed_url": "https://exemplo.invalid/feed"}},
    ).json()

    assert body["config"]["feed_url"] == "https://exemplo.invalid/feed"


def test_an_update_can_pause_a_source_without_touching_its_config(client, db, pipeline):
    source = make_source(db, pipeline)
    db.commit()

    body = client.put(
        f"/admin/pipelines/{pipeline.id}/sources/{source.id}", json={"is_active": False}
    ).json()

    assert body["is_active"] is False
    assert body["config"]["queries"] == ["serie a"]


def test_an_update_is_validated_like_a_create(client, db, pipeline):
    source = make_source(
        db, pipeline, kind=DiscoverySourceKind.RSS,
        config_json={"feed_url": "https://exemplo.invalid/feed"},
    )
    db.commit()

    response = client.put(
        f"/admin/pipelines/{pipeline.id}/sources/{source.id}", json={"config": {}}
    )

    assert response.status_code == 422


def test_a_source_cannot_be_reached_through_another_pipeline(client, db, pipeline):
    """Senão a URL de um pipeline editaria a fonte de outro, e a auditoria nomearia o errado."""
    other = Pipeline(name="Premier League", keywords_json=[], is_active=True)
    db.add(other)
    db.flush()
    source = make_source(db, pipeline)
    db.commit()

    response = client.put(
        f"/admin/pipelines/{other.id}/sources/{source.id}", json={"is_active": False}
    )

    assert response.status_code == 404


# ===========================================================================
# Delete
# ===========================================================================


def test_removing_a_source_keeps_the_videos_it_found(client, db, pipeline):
    """Os candidatos são do pipeline, não do feed.

    Apagar um feed que envelheceu não pode retirar vídeos que foram encontrados, avaliados e
    possivelmente já produzidos.
    """
    source = make_source(db, pipeline)
    db.add(VideoCandidate(
        pipeline_id=pipeline.id, source_id=source.id,
        url="https://youtu.be/v", title="v", status=VideoCandidateStatus.SELECTED,
    ))
    db.commit()

    response = client.delete(f"/admin/pipelines/{pipeline.id}/sources/{source.id}")

    assert response.status_code == 200
    assert response.json()["candidates_kept"] == 1
    assert db.query(VideoCandidate).count() == 1
    assert db.query(DiscoverySource).count() == 0


def test_removing_an_unknown_source_is_a_404(client, pipeline):
    assert client.delete(
        f"/admin/pipelines/{pipeline.id}/sources/{uuid.uuid4()}"
    ).status_code == 404


# ===========================================================================
# Kinds
# ===========================================================================


def test_a_kind_that_no_provider_serves_is_not_offered(client, pipeline):
    """`youtube_trending` estava ligado ao provider de *busca* e `news` não tinha provider.

    Uma opção que silenciosamente faz outra coisa é pior do que opção nenhuma.
    """
    for dead in ("youtube_trending", "news"):
        response = client.post(
            f"/admin/pipelines/{pipeline.id}/sources", json={"kind": dead, "config": {}}
        )
        assert response.status_code == 422, dead


# ===========================================================================
# Access
# ===========================================================================


@pytest.mark.parametrize("method", ["get", "post"])
def test_source_routes_require_the_operator(db, no_event_fanout, method):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    url = f"/admin/pipelines/{uuid.uuid4()}/sources"
    with TestClient(app) as anonymous:
        kwargs = {"json": {"kind": "youtube_search"}} if method == "post" else {}
        assert getattr(anonymous, method)(url, **kwargs).status_code in (401, 403)
