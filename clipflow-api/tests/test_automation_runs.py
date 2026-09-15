"""O registro de um ciclo.

O relatório já era completo — cada estágio com suas contagens e seus motivos — e era jogado
fora: saía na resposta HTTP de quem disparou o tick e em mais lugar nenhum. O que se afirma
aqui é que ele vira uma linha, que um ciclo que não fez nada também vira (é a resposta para
"por que nada está acontecendo?"), e que "o ciclo terminou" e "os cortes terminaram" são duas
coisas diferentes que a tela não pode confundir.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import api_router
from app.db.session import get_db
from app.models.automation_run import AutomationRun
from app.models.enums import PipelineState, UserRole, UserStatus
from app.models.pipeline import Pipeline
from app.models.user import User
from app.security.auth_middleware import get_current_admin
from app.services.automation_run_service import (
    COMPLETE,
    NONE,
    PARTIAL,
    RUNNING,
    AutomationRunService,
)
from app.services.automation_service import AutomationRunReport, StageResult
from tests.conftest import make_run as make_production

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


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


def report(**overrides) -> AutomationRunReport:
    built = AutomationRunReport(
        automation_run_id=str(uuid.uuid4()),
        pipeline_id="ignored",
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=4),
    )
    built.status = overrides.pop("status", "completed")
    built.duration_ms = overrides.pop("duration_ms", 4000)
    built.discovery = StageResult(
        "discovery", counts={"new_candidates": overrides.pop("discovered", 0)}
    )
    built.selection = StageResult(
        "selection", counts={"selected": overrides.pop("selected", 0)}
    )
    built.admission = StageResult(
        "admission", counts={"admitted": overrides.pop("admitted", 0)}
    )
    built.publication = StageResult(
        "publication", counts={"queued": overrides.pop("queued", 0)}
    )
    for key, value in overrides.items():
        setattr(built, key, value)
    return built


# ===========================================================================
# Recording
# ===========================================================================


def test_a_cycle_becomes_a_row_with_its_stages(db, pipeline):
    built = report(discovered=5, selected=2, admitted=1, queued=1)

    run = AutomationRunService().record(
        db, pipeline_id=pipeline.id, report=built, trigger="manual", actor="+5511999999999"
    )

    assert run is not None
    assert run.discovered == 5
    assert run.selected == 2
    assert run.admitted == 1
    assert run.publications_queued == 1
    assert run.trigger == "manual"
    assert set(run.stages_json) == {"discovery", "selection", "admission", "publication"}


def test_queued_is_not_published(db, pipeline):
    """O publicador pode nem estar rodando.

    Uma coluna que juntasse os dois diria que há vídeos num canal que nada enviou.
    """
    run = AutomationRunService().record(
        db, pipeline_id=pipeline.id, report=report(admitted=1, queued=3), trigger="tick"
    )

    assert run.publications_queued == 3
    assert run.published == 0


def test_a_cycle_that_did_nothing_is_still_recorded(db, pipeline):
    """É a resposta para "por que nada está acontecendo?"."""
    built = report(status="skipped", skip_reason="pipeline_disabled")

    run = AutomationRunService().record(
        db, pipeline_id=pipeline.id, report=built, trigger="tick"
    )

    assert run.status == "skipped"
    assert run.skip_reason == "pipeline_disabled"
    assert run.production_status == NONE
    assert run.settled_at is not None


def test_a_cycle_with_nothing_admitted_settles_immediately(db, pipeline):
    run = AutomationRunService().record(
        db, pipeline_id=pipeline.id, report=report(discovered=9, admitted=0), trigger="tick"
    )

    assert run.production_status == NONE
    assert run.settled_at == NOW + timedelta(seconds=4)


def test_a_cycle_that_admitted_stays_open(db, pipeline):
    """Admissão é onde o ciclo deixa de ser síncrono.

    Marcá-lo terminado ali reportaria sucesso para cortes que ainda nem foram renderizados.
    """
    run = AutomationRunService().record(
        db, pipeline_id=pipeline.id, report=report(admitted=2), trigger="tick"
    )

    assert run.production_status == RUNNING
    assert run.settled_at is None


def test_a_record_that_cannot_be_written_does_not_raise(db, pipeline):
    """Um ciclo que funcionou e cujo registro se perdeu ainda é um ciclo que funcionou."""
    class Exploding:
        automation_run_id = "x"

        def as_dict(self):
            raise RuntimeError("boom")

    assert AutomationRunService().record(
        db, pipeline_id=pipeline.id, report=Exploding(), trigger="tick"
    ) is None


# ===========================================================================
# Settling
# ===========================================================================


def attach_productions(db, run, pipeline, states):
    ids = []
    for state in states:
        job = make_production(db, pipeline_id=pipeline.id, state=state)
        ids.append(job.id)
    AutomationRunService().attach(db, run.id, ids)
    db.flush()
    return ids


def test_a_run_waiting_on_a_render_is_not_settled(db, pipeline):
    service = AutomationRunService()
    run = service.record(
        db, pipeline_id=pipeline.id, report=report(admitted=2), trigger="tick"
    )
    attach_productions(db, run, pipeline, [PipelineState.PUBLISHED, PipelineState.RENDERING])
    db.flush()

    assert service.settle_finished(db) == 0
    db.refresh(run)
    assert run.production_status == RUNNING
    assert run.settled_at is None


def test_a_run_whose_cuts_all_published_is_complete(db, pipeline):
    service = AutomationRunService()
    run = service.record(
        db, pipeline_id=pipeline.id, report=report(admitted=2), trigger="tick"
    )
    attach_productions(db, run, pipeline, [PipelineState.PUBLISHED, PipelineState.PUBLISHED])
    db.flush()

    assert service.settle_finished(db) == 1
    db.refresh(run)
    assert run.production_status == COMPLETE
    assert run.published == 2
    assert run.settled_at is not None


def test_a_run_that_ended_with_a_failure_is_partial_not_complete(db, pipeline):
    """Terminado não é bem-sucedido. Um ciclo com um corte perdido tem que aparecer assim."""
    service = AutomationRunService()
    run = service.record(
        db, pipeline_id=pipeline.id, report=report(admitted=2), trigger="tick"
    )
    attach_productions(db, run, pipeline, [PipelineState.PUBLISHED, PipelineState.FAILED])
    db.flush()

    service.settle_finished(db)
    db.refresh(run)
    assert run.production_status == PARTIAL
    assert run.published == 1


def test_a_run_waiting_on_a_person_counts_as_ended(db, pipeline):
    """`review_required` não vai se mover sozinho — o ciclo não fica aberto para sempre."""
    service = AutomationRunService()
    run = service.record(
        db, pipeline_id=pipeline.id, report=report(admitted=1), trigger="tick"
    )
    attach_productions(db, run, pipeline, [PipelineState.REVIEW_REQUIRED])
    db.flush()

    assert service.settle_finished(db) == 1
    db.refresh(run)
    assert run.production_status == PARTIAL


# ===========================================================================
# Read model
# ===========================================================================


def test_the_list_shows_every_cycle_including_the_empty_ones(client, db, pipeline):
    service = AutomationRunService()
    service.record(db, pipeline_id=pipeline.id, report=report(discovered=4), trigger="tick")
    service.record(
        db, pipeline_id=pipeline.id,
        report=report(status="skipped", skip_reason="not_due"), trigger="tick",
    )
    db.commit()

    body = client.get(f"/admin/pipelines/{pipeline.id}/runs").json()

    assert len(body) == 2
    assert {row["status"] for row in body} == {"completed", "skipped"}
    assert any(row["skip_reason"] == "not_due" for row in body)


def test_the_detail_carries_the_stages_and_the_productions(client, db, pipeline):
    service = AutomationRunService()
    run = service.record(
        db, pipeline_id=pipeline.id, report=report(admitted=1), trigger="tick"
    )
    attach_productions(db, run, pipeline, [PipelineState.RENDERING])
    db.commit()

    body = client.get(f"/admin/pipelines/{pipeline.id}/runs/{run.id}").json()

    assert set(body["stages"]) == {"discovery", "selection", "admission", "publication"}
    assert len(body["productions"]) == 1
    assert body["productions"][0]["state"] == "rendering"


def test_a_run_from_another_pipeline_is_a_404(client, db, pipeline):
    other = Pipeline(name="Premier League", keywords_json=[], is_active=True)
    db.add(other)
    db.flush()
    run = AutomationRunService().record(
        db, pipeline_id=pipeline.id, report=report(), trigger="tick"
    )
    db.commit()

    assert client.get(f"/admin/pipelines/{other.id}/runs/{run.id}").status_code == 404


def test_run_routes_require_the_operator(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        assert anonymous.get(
            f"/admin/pipelines/{uuid.uuid4()}/runs"
        ).status_code in (401, 403)


# ===========================================================================
# Survival
# ===========================================================================


def test_a_run_outlives_the_pipeline_it_belonged_to(db, pipeline):
    """Ele descreve trabalho que realmente aconteceu.

    Apagar a configuração não pode apagar o registro de que os cortes foram feitos.
    """
    run = AutomationRunService().record(
        db, pipeline_id=pipeline.id, report=report(discovered=3), trigger="tick"
    )
    db.commit()
    run_id = run.id

    db.delete(pipeline)
    db.commit()

    survivor = db.query(AutomationRun).filter(AutomationRun.id == run_id).first()
    assert survivor is not None
    assert survivor.pipeline_id is None
    assert survivor.discovered == 3
