"""Resgata a run que parou de andar.

Nada tirava um job de um estado intermediário. Um worker que morre no meio de um render, um
processo morto pelo OOM killer, uma máquina reiniciada — a linha ficava em `RENDERING` para
sempre, sem alarme e sem nova tentativa, e o operador só descobria abrindo a tela e reparando
que um número não mudava.

O banco provou o modo de falha: uma run ficou parada em `WAITING_AI` por horas porque o
worker deu ack no job e foi embora. Ninguém a procurou.

**Progresso, não idade.** O corte é sobre quando a run andou pela última vez, não sobre quando
começou: uma transcrição de vídeo longo em CPU leva bastante tempo, e cancelar por duração
mataria exatamente os jobs mais caros. Cada evento da run renova o relógio, então uma run que
está trabalhando nunca é tocada, por mais devagar que vá.

**Devolver à fila, não declarar morta.** A maioria das paradas é o processo, não o trabalho —
e o trabalho já foi pago em download e transcrição. Só depois de esgotar as tentativas a run
é declarada falha, com o motivo escrito, para não virar mais um número parado na tela.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.enums import PipelineState
from app.models.pipeline_event import PipelineEvent
from app.models.pipeline_job import PipelineJob
from app.services.pipeline_state_machine import (
    COMPLETION_STATES,
    TERMINAL_STATES,
    PipelineStateMachine,
)

logger = logging.getLogger(__name__)

# Onde uma run pode estar sem que ninguém esteja trabalhando nela. Os estados de repouso e os
# terminais ficam de fora: `READY_TO_PUBLISH` espera o publicador e `REVIEW_REQUIRED` espera
# uma pessoa — parados de propósito, não travados.
_AT_REST = TERMINAL_STATES | COMPLETION_STATES | {PipelineState.FAILED}
IN_FLIGHT_STATES = tuple(state for state in PipelineState if state not in _AT_REST)

REQUEUED = "requeued"
FAILED = "failed"


@dataclass(frozen=True)
class Rescue:
    pipeline_job_id: str
    state: str
    stalled_for_minutes: int
    attempt: int
    outcome: str

    def as_dict(self) -> dict:
        return {
            "pipeline_job_id": self.pipeline_job_id,
            "state": self.state,
            "stalled_for_minutes": self.stalled_for_minutes,
            "attempt": self.attempt,
            "outcome": self.outcome,
        }


class StalledRunService:
    def __init__(
        self,
        state_machine: PipelineStateMachine | None = None,
        republish: Callable[[Session, PipelineJob], None] | None = None,
    ) -> None:
        self.states = state_machine or PipelineStateMachine()
        # Injetável porque enfileirar é I/O: o teste prova a decisão sem um Redis.
        self._republish = republish

    def rescue(
        self,
        db: Session,
        *,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[Rescue]:
        """Uma passada. Nunca levanta: isto roda dentro do tick do agendador."""
        now = now or datetime.now(timezone.utc)
        try:
            return self._rescue(db, now=now, limit=limit)
        except Exception:  # noqa: BLE001
            logger.exception("stalled_run_sweep_crashed")
            db.rollback()
            return []

    # ------------------------------------------------------------------ internals

    def _rescue(self, db: Session, *, now: datetime, limit: int) -> list[Rescue]:
        cutoff = now - timedelta(minutes=settings.stalled_run_timeout_minutes)
        rescued: list[Rescue] = []

        for job, last_seen in self._stalled(db, cutoff=cutoff, limit=limit):
            minutes = int((now - last_seen).total_seconds() // 60)
            attempt = (job.retry_count or 0) + 1

            if attempt > settings.stalled_run_max_attempts:
                self.states.fail(
                    db,
                    job,
                    error_type="stalled",
                    error_message=(
                        f"A run parou em {job.state.value} e nao voltou a andar "
                        f"por {minutes} minutos, em {attempt - 1} tentativas."
                    ),
                    service="scheduler",
                    commit=False,
                )
                outcome = FAILED
            else:
                # `requeue`, não `report`: voltar para a fila é um comando, e o guarda de
                # relatório atrasado recusaria o movimento para trás — que aqui é a verdade.
                self.states.requeue(
                    db,
                    job,
                    reason=f"parada por {minutes} min em {job.state.value}",
                    service="scheduler",
                    commit=False,
                )
                if self._republish is not None:
                    self._republish(db, job)
                outcome = REQUEUED

            rescued.append(
                Rescue(
                    pipeline_job_id=str(job.id),
                    state=job.state.value,
                    stalled_for_minutes=minutes,
                    attempt=attempt,
                    outcome=outcome,
                )
            )

        if rescued:
            # Commit próprio: isto roda no topo do tick, antes de qualquer outro trabalho, e
            # um resgate que depende do commit de outra etapa some nos ticks em que ele é a
            # única coisa que aconteceu.
            db.commit()
            logger.info("stalled_runs_rescued", extra={"count": len(rescued)})
        return rescued

    def _stalled(self, db: Session, *, cutoff: datetime, limit: int):
        """As runs em voo cujo último sinal de vida é anterior ao corte.

        O sinal é o evento mais recente da run; sem nenhum, é o `updated_at` dela. Uma run
        recém-criada tem `updated_at` de agora, então nunca entra aqui por engano.
        """
        last_event = (
            db.query(
                PipelineEvent.pipeline_job_id.label("job_id"),
                func.max(PipelineEvent.created_at).label("last_seen"),
            )
            .group_by(PipelineEvent.pipeline_job_id)
            .subquery()
        )
        rows = (
            db.query(PipelineJob, last_event.c.last_seen)
            .outerjoin(last_event, last_event.c.job_id == PipelineJob.id)
            .filter(PipelineJob.state.in_(IN_FLIGHT_STATES))
            .filter(func.coalesce(last_event.c.last_seen, PipelineJob.updated_at) < cutoff)
            .order_by(PipelineJob.updated_at.asc())
            .limit(limit)
            .all()
        )
        out = []
        for job, last_seen in rows:
            seen = last_seen or job.updated_at
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            out.append((job, seen))
        return out
