"""O painel: o negócio está andando, e o que saiu daqui funcionou.

Duas perguntas, e até agora a tela não respondia nenhuma. Ela fazia dez chamadas e costurava
números no navegador — saúde de processo, profundidade de fila, contagem de heartbeat. Tudo
verdadeiro e nada sobre o produto: um operador olhava aquilo e não sabia dizer se o sistema
tinha cortado um vídeo naquele dia.

**Uma consulta por pergunta, no servidor.** Somar no navegador significa que cada tela inventa
o próprio total e eles divergem do banco. Os números daqui saem das mesmas tabelas que a
auditoria lê.

**O funil é de coortes, não de estoque.** "12 encontrados, 3 publicados" só quer dizer alguma
coisa se os 3 vieram dos 12. Cada etapa conta o que aconteceu na janela, e a tela diz que é
uma janela — um funil que mistura o estoque histórico com o fluxo da semana produz taxas de
conversão que não descrevem nada.

**Nunca medido não é zero.** Um vídeo que a coleta ainda não visitou volta com `null`, e a
tela mostra `—`. Arredondar isso para zero diria que o corte fracassou quando ninguém olhou.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.automation_run import AutomationRun
from app.models.enums import PipelineState, PublishAttemptStatus
from app.models.pipeline import Pipeline
from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt
from app.publishing.publish_queue import PublishQueue
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.models.video_performance_snapshot import VideoPerformanceSnapshot
from app.security.auth_middleware import get_current_admin
from app.services.pipeline_readiness_service import PipelineReadinessService

router = APIRouter()
readiness_service = PipelineReadinessService()

# Estados em que uma produção ainda está se movendo. O que não está aqui, ou terminou, ou
# está esperando uma pessoa.
IN_FLIGHT = (
    PipelineState.QUEUED,
    PipelineState.DOWNLOADING,
    PipelineState.DOWNLOADED,
    PipelineState.TRANSCRIBING,
    PipelineState.TRANSCRIBED,
    PipelineState.ANALYZING,
    PipelineState.PROMPT_BUILDING,
    PipelineState.WAITING_AI,
    PipelineState.AI_COMPLETED,
    PipelineState.RENDERING,
    PipelineState.RENDERED,
    PipelineState.PUBLISHING,
)

# O nome de cada estado para quem não conhece o schema. O painel fala de trabalho, não de
# enum — "Transcrevendo" diz o que está acontecendo; `TRANSCRIBING` diz onde olhar no código.
STAGE_LABEL = {
    PipelineState.QUEUED: "Na fila",
    PipelineState.DOWNLOADING: "Baixando",
    PipelineState.DOWNLOADED: "Baixado",
    PipelineState.TRANSCRIBING: "Transcrevendo",
    PipelineState.TRANSCRIBED: "Transcrito",
    PipelineState.ANALYZING: "Analisando",
    PipelineState.PROMPT_BUILDING: "Preparando cortes",
    PipelineState.WAITING_AI: "Aguardando IA",
    PipelineState.AI_COMPLETED: "IA concluída",
    PipelineState.RENDERING: "Renderizando",
    PipelineState.RENDERED: "Renderizado",
    PipelineState.PUBLISHING: "Publicando",
    PipelineState.REVIEW_REQUIRED: "Requer revisão",
    PipelineState.READY_TO_PUBLISH: "Pronto para publicar",
}


@router.get("/admin/dashboard")
def dashboard(
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return {
        "window_days": days,
        "since": since.isoformat(),
        "flow": _flow(db, since),
        "bottleneck": _bottleneck(db),
        "cycles": _cycles(db, since),
        "attention": _attention(db),
        "performance": _performance(db, since),
    }


# =============================================================================
# O fluxo
# =============================================================================


def _flow(db: Session, since: datetime) -> dict[str, Any]:
    """Quantos vídeos entraram, quantos viraram corte, quantos saíram — na janela.

    Cada etapa é contada pela hora em que ela *aconteceu*, não pelo estado atual da linha: um
    vídeo encontrado na semana passada e publicado hoje conta uma vez em cada etapa, na semana
    certa. Contar pelo estado atual faria a etapa anterior encolher toda vez que algo avança.
    """
    found = (
        db.query(func.count(VideoCandidate.id))
        .filter(VideoCandidate.created_at >= since)
        .scalar()
    ) or 0
    selected = (
        db.query(func.count(VideoCandidate.id))
        .filter(VideoCandidate.selected_at.isnot(None), VideoCandidate.selected_at >= since)
        .scalar()
    ) or 0
    admitted = (
        db.query(func.count(PipelineJob.id))
        .filter(PipelineJob.created_at >= since)
        .scalar()
    ) or 0
    published = (
        db.query(func.count(PublishAttempt.id))
        .filter(
            PublishAttempt.status == PublishAttemptStatus.SUCCEEDED,
            PublishAttempt.finished_at.isnot(None),
            PublishAttempt.finished_at >= since,
        )
        .scalar()
    ) or 0

    return {
        "found": int(found),
        "selected": int(selected),
        "admitted": int(admitted),
        "published": int(published),
        # A taxa só é honesta quando há de onde converter. Sem encontrados ela é `null`, e a
        # tela mostra `—` em vez de 0%, que leria como "tudo falhou".
        "selection_rate": _rate(selected, found),
        "publish_rate": _rate(published, admitted),
    }


def _bottleneck(db: Session) -> dict[str, Any]:
    """Onde o trabalho está parado agora, e qual etapa segura mais.

    Estoque, não janela: a pergunta aqui é "o que está travado neste instante", e o passado
    não trava nada.
    """
    rows = (
        db.query(PipelineJob.state, func.count(PipelineJob.id))
        .filter(PipelineJob.state.in_(IN_FLIGHT))
        .group_by(PipelineJob.state)
        .all()
    )
    stages = [
        {
            "state": state.value if state else None,
            "label": STAGE_LABEL.get(state, state.value if state else "—"),
            "count": int(count),
        }
        for state, count in rows
    ]
    stages.sort(key=lambda item: item["count"], reverse=True)

    waiting_person = (
        db.query(func.count(PipelineJob.id))
        .filter(
            PipelineJob.state.in_(
                (PipelineState.REVIEW_REQUIRED, PipelineState.READY_TO_PUBLISH)
            )
        )
        .scalar()
    ) or 0

    return {
        "in_flight": sum(item["count"] for item in stages),
        "stages": stages,
        # Separado do resto: estes não vão se mover sozinhos, e somá-los ao "em andamento"
        # faria a fila parecer viva quando ela está esperando alguém.
        "waiting_on_a_person": int(waiting_person),
        "worst": stages[0] if stages else None,
    }


def _cycles(db: Session, since: datetime) -> dict[str, Any]:
    """As execuções da janela: quantas rodaram, quantas acharam algo, quando foi a última."""
    rows = (
        db.query(AutomationRun.status, func.count(AutomationRun.id))
        .filter(AutomationRun.started_at >= since)
        .group_by(AutomationRun.status)
        .all()
    )
    by_status = {str(status): int(count) for status, count in rows}
    total = sum(by_status.values())

    productive = (
        db.query(func.count(AutomationRun.id))
        .filter(AutomationRun.started_at >= since, AutomationRun.admitted > 0)
        .scalar()
    ) or 0

    last = (
        db.query(AutomationRun).order_by(AutomationRun.started_at.desc()).first()
    )

    return {
        "total": total,
        "by_status": by_status,
        # Um ciclo que rodou e não admitiu nada é um ciclo que aconteceu. A distinção entre
        # "rodou 48 vezes" e "48 rodadas, 0 produtivas" é a diferença entre um sistema que
        # parece saudável e um que está girando em falso.
        "productive": int(productive),
        "last": (
            {
                "status": last.status,
                "trigger": last.trigger,
                "started_at": last.started_at,
                "discovered": last.discovered,
                "admitted": last.admitted,
            }
            if last
            else None
        ),
    }


def _attention(db: Session) -> dict[str, Any]:
    """O que precisa de uma pessoa, contado do mesmo lugar que as telas de detalhe."""
    pipelines = db.query(Pipeline).filter(Pipeline.is_active.is_(True)).all()
    shared = readiness_service.shared_facts()

    blocked = []
    for pipeline in pipelines:
        report = readiness_service.evaluate(db, pipeline, shared=shared)
        if report.blockers:
            blocked.append(
                {
                    "id": str(pipeline.id),
                    "name": pipeline.name,
                    "blocking": len(report.blockers),
                    "first": report.blockers[0].title,
                }
            )

    unresolved = (
        db.query(func.count(PublishAttempt.id))
        .filter(
            PublishAttempt.status.in_(
                (PublishAttemptStatus.UNKNOWN, PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION)
            )
        )
        .scalar()
    ) or 0

    return {
        "blocked_pipelines": blocked,
        "unresolved_publications": int(unresolved),
        "dead_letters": _dead_letters(),
        "pipelines_total": len(pipelines),
    }


def _dead_letters() -> int:
    """Publicações que a fila desistiu de entregar.

    Ficavam só no Redis, invisíveis em qualquer tela: havia 44 nesta instalação e ninguém
    tinha como saber. Um vídeo que não chega ao canal é o oposto do resultado, e um número
    que só existe num `redis-cli` não é observabilidade.
    """
    try:
        return int(PublishQueue().depths().get("dead", 0))
    except Exception:  # noqa: BLE001
        # O painel inteiro não pode cair porque o Redis piscou.
        return 0


# =============================================================================
# O desempenho
# =============================================================================


def _performance(db: Session, since: datetime) -> dict[str, Any]:
    """O que os cortes publicados fizeram no canal.

    Lê a última medição de cada vídeo. Um vídeo que a coleta nunca visitou não entra nos
    totais e é contado à parte — somá-lo como zero diria que ele fracassou quando ninguém
    olhou para ele.
    """
    published = (
        db.query(PublishAttempt)
        .filter(
            PublishAttempt.status == PublishAttemptStatus.SUCCEEDED,
            PublishAttempt.external_id.isnot(None),
        )
        .order_by(PublishAttempt.finished_at.desc().nullslast())
        .limit(200)
        .all()
    )
    if not published:
        return {
            "videos": 0, "measured": 0, "unmeasured": 0,
            "views": None, "likes": None, "avg_views": None, "top": [],
        }

    latest = _latest_snapshots(db, [attempt.id for attempt in published])

    views = likes = 0
    measured = 0
    rows = []
    for attempt in published:
        snapshot = latest.get(attempt.id)
        if snapshot is None or snapshot.view_count is None:
            continue
        measured += 1
        views += int(snapshot.view_count or 0)
        likes += int(snapshot.like_count or 0)
        rows.append(
            {
                "attempt_id": str(attempt.id),
                "external_id": attempt.external_id,
                "external_url": f"https://www.youtube.com/watch?v={attempt.external_id}",
                "title": attempt.media_identity,
                "published_at": attempt.finished_at,
                "views": int(snapshot.view_count),
                "likes": int(snapshot.like_count or 0),
            }
        )

    rows.sort(key=lambda row: row["views"], reverse=True)
    return {
        "videos": len(published),
        "measured": measured,
        # Dito, não escondido: sem isto "média de 0 views" seria lido como fracasso quando o
        # que aconteceu foi a coleta não ter rodado.
        "unmeasured": len(published) - measured,
        "views": views if measured else None,
        "likes": likes if measured else None,
        "avg_views": round(views / measured) if measured else None,
        "top": rows[:5],
    }


def _latest_snapshots(db: Session, attempt_ids: list) -> dict:
    if not attempt_ids:
        return {}
    newest = (
        db.query(
            VideoPerformanceSnapshot.publish_attempt_id.label("attempt_id"),
            func.max(VideoPerformanceSnapshot.captured_at).label("captured_at"),
        )
        .filter(VideoPerformanceSnapshot.publish_attempt_id.in_(attempt_ids))
        .group_by(VideoPerformanceSnapshot.publish_attempt_id)
        .subquery()
    )
    rows = (
        db.query(VideoPerformanceSnapshot)
        .join(
            newest,
            (VideoPerformanceSnapshot.publish_attempt_id == newest.c.attempt_id)
            & (VideoPerformanceSnapshot.captured_at == newest.c.captured_at),
        )
        .all()
    )
    return {row.publish_attempt_id: row for row in rows}


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None
