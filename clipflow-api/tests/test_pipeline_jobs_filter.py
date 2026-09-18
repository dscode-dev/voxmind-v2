"""As produções de um pipeline são recortadas pelo servidor.

A tela de detalhe pedia as 50 produções mais recentes do sistema e filtrava no navegador por
`job.topic.id`. A API deixou de mandar `topic` quando `ContentTopic` virou `Pipeline` — passou
a mandar `pipeline` — então o filtro nunca casava e a aba ficava permanentemente vazia,
enquanto o cartão ao lado contava certo. A mesma tela dizia "PRODUÇÕES 1" e "Nenhuma produção
ainda".

Dois problemas num: o nome do campo, e filtrar uma página no cliente — que perderia as
produções deste pipeline que não coubessem nas mais recentes do sistema inteiro.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import api_router
from app.db.session import get_db
from app.models.enums import PipelineState, UserRole, UserStatus
from app.models.pipeline import Pipeline
from app.models.user import User
from app.security.auth_middleware import get_current_admin
from tests.conftest import make_run


@pytest.fixture()
def admin_user(db):
    user = User(
        phone_number="+5511988887777",
        full_name="Dono",
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


def _pipeline(db, name):
    row = Pipeline(name=name, keywords_json=[name.lower()], is_active=True)
    db.add(row)
    db.flush()
    return row


def test_a_pipeline_sees_only_its_own_productions(client, db):
    meu = _pipeline(db, "PipeOne")
    outro = _pipeline(db, "PipeDois")
    make_run(db, pipeline_id=meu.id, state=PipelineState.RENDERING)
    make_run(db, pipeline_id=outro.id, state=PipelineState.RENDERING)
    db.commit()

    page = client.get("/admin/pipeline-jobs", params={"pipeline_id": str(meu.id)}).json()

    assert page["total"] == 1
    assert page["items"][0]["pipeline"]["name"] == "PipeOne"


def test_the_count_is_the_pipelines_own_not_the_page(client, db):
    """Filtrar no navegador perdia o que não coubesse na página pedida, e o total ficava
    sendo o do sistema inteiro."""
    meu = _pipeline(db, "PipeOne")
    outro = _pipeline(db, "PipeDois")
    for _ in range(3):
        make_run(db, pipeline_id=meu.id, state=PipelineState.RENDERING)
    for _ in range(5):
        make_run(db, pipeline_id=outro.id, state=PipelineState.RENDERING)
    db.commit()

    page = client.get(
        "/admin/pipeline-jobs", params={"pipeline_id": str(meu.id), "limit": 2}
    ).json()

    assert page["total"] == 3
    assert len(page["items"]) == 2


def test_the_run_names_its_pipeline_under_the_key_the_screen_reads(client, db):
    """O contrato que divergiu. `topic` some, `pipeline` fica — e a tela lê `pipeline`."""
    meu = _pipeline(db, "PipeOne")
    make_run(db, pipeline_id=meu.id, state=PipelineState.RENDERING)
    db.commit()

    item = client.get("/admin/pipeline-jobs").json()["items"][0]

    assert "topic" not in item
    assert item["pipeline"]["id"] == str(meu.id)


def test_without_the_filter_everything_is_listed(client, db):
    """O controle: o filtro é opcional, e a lista geral continua existindo."""
    meu = _pipeline(db, "PipeOne")
    outro = _pipeline(db, "PipeDois")
    make_run(db, pipeline_id=meu.id, state=PipelineState.RENDERING)
    make_run(db, pipeline_id=outro.id, state=PipelineState.RENDERING)
    db.commit()

    assert client.get("/admin/pipeline-jobs").json()["total"] == 2
