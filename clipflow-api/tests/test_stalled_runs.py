"""Uma run que para de andar é resgatada.

O banco provou o modo de falha antes de existir teste: uma run ficou em `WAITING_AI` por mais
de uma hora porque o worker deu ack no job e foi embora. Nada a procurava — `job_leases` é
escrito por um endpoint que o worker não usa e nunca é lido, e o `STALE_RUN_MINUTES` do
agendador cobre o ciclo, não a run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.settings import settings
from app.models.enums import PipelineState
from app.models.pipeline_event import PipelineEvent
from app.services.stalled_run_service import FAILED, REQUEUED, StalledRunService
from tests.conftest import make_run

NOW = datetime.now(timezone.utc)


def _age(db, job, minutes, *, with_event=False):
    """Envelhece a run: é o último sinal de vida que conta, não a idade dela."""
    old = NOW - timedelta(minutes=minutes)
    job.updated_at = old
    if with_event:
        event = PipelineEvent(
            pipeline_job_id=job.id,
            event_type="INFO",
            service="worker",
            message="ultimo sinal",
        )
        db.add(event)
        db.flush()
        event.created_at = old
    db.commit()


@pytest.fixture()
def service():
    published = []
    yield StalledRunService(republish=lambda db, job: published.append(str(job.id))), published


def test_a_run_that_stopped_moving_goes_back_to_the_queue(db, service):
    svc, published = service
    job = make_run(db, state=PipelineState.TRANSCRIBING)
    _age(db, job, settings.stalled_run_timeout_minutes + 10)

    rescued = svc.rescue(db, now=NOW)

    assert [r.outcome for r in rescued] == [REQUEUED]
    assert job.state == PipelineState.QUEUED
    # Voltar o estado sem devolver o payload deixaria a run "na fila" sem estar em fila
    # nenhuma — parada de novo, agora mentindo sobre isso.
    assert published == [str(job.id)]


def test_a_run_that_is_working_is_left_alone(db, service):
    """O corte é sobre progresso. Transcrever um vídeo longo em CPU é lento e legítimo."""
    svc, _ = service
    job = make_run(db, state=PipelineState.TRANSCRIBING)
    _age(db, job, 600, with_event=False)
    # Um evento recente: a run deu sinal agora mesmo, por mais velha que seja.
    db.add(
        PipelineEvent(
            pipeline_job_id=job.id, event_type="INFO", service="worker", message="vivo"
        )
    )
    db.commit()

    assert svc.rescue(db, now=NOW) == []
    assert job.state == PipelineState.TRANSCRIBING


def test_work_resting_on_purpose_is_not_rescued(db, service):
    """`READY_TO_PUBLISH` espera o publicador e `REVIEW_REQUIRED` espera uma pessoa. Os dois
    estão parados de propósito, e resgatá-los reenviaria para render o que já foi renderizado."""
    svc, _ = service
    for state in (PipelineState.READY_TO_PUBLISH, PipelineState.REVIEW_REQUIRED):
        job = make_run(db, state=state)
        _age(db, job, settings.stalled_run_timeout_minutes + 60)

    assert svc.rescue(db, now=NOW) == []


def test_a_run_that_keeps_stalling_is_declared_failed(db, service):
    """Sem um teto, uma run que trava sempre no mesmo ponto recircula para sempre."""
    svc, published = service
    job = make_run(db, state=PipelineState.RENDERING)
    job.retry_count = settings.stalled_run_max_attempts
    _age(db, job, settings.stalled_run_timeout_minutes + 5)

    rescued = svc.rescue(db, now=NOW)

    assert [r.outcome for r in rescued] == [FAILED]
    assert job.state == PipelineState.FAILED
    assert published == []
    # O motivo fica escrito: um número parado na tela sem explicação é o que havia antes.
    assert "nao voltou a andar" in (job.error_message or "")


def test_the_sweep_commits_its_own_work(db, service):
    """Roda no topo do tick, antes de qualquer outra etapa. Sem commit próprio, o resgate
    some justamente nos ticks em que ele foi a única coisa que aconteceu."""
    svc, _ = service
    job = make_run(db, state=PipelineState.DOWNLOADING)
    _age(db, job, settings.stalled_run_timeout_minutes + 1)

    svc.rescue(db, now=NOW)
    db.rollback()

    assert job.state == PipelineState.QUEUED
