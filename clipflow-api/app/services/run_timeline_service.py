"""O que esta run fez, passo a passo.

A tela de uma produção mostrava um componente de etapas que ficava todo apagado, como se nada
estivesse acontecendo — e ficava apagado porque não havia o que acender: o endpoint da run
devolvia só o estado atual. Os passos existiam em `pipeline_events` desde sempre, com horário
de início, horário de fim e o payload de cada um. Ninguém os lia.

**Fases, e dentro delas os passos.** O worker reporta vinte e poucos passos técnicos —
`span_catalog`, `candidate_score`, `final_reel_subtitles` — e uma lista crua disso não responde
"em que pé está isso". As fases são o que alguém acompanha; os passos são o detalhe de quem
abre uma delas.

**Derivado, nunca gravado.** Nada aqui é uma segunda fonte de verdade: a linha do tempo é uma
leitura dos eventos que o worker já reportava. Uma tabela de progresso poderia divergir do que
de fato aconteceu, e aí a tela mentiria com mais confiança do que hoje.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.pipeline_event import PipelineEvent
from app.models.pipeline_job import PipelineJob

DONE = "done"
RUNNING = "running"
FAILED = "failed"
PENDING = "pending"

# As fases, na ordem em que acontecem, e os passos que pertencem a cada uma. Um passo que o
# worker reporte e não esteja aqui aparece em "Outros passos" em vez de sumir: um passo novo
# não deve ficar invisível só porque esta tabela não foi atualizada junto.
PHASES: list[tuple[str, str, tuple[str, ...]]] = [
    ("download", "Baixar o vídeo", ("download_video", "upload_video")),
    ("transcribe", "Transcrever", ("transcribe", "diarization")),
    (
        "analyze",
        "Analisar o conteúdo",
        (
            "chunk",
            "hook_detection",
            "audio_peak_detection",
            "story_shift_detection",
            "candidate_build",
            "candidate_score",
            "span_catalog",
        ),
    ),
    ("ai", "Decidir os cortes (IA)", ("prompt_build", "ai_request", "validate_ai_response")),
    (
        "render",
        "Renderizar",
        ("render_plan", "render_cuts", "final_reel_subtitles", "final_reel", "final_clips"),
    ),
    ("qa", "Conferir a qualidade", ("qa", "final_media_qa", "auto_review")),
    ("delivery", "Entregar", ("delivery_package", "publish_package", "send_cuts")),
]

STEP_LABEL: dict[str, str] = {
    "download_video": "Baixar do YouTube",
    "upload_video": "Guardar o original",
    "transcribe": "Transcrever a fala",
    "diarization": "Separar os locutores",
    "chunk": "Dividir em blocos",
    "hook_detection": "Procurar ganchos",
    "audio_peak_detection": "Achar picos de áudio",
    "story_shift_detection": "Achar viradas de narrativa",
    "candidate_build": "Montar candidatos a corte",
    "candidate_score": "Pontuar os candidatos",
    "span_catalog": "Catalogar os trechos",
    "prompt_build": "Montar o pedido para a IA",
    "ai_request": "Perguntar à IA quais cortes",
    "validate_ai_response": "Conferir a resposta da IA",
    "render_plan": "Planejar o render",
    "render_cuts": "Cortar os trechos",
    "final_reel_subtitles": "Gerar as legendas",
    "final_reel": "Montar o vídeo final",
    "final_clips": "Renderizar os cortes",
    "qa": "QA editorial",
    "final_media_qa": "QA do arquivo final",
    "auto_review": "Decidir se pode publicar",
    "delivery_package": "Empacotar a entrega",
    "publish_package": "Preparar a publicação",
    "send_cuts": "Enviar os cortes",
}

# Passos que o worker emite e que não são trabalho sobre o vídeo. Mostrá-los como etapa faria
# a tela contar duas vezes a mesma coisa.
_STRUCTURAL = {"pipeline", "prepare", "finalize"}

# Chaves do payload que descrevem o evento, não o passo. O resto vira detalhe.
_NOT_DETAIL = {"stage", "step_status", "attempt", "to", "from"}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


@dataclass
class Step:
    step: str
    label: str
    status: str = PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempt: int | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "label": self.label,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "duration_ms": self.duration_ms,
            "attempt": self.attempt,
            "error": self.error,
            "detail": self.detail,
        }


class RunTimelineService:
    def timeline(self, db: Session, job: PipelineJob) -> dict[str, Any]:
        events = (
            db.query(PipelineEvent)
            .filter(PipelineEvent.pipeline_job_id == job.id)
            .order_by(PipelineEvent.created_at.asc())
            .all()
        )

        steps: dict[str, Step] = {}
        order: list[str] = []
        ai_calls: list[dict[str, Any]] = []

        for event in events:
            payload = dict(event.payload_json or {})

            # As chamadas de IA vêm à parte, com provedor, modelo e latência. São o que
            # explica um corte ter saído como saiu, então ficam visíveis por si.
            if payload.get("ai_event"):
                ai_calls.append(
                    {
                        "event": payload.get("ai_event"),
                        "provider": payload.get("provider"),
                        "model": payload.get("model"),
                        "latency_ms": payload.get("latency_ms"),
                        "at": _iso(event.created_at),
                    }
                )
                continue

            name = payload.get("stage")
            status = payload.get("step_status")
            if not name or name in _STRUCTURAL:
                continue

            step = steps.get(name)
            if step is None:
                step = Step(step=name, label=STEP_LABEL.get(name, name.replace("_", " ")))
                steps[name] = step
                order.append(name)

            if payload.get("attempt") is not None:
                step.attempt = payload["attempt"]

            if status == "started":
                step.started_at = step.started_at or event.created_at
                if step.status == PENDING:
                    step.status = RUNNING
            elif status in ("completed", "skipped"):
                step.finished_at = event.created_at
                step.status = DONE
            elif status == "failed":
                step.finished_at = event.created_at
                step.status = FAILED
                step.error = event.message or payload.get("error")

            # O resto do payload é o detalhe do passo: contagens, provedor, o que o worker
            # achou útil dizer. Guardado inteiro, porque é o que alguém quer ver ao abri-lo.
            for key, value in payload.items():
                if key not in _NOT_DETAIL:
                    step.detail[key] = value

        return {
            "state": job.state.value if job.state else None,
            "started_at": _iso(job.started_at),
            "finished_at": _iso(job.finished_at),
            "error_message": job.error_message,
            "phases": self._phases(steps, order, job),
            "ai_calls": ai_calls,
        }

    # ------------------------------------------------------------------- fases

    def _phases(
        self, steps: dict[str, Step], order: list[str], job: PipelineJob
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        placed: set[str] = set()

        for key, label, names in PHASES:
            mine = [steps[n] for n in names if n in steps]
            placed.update(s.step for s in mine)
            out.append(self._phase(key, label, mine))

        extra = [steps[n] for n in order if n not in placed]
        if extra:
            out.append(self._phase("other", "Outros passos", extra))

        self._settle(out, job)
        return out

    @staticmethod
    def _phase(key: str, label: str, steps: list[Step]) -> dict[str, Any]:
        if not steps:
            status = PENDING
        elif any(s.status == FAILED for s in steps):
            status = FAILED
        elif any(s.status == RUNNING for s in steps):
            status = RUNNING
        elif all(s.status == DONE for s in steps):
            status = DONE
        else:
            status = RUNNING

        starts = [s.started_at for s in steps if s.started_at]
        ends = [s.finished_at for s in steps if s.finished_at]
        started = min(starts) if starts else None
        # Só declara fim quando a fase inteira terminou: um fim parcial faria uma fase ainda
        # em curso parecer encerrada.
        finished = max(ends) if ends and status == DONE else None
        duration = (
            max(0, int((finished - started).total_seconds() * 1000))
            if started and finished
            else None
        )

        return {
            "key": key,
            "label": label,
            "status": status,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "duration_ms": duration,
            "steps": [s.as_dict() for s in steps],
        }

    # Estados em que a run não está mais andando. Alcançado um deles, nenhum passo pode
    # estar "executando": o que começou, acabou.
    _AT_REST = frozenset(
        {"ready_to_publish", "review_required", "published", "canceled", "failed"}
    )

    @classmethod
    def _settle(cls, phases: list[dict[str, Any]], job: PipelineJob) -> None:
        """Fecha o que ficou aberto quando a run já terminou.

        Sem isto, uma run concluída aparecia com as sete fases girando — que é a tela
        apagada de novo, só que em roxo. Acontece com toda run anterior ao worker passar a
        relatar o fim de cada passo, e aconteceria de novo em qualquer passo cujo fim se
        perdesse no caminho.
        """
        state = job.state.value if job.state else ""
        if state not in cls._AT_REST:
            return

        failed = state == "failed"
        marked = False
        for phase in phases:
            if phase["status"] != RUNNING:
                continue
            # Numa run que falhou, a primeira fase aberta é onde ela parou; as demais
            # simplesmente não chegaram a terminar de reportar.
            outcome = FAILED if (failed and not marked) else DONE
            marked = marked or failed
            phase["status"] = outcome
            for step in phase["steps"]:
                if step["status"] == RUNNING:
                    step["status"] = outcome
                    if outcome == FAILED:
                        step["error"] = step["error"] or job.error_message
