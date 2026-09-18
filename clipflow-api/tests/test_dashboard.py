"""O painel.

A tela fazia dez chamadas e somava no navegador — saúde de processo, profundidade de fila,
contagem de heartbeat. Tudo verdadeiro e nada sobre o produto. O que se afirma aqui é sobre o
que ele passou a responder: quantos vídeos entraram e saíram na janela, onde o trabalho está
parado, quantos ciclos rodaram *e produziram*, e o que os cortes publicados renderam.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import api_router
from app.db.session import get_db
from app.models.automation_run import AutomationRun
from app.models.enums import (
    PipelineState,
    PublishAttemptStatus,
    PublishPlatform,
    PublishTargetConnectionStatus,
    UserRole,
    UserStatus,
    VideoCandidateStatus,
)
from app.models.pipeline import Pipeline
from app.models.publish_attempt import PublishAttempt
from app.models.publish_target import PublishTarget
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.models.video_performance_snapshot import VideoPerformanceSnapshot
from app.security.auth_middleware import get_current_admin
from tests.conftest import make_run

NOW = datetime.now(timezone.utc)


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


def target(db):
    row = PublishTarget(
        platform=PublishPlatform.YOUTUBE,
        name="Canal",
        is_active=True,
        connection_status=PublishTargetConnectionStatus.CONNECTED,
        config_json={},
    )
    db.add(row)
    db.flush()
    return row


def publication(db, channel, *, external_id, finished_at, views=None):
    job = make_run(db, state=PipelineState.PUBLISHED)
    attempt = PublishAttempt(
        pipeline_job_id=job.id,
        target_id=channel.id,
        media_identity=f"final_clips/{external_id}.mp4",
        media_storage_key=f"jobs/x/{external_id}.mp4",
        status=PublishAttemptStatus.SUCCEEDED,
        attempt_no=1,
        max_attempts=3,
        initiator="automatic",
        external_id=external_id,
        finished_at=finished_at,
    )
    db.add(attempt)
    db.flush()
    if views is not None:
        db.add(
            VideoPerformanceSnapshot(
                publish_attempt_id=attempt.id,
                publish_target_id=channel.id,
                external_video_id=external_id,
                provider="youtube",
                captured_at=finished_at,
                capture_slot="h24",
                view_count=views,
                like_count=views // 10,
                availability="ok",
            )
        )
        db.flush()
    return attempt


# ===========================================================================
# O funil
# ===========================================================================


def test_the_funnel_counts_the_window_not_the_stock(client, db, pipeline):
    """"12 encontrados, 3 publicados" só quer dizer algo se olharem a mesma janela.

    Um candidato de trinta dias atrás não pertence à semana, e contá-lo produziria uma taxa
    de conversão que não descreve nada.
    """
    db.add(
        VideoCandidate(
            pipeline_id=pipeline.id,
            url="https://youtu.be/novo",
            title="novo",
            status=VideoCandidateStatus.DISCOVERED,
        )
    )
    db.flush()
    velho = VideoCandidate(
        pipeline_id=pipeline.id,
        url="https://youtu.be/velho",
        title="velho",
        status=VideoCandidateStatus.DISCOVERED,
    )
    db.add(velho)
    db.flush()
    velho.created_at = NOW - timedelta(days=30)
    db.commit()

    flow = client.get("/admin/dashboard", params={"days": 7}).json()["flow"]

    assert flow["found"] == 1


def test_a_rate_with_nothing_to_convert_is_null_not_zero(client, db):
    """0% leria como "tudo falhou". O que houve foi nada ter entrado."""
    flow = client.get("/admin/dashboard").json()["flow"]

    assert flow["found"] == 0
    assert flow["selection_rate"] is None


# ===========================================================================
# O gargalo
# ===========================================================================


def test_the_bottleneck_names_the_stage_holding_the_most(client, db, pipeline):
    for _ in range(3):
        make_run(db, pipeline_id=pipeline.id, state=PipelineState.TRANSCRIBING)
    make_run(db, pipeline_id=pipeline.id, state=PipelineState.RENDERING)
    db.commit()

    bottleneck = client.get("/admin/dashboard").json()["bottleneck"]

    assert bottleneck["in_flight"] == 4
    assert bottleneck["worst"]["count"] == 3
    # Em português, não o enum: o painel fala de trabalho.
    assert bottleneck["worst"]["label"] == "Transcrevendo"


def test_work_waiting_on_a_person_is_counted_apart(client, db, pipeline):
    """Somá-lo ao "em andamento" faria a fila parecer viva quando ela espera alguém."""
    make_run(db, pipeline_id=pipeline.id, state=PipelineState.RENDERING)
    make_run(db, pipeline_id=pipeline.id, state=PipelineState.REVIEW_REQUIRED)
    db.commit()

    bottleneck = client.get("/admin/dashboard").json()["bottleneck"]

    assert bottleneck["in_flight"] == 1
    assert bottleneck["waiting_on_a_person"] == 1


# ===========================================================================
# Os ciclos
# ===========================================================================


def test_running_and_producing_are_counted_separately(client, db, pipeline):
    """A diferença entre um sistema saudável e um girando em falso."""
    for admitted in (0, 0, 2):
        db.add(
            AutomationRun(
                pipeline_id=pipeline.id,
                trigger="tick",
                status="completed",
                started_at=NOW - timedelta(minutes=5),
                finished_at=NOW,
                admitted=admitted,
                production_status="none",
            )
        )
    db.commit()

    cycles = client.get("/admin/dashboard").json()["cycles"]

    assert cycles["total"] == 3
    assert cycles["productive"] == 1


# ===========================================================================
# O desempenho
# ===========================================================================


def test_an_unmeasured_video_is_reported_apart_not_as_zero(client, db):
    """Sem isto, "média de 0 views" seria lido como fracasso quando a coleta é que não rodou."""
    channel = target(db)
    publication(db, channel, external_id="vid_medido", finished_at=NOW, views=900)
    publication(db, channel, external_id="vid_nao_medido", finished_at=NOW)
    db.commit()

    performance = client.get("/admin/dashboard").json()["performance"]

    assert performance["videos"] == 2
    assert performance["measured"] == 1
    assert performance["unmeasured"] == 1
    assert performance["views"] == 900
    assert performance["avg_views"] == 900


def test_with_nothing_published_the_totals_are_null(client, db):
    performance = client.get("/admin/dashboard").json()["performance"]

    assert performance["videos"] == 0
    assert performance["views"] is None
    assert performance["avg_views"] is None


def test_the_top_is_ordered_by_views(client, db):
    channel = target(db)
    publication(db, channel, external_id="vid_a", finished_at=NOW, views=100)
    publication(db, channel, external_id="vid_b", finished_at=NOW, views=900)
    db.commit()

    top = client.get("/admin/dashboard").json()["performance"]["top"]

    assert [row["external_id"] for row in top] == ["vid_b", "vid_a"]


# ===========================================================================
# O que precisa de você
# ===========================================================================


def test_a_blocked_pipeline_is_named_with_its_first_blocker(client, db, pipeline):
    """A tela oferece "Resolver"; sem o motivo o operador clica sem saber no que vai cair."""
    db.commit()

    attention = client.get("/admin/dashboard").json()["attention"]

    assert len(attention["blocked_pipelines"]) == 1
    assert attention["blocked_pipelines"][0]["name"] == "Serie A"
    assert attention["blocked_pipelines"][0]["first"]


def test_the_dashboard_requires_the_operator(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        assert anonymous.get("/admin/dashboard").status_code in (401, 403)
