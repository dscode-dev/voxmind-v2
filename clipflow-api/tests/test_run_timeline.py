"""A run diz em que passo está, o que já passou e onde falhou.

A tela de uma produção mostrava um componente de etapas sempre apagado, como se nada
estivesse acontecendo — enquanto o worker transcrevia o vídeo. Apagado porque não havia o que
acender: o detalhe da run devolvia só o estado atual, e os passos, que sempre estiveram em
`pipeline_events` com início, fim e payload, não eram lidos por ninguém.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import api_router
from app.db.session import get_db
from app.models.enums import (
    AssetStatus,
    ClipAssetType,
    PipelineState,
    UserRole,
    UserStatus,
)
from app.models.clip_asset import ClipAsset
from app.models.pipeline_event import PipelineEvent
from app.models.user import User
from app.security.auth_middleware import get_current_admin
from app.services.run_timeline_service import DONE, FAILED, RUNNING, RunTimelineService
from tests.conftest import make_clip_job, make_run

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def admin_user(db):
    user = User(
        phone_number="+5511977776666",
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



@pytest.fixture()
def empty_storage(monkeypatch):
    """Sem isto o teste sai para a rede procurar um bucket que não existe no CI."""
    from app.api import operations_read

    monkeypatch.setattr(
        operations_read.artifact_storage_client,
        "list_objects",
        lambda bucket, prefix, recursive: [],
    )


@pytest.fixture()
def service():
    return RunTimelineService()


def _step(db, job, stage, status, *, at, **payload):
    event = PipelineEvent(
        pipeline_job_id=job.id,
        event_type="INFO",
        service="worker",
        message=f"{stage}:{status}",
        payload_json={"stage": stage, "step_status": status, "attempt": 1, **payload},
    )
    db.add(event)
    db.flush()
    event.created_at = at
    db.flush()
    return event


def _phase(timeline, key):
    return next(p for p in timeline["phases"] if p["key"] == key)


# ===========================================================================
# O que está acontecendo agora
# ===========================================================================


def test_a_step_that_started_and_has_not_finished_is_running(db, service):
    """O caso que a tela pintava de apagado: o worker transcrevendo há dez minutos."""
    job = make_run(db, state=PipelineState.TRANSCRIBING)
    _step(db, job, "download_video", "started", at=NOW - timedelta(minutes=12))
    _step(db, job, "download_video", "completed", at=NOW - timedelta(minutes=11))
    _step(db, job, "transcribe", "started", at=NOW - timedelta(minutes=10))
    db.commit()

    timeline = service.timeline(db, job)

    assert _phase(timeline, "download")["status"] == DONE
    assert _phase(timeline, "transcribe")["status"] == RUNNING
    assert _phase(timeline, "render")["status"] == "pending"


def test_a_finished_step_carries_how_long_it_took(db, service):
    job = make_run(db, state=PipelineState.TRANSCRIBING)
    _step(db, job, "download_video", "started", at=NOW - timedelta(seconds=30))
    _step(db, job, "download_video", "completed", at=NOW)
    db.commit()

    step = _phase(service.timeline(db, job), "download")["steps"][0]

    assert step["status"] == DONE
    assert step["duration_ms"] == 30_000


def test_a_phase_only_reports_an_end_once_it_has_ended(db, service):
    """Um fim parcial faria uma fase ainda em curso parecer encerrada."""
    job = make_run(db, state=PipelineState.ANALYZING)
    _step(db, job, "chunk", "started", at=NOW - timedelta(minutes=2))
    _step(db, job, "chunk", "completed", at=NOW - timedelta(minutes=1))
    _step(db, job, "candidate_build", "started", at=NOW)
    db.commit()

    phase = _phase(service.timeline(db, job), "analyze")

    assert phase["status"] == RUNNING
    assert phase["finished_at"] is None


# ===========================================================================
# O que deu errado
# ===========================================================================


def test_a_failed_step_says_which_one_and_why(db, service):
    job = make_run(db, state=PipelineState.RENDERING)
    _step(db, job, "render_cuts", "started", at=NOW - timedelta(minutes=1))
    _step(db, job, "render_cuts", "failed", at=NOW, error="ffmpeg saiu com 1")
    db.commit()

    phase = _phase(service.timeline(db, job), "render")

    assert phase["status"] == FAILED
    assert phase["steps"][0]["error"]


def test_a_run_that_died_mid_step_does_not_stay_running_for_ever(db, service):
    """Um worker morto no meio do render deixaria a fase "executando" para sempre — que é a
    tela apagada outra vez, só que em roxo."""
    job = make_run(db, state=PipelineState.FAILED)
    job.error_message = "worker perdeu o lease"
    _step(db, job, "render_cuts", "started", at=NOW - timedelta(minutes=30))
    db.commit()

    phase = _phase(service.timeline(db, job), "render")

    assert phase["status"] == FAILED
    assert phase["steps"][0]["error"] == "worker perdeu o lease"


# ===========================================================================
# O detalhe de cada passo
# ===========================================================================


def test_opening_a_step_shows_what_the_worker_reported(db, service):
    """É o que se vê ao clicar num passo. Sem isto, a etapa é só uma bolinha colorida."""
    job = make_run(db, state=PipelineState.ANALYZING)
    _step(db, job, "candidate_score", "started", at=NOW - timedelta(seconds=10))
    _step(db, job, "candidate_score", "completed", at=NOW, ranked_count=42)
    db.commit()

    step = _phase(service.timeline(db, job), "analyze")["steps"][0]

    assert step["detail"]["ranked_count"] == 42


def test_the_ai_call_is_reported_with_provider_and_latency(db, service):
    """O que explica um corte ter saído como saiu."""
    job = make_run(db, state=PipelineState.AI_COMPLETED)
    event = PipelineEvent(
        pipeline_job_id=job.id,
        event_type="INFO",
        service="ai",
        message="AI_REQUEST_FINISHED",
        payload_json={
            "ai_event": "AI_REQUEST_FINISHED",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "latency_ms": 6703,
        },
    )
    db.add(event)
    db.commit()

    calls = service.timeline(db, job)["ai_calls"]

    assert calls[0]["provider"] == "openai"
    assert calls[0]["latency_ms"] == 6703


def test_a_step_the_table_does_not_know_is_still_shown(db, service):
    """Um passo novo não pode ficar invisível só porque a tabela de fases não foi
    atualizada junto."""
    job = make_run(db, state=PipelineState.ANALYZING)
    _step(db, job, "passo_inventado", "started", at=NOW)
    db.commit()

    outros = _phase(service.timeline(db, job), "other")

    assert [s["step"] for s in outros["steps"]] == ["passo_inventado"]


def test_structural_steps_are_not_counted_twice(db, service):
    """`pipeline` e `prepare` envolvem os outros; mostrá-los como etapa contaria duas vezes
    a mesma coisa."""
    job = make_run(db, state=PipelineState.DOWNLOADING)
    _step(db, job, "pipeline", "started", at=NOW)
    _step(db, job, "prepare", "started", at=NOW)
    db.commit()

    timeline = service.timeline(db, job)

    assert all(p["steps"] == [] for p in timeline["phases"])


# ===========================================================================
# O vídeo no fim
# ===========================================================================


def test_the_finished_clips_come_with_a_link_to_download(client, db):
    """O resultado do trabalho todo ficava só no storage, alcançável por quem soubesse montar
    a URL."""
    worker_job = make_clip_job(db)
    run = make_run(db, worker_job_id=str(worker_job.id), state=PipelineState.READY_TO_PUBLISH)
    db.add(
        ClipAsset(
            job_id=worker_job.id,
            asset_type=ClipAssetType.SHORT_CLIP,
            status=AssetStatus.READY,
            order_index=1,
            storage_key=f"jobs/{worker_job.id}/final_clips/final_clip_01.mp4",
            title="Gol de bicicleta",
            start_sec=0,
            end_sec=45,
            duration_sec=45,
        )
    )
    db.commit()

    outputs = client.get(f"/admin/pipeline-jobs/{run.id}/timeline").json()["outputs"]

    assert len(outputs) == 1
    assert outputs[0]["name"] == "final_clip_01.mp4"
    assert outputs[0]["title"] == "Gol de bicicleta"
    assert "X-Amz-Signature" in (outputs[0]["url"] or "")


def test_a_run_with_nothing_rendered_yet_offers_nothing(client, db, empty_storage):
    run = make_run(db, state=PipelineState.TRANSCRIBING)
    db.commit()

    assert client.get(f"/admin/pipeline-jobs/{run.id}/timeline").json()["outputs"] == []


def test_an_autonomous_run_offers_what_is_in_storage(client, db, monkeypatch):
    """Uma run criada pela automação não passa por `clip_jobs`, então não tem linha de
    asset — mas os arquivos renderizados estão lá do mesmo jeito. Sem esta queda, justamente
    as produções autônomas seriam as únicas sem link para baixar."""
    from app.api import operations_read

    run = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    db.commit()

    class _Obj:
        def __init__(self, name):
            self.object_name = name
            self.size = 42_000_000

    monkeypatch.setattr(
        operations_read.artifact_storage_client,
        "list_objects",
        lambda bucket, prefix, recursive: [
            _Obj(prefix + "final_clip_02.mp4"),
            _Obj(prefix + "final_clip_01.mp4"),
            _Obj(prefix + "notas.txt"),
        ],
    )

    outputs = client.get(f"/admin/pipeline-jobs/{run.id}/timeline").json()["outputs"]

    # Ordenados, e só o que é vídeo.
    assert [o["name"] for o in outputs] == ["final_clip_01.mp4", "final_clip_02.mp4"]
    assert all("X-Amz-Signature" in (o["url"] or "") for o in outputs)


def test_a_blind_storage_does_not_take_the_screen_down(client, db, monkeypatch):
    from app.api import operations_read

    run = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    db.commit()

    def explode(*args, **kwargs):
        raise RuntimeError("minio fora")

    monkeypatch.setattr(operations_read.artifact_storage_client, "list_objects", explode)

    response = client.get(f"/admin/pipeline-jobs/{run.id}/timeline")

    assert response.status_code == 200
    assert response.json()["outputs"] == []


def test_the_timeline_requires_the_operator(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    run = make_run(db)
    db.commit()
    with TestClient(app) as anonymous:
        response = anonymous.get(f"/admin/pipeline-jobs/{run.id}/timeline")
        assert response.status_code in (401, 403)

# ===========================================================================
# O relato de fim não é uma transição
# ===========================================================================


def test_the_end_of_a_step_is_recorded_without_moving_the_state(db, monkeypatch, no_event_fanout):
    """O worker passou a relatar o fim de cada passo, porque sem isso a tela ficava com
    todas as fases girando para sempre. Quem recusa mexer no estado é este lado.

    Transicionar de novo para o mesmo alvo seria ruído; pior, um passo cujo estado já ficou
    para trás teria o fim recusado como relatório atrasado, e o tempo dele se perderia.
    """
    from fastapi.testclient import TestClient

    from app.api.router import api_router
    from app.core.settings import settings
    from app.db.session import get_db
    from app.models.pipeline_event import PipelineEvent

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db

    run = make_run(db, state=PipelineState.TRANSCRIBING)
    db.commit()
    headers = {"X-Internal-Token": settings.internal_api_token}

    with TestClient(app) as worker:
        response = worker.post(
            f"/internal/pipeline-runs/{run.id}/stage",
            json={"stage": "transcribe", "status": "completed"},
            headers=headers,
        )

    assert response.status_code == 200
    # O estado não andou...
    db.refresh(run)
    assert run.state == PipelineState.TRANSCRIBING
    # ...e mesmo assim o fim ficou registrado, que é o que a linha do tempo lê.
    events = (
        db.query(PipelineEvent)
        .filter(PipelineEvent.pipeline_job_id == run.id)
        .all()
    )
    assert any(
        (e.payload_json or {}).get("step_status") == "completed" for e in events
    )

def test_a_step_that_does_not_move_the_state_is_still_recorded(db, no_event_fanout):
    """`download_video` começa logo depois de o claim já ter movido a run para DOWNLOADING.

    A transição vira um não-evento, e a máquina de estados — corretamente — não grava nada.
    Mas o passo aconteceu: sem este registro ele aparecia na tela sem início e, portanto,
    sem quanto tempo levou.
    """
    from fastapi.testclient import TestClient

    from app.api.router import api_router
    from app.core.settings import settings
    from app.db.session import get_db
    from app.models.pipeline_event import PipelineEvent

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db

    run = make_run(db, state=PipelineState.DOWNLOADING)
    db.commit()

    with TestClient(app) as worker:
        worker.post(
            f"/internal/pipeline-runs/{run.id}/stage",
            json={"stage": "download_video", "status": "started"},
            headers={"X-Internal-Token": settings.internal_api_token},
        )

    recorded = [
        e
        for e in db.query(PipelineEvent).filter(
            PipelineEvent.pipeline_job_id == run.id
        )
        if (e.payload_json or {}).get("stage") == "download_video"
    ]
    assert recorded, "o passo sumiu porque a transição era duplicada"

def test_a_finished_run_has_nothing_still_running(db, service):
    """Uma run concluída aparecia com as sete fases girando — a tela apagada de novo, só
    que em roxo.

    Acontece com toda run anterior ao worker passar a relatar o fim de cada passo, e
    aconteceria de novo em qualquer passo cujo fim se perdesse no caminho.
    """
    job = make_run(db, state=PipelineState.READY_TO_PUBLISH)
    _step(db, job, "transcribe", "started", at=NOW - timedelta(minutes=5))
    _step(db, job, "render_cuts", "started", at=NOW - timedelta(minutes=2))
    db.commit()

    timeline = service.timeline(db, job)

    assert all(p["status"] != RUNNING for p in timeline["phases"])
    assert _phase(timeline, "render")["status"] == DONE


def test_a_failed_run_marks_where_it_stopped_and_closes_the_rest(db, service):
    """A primeira fase aberta é onde ela parou. As anteriores tinham terminado; o fim delas
    é que não chegou a ser relatado."""
    job = make_run(db, state=PipelineState.FAILED)
    job.error_message = "ffmpeg saiu com 1"
    _step(db, job, "transcribe", "started", at=NOW - timedelta(minutes=5))
    _step(db, job, "render_cuts", "started", at=NOW - timedelta(minutes=2))
    db.commit()

    timeline = service.timeline(db, job)

    assert _phase(timeline, "transcribe")["status"] == FAILED
    assert _phase(timeline, "render")["status"] == DONE
