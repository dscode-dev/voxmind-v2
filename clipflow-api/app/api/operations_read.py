"""Two read models the operations console could not assemble for itself.

Both exist because a question an operator asks every day had no endpoint behind it, and the
Studio was reduced to inferring the answer or not showing it at all.

**`GET /admin/ai/status`** — "is the AI working?". The configuration lives in settings and the
evidence lives in `ai_executions`, and neither was reachable. Note the two are different
claims: a key being present says the system is *configured*, and only a recorded call says it
*works*. Both are reported, separately.

**`GET /admin/pipeline-jobs`** — the production list. There was no admin endpoint that
enumerated runs at all, so the console derived them from the operations event feed and could
only ever show recently active ones. That was recorded as a blocker in PR-STUDIO-V1; this is
the minimal fix, not a general query surface.

Admin-only, read-only, and neither returns a secret: no API key, no token, no prompt, no
provider response body.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from minio import Minio
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.settings import settings
from app.db.session import get_db
from app.models.ai_execution import AIExecution
from app.models.enums import AIExecutionStatus, ClipAssetType, PipelineState
from app.models.clip_asset import ClipAsset
from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt
from app.models.user import User
from app.services.asset_url_service import AssetUrlService
from app.services.run_timeline_service import RunTimelineService
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter()

asset_url_service = AssetUrlService()
run_timeline_service = RunTimelineService()

# Para listar o que foi renderizado. O endpoint *interno*, porque quem lista é a API;
# a URL assinada é que aponta para o endereço que o navegador alcança.
artifact_storage_client = Minio(
    settings.minio_endpoint,
    access_key=settings.minio_access_key,
    secret_key=settings.minio_secret_key,
    secure=settings.minio_secure,
)

MAX_PAGE_SIZE = 100


# =============================================================================
# AI status
# =============================================================================


@router.get("/admin/ai/status")
def ai_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Whether the AI integration is configured, and whether it has actually worked.

    `configured` comes from settings; `last_execution` comes from recorded calls. A
    deployment can be configured and broken, and the difference is the whole reason an
    operator opens this — so the two are never collapsed into one green dot.

    The key itself is never returned, in any form. Not masked, not truncated: absent.
    """
    key = settings.resolve_openai_key()
    latest = (
        db.query(AIExecution)
        .order_by(AIExecution.created_at.desc())
        .first()
    )
    last_success = (
        db.query(AIExecution)
        .filter(AIExecution.status == AIExecutionStatus.SUCCEEDED)
        .order_by(AIExecution.created_at.desc())
        .first()
    )

    since = datetime.now(timezone.utc) - timedelta(days=7)
    counts = dict(
        db.query(AIExecution.status, func.count(AIExecution.id))
        .filter(AIExecution.created_at >= since.replace(tzinfo=None))
        .group_by(AIExecution.status)
        .all()
    )

    return {
        # Configuration: this deployment could call a provider.
        "configured": bool(key),
        "provider": "openai" if key else None,
        "model": settings.publication_metadata_model if key else None,
        "purpose": "publication_metadata",
        # Evidence: it has actually called one.
        "last_execution": _serialize_execution(latest),
        "last_success_at": _iso(last_success.created_at) if last_success else None,
        "executions_last_7d": {
            (state.value if hasattr(state, "value") else str(state)): int(count)
            for state, count in counts.items()
        },
    }


def _serialize_execution(execution: AIExecution | None) -> dict[str, Any] | None:
    if execution is None:
        return None
    return {
        "id": str(execution.id),
        "pipeline_job_id": (
            str(execution.pipeline_job_id) if execution.pipeline_job_id else None
        ),
        "provider": execution.provider,
        "model": execution.model,
        "purpose": execution.purpose,
        "status": execution.status.value if execution.status else None,
        "latency_ms": execution.latency_ms,
        # A provider error CODE or an exception class name, sanitised at the adapter. Never a
        # raw body: an authenticated API reflects the request, and the request carries the key.
        "error": execution.error_message,
        "created_at": _iso(execution.created_at),
    }


# =============================================================================
# Production runs
# =============================================================================


# States a run is actively moving through, as opposed to finished or waiting on a person.
ACTIVE_STATES = (
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


@router.get("/admin/pipeline-jobs")
def list_pipeline_jobs(
    state: PipelineState | None = Query(
        default=None, description="Filter to one state."
    ),
    active: bool = Query(
        default=False, description="Only runs currently moving through production."
    ),
    pipeline_id: uuid.UUID | None = Query(
        default=None, description="Only runs belonging to one pipeline."
    ),
    limit: int = Query(default=25, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Production runs, newest first.

    Bounded and ordered rather than filterable in general: this exists so a console can show
    what is being produced, and a query language would be a larger surface than the problem.
    """
    query = db.query(PipelineJob).options(
        joinedload(PipelineJob.candidate),
        joinedload(PipelineJob.pipeline),
    )
    if state is not None:
        query = query.filter(PipelineJob.state == state)
    elif active:
        query = query.filter(PipelineJob.state.in_(ACTIVE_STATES))

    # Recortar por pipeline aqui, e não no navegador. A tela de detalhe filtrava a própria
    # página de resultados, então uma produção que não coubesse nas 50 mais recentes do
    # sistema inteiro sumia da aba do pipeline que a criou — e o cartão ao lado continuava
    # contando certo, o que deixava a mesma tela afirmando "1 produção" e "nenhuma produção".
    if pipeline_id is not None:
        query = query.filter(PipelineJob.pipeline_id == pipeline_id)

    total = query.count()
    jobs = (
        query.order_by(PipelineJob.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    # One query for every listed run's publication counts, rather than one per row.
    counts = _attempt_counts(db, [job.id for job in jobs])

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            _serialize_job(job, counts.get(job.id, {})) for job in jobs
        ],
    }


@router.get("/admin/pipeline-jobs/{job_id}/timeline")
def get_pipeline_job_timeline(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Os passos desta run, e os arquivos que ela produziu.

    A tela mostrava um componente de etapas que ficava todo apagado — e ficava porque não
    havia o que acender: o detalhe da run devolvia só o estado atual. Os passos sempre
    estiveram em `pipeline_events`, com início, fim e payload; faltava lê-los.
    """
    job = db.query(PipelineJob).filter(PipelineJob.id == job_id).first()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown pipeline job"
        )

    payload = run_timeline_service.timeline(db, job)
    payload["outputs"] = _run_outputs(db, job)
    return payload



def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None

def _run_outputs(db: Session, job: PipelineJob) -> list[dict[str, Any]]:
    """Os cortes prontos, com link para baixar.

    O vídeo é o resultado do trabalho todo; ficava só no storage, alcançável por quem
    soubesse montar a URL. A run aponta para o job do worker, e é nele que os arquivos
    renderizados estão registrados.
    """
    # `worker_job_id` é texto na run e UUID na coluna do asset. Sem a coerção o SQLAlchemy
    # quebra no bind em vez de não encontrar nada — e um erro de tipo ao abrir a tela é bem
    # pior do que uma lista vazia.
    worker_job_id = _as_uuid(job.worker_job_id)
    if worker_job_id is None:
        return []
    assets = (
        db.query(ClipAsset)
        .filter(
            ClipAsset.job_id == worker_job_id,
            ClipAsset.asset_type == ClipAssetType.SHORT_CLIP,
        )
        .order_by(ClipAsset.order_index.asc())
        .all()
    )
    if assets:
        return [
            {
                "id": str(asset.id),
                "name": (asset.storage_key or "").rsplit("/", 1)[-1],
                "title": asset.title,
                "order": asset.order_index,
                "duration_sec": (
                    float(asset.duration_sec) if asset.duration_sec else None
                ),
                "status": asset.status.value if asset.status else None,
                "url": asset_url_service.build_signed_url(asset.storage_key),
            }
            for asset in assets
        ]

    # Uma run criada pela automação não passa por `clip_jobs`, então não há linha de asset
    # para ela — mas os arquivos renderizados estão no storage do mesmo jeito. Sem esta
    # queda, justamente as produções autônomas (que é o que o produto faz sozinho) seriam as
    # únicas sem link para baixar.
    return _outputs_from_storage(str(job.worker_job_id))


def _outputs_from_storage(worker_job_id: str) -> list[dict[str, Any]]:
    prefix = f"jobs/{worker_job_id}/final_clips/"
    try:
        objects = list(
            artifact_storage_client.list_objects(
                settings.worker_artifacts_bucket, prefix=prefix, recursive=True
            )
        )
    except Exception:  # noqa: BLE001
        # A tela inteira não pode cair porque o storage piscou.
        logger.warning("run_outputs_listing_failed", exc_info=True)
        return []

    out = []
    for index, obj in enumerate(sorted(objects, key=lambda o: o.object_name), start=1):
        name = obj.object_name.rsplit("/", 1)[-1]
        if not name.lower().endswith(".mp4"):
            continue
        out.append(
            {
                "id": obj.object_name,
                "name": name,
                "title": None,
                "order": index,
                "duration_sec": None,
                "size_bytes": obj.size,
                "status": "ready",
                "url": asset_url_service.build_signed_url(obj.object_name),
            }
        )
    return out


@router.get("/admin/pipeline-jobs/{job_id}")
def get_pipeline_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """One run, with the provenance a person needs to understand where it came from."""
    job = (
        db.query(PipelineJob)
        .options(joinedload(PipelineJob.candidate), joinedload(PipelineJob.pipeline))
        .filter(PipelineJob.id == job_id)
        .first()
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown pipeline job"
        )

    counts = _attempt_counts(db, [job.id]).get(job.id, {})
    payload = _serialize_job(job, counts, detail=True)

    metadata = job.metadata_json or {}
    payload["provenance"] = metadata.get("provenance") or {}
    payload["frozen_inputs"] = metadata.get("snapshot") or {}
    payload["publication_eligibility"] = metadata.get("publication_eligibility")
    payload["publication_summary"] = metadata.get("publication_summary") or {}
    # The editorial text generated for this run's clips, so the console can show what will
    # actually be sent without opening the artifact store.
    payload["editorial_metadata"] = metadata.get("editorial_metadata") or {}
    return payload


def _attempt_counts(db: Session, job_ids: list[Any]) -> dict[Any, dict[str, int]]:
    if not job_ids:
        return {}
    rows = (
        db.query(
            PublishAttempt.pipeline_job_id,
            PublishAttempt.status,
            func.count(PublishAttempt.id),
        )
        .filter(PublishAttempt.pipeline_job_id.in_(job_ids))
        .group_by(PublishAttempt.pipeline_job_id, PublishAttempt.status)
        .all()
    )
    grouped: dict[Any, dict[str, int]] = {}
    for job_id, attempt_status, count in rows:
        key = attempt_status.value if hasattr(attempt_status, "value") else str(attempt_status)
        grouped.setdefault(job_id, {})[key] = int(count)
    return grouped


def _serialize_job(
    job: PipelineJob, attempt_counts: dict[str, int], *, detail: bool = False
) -> dict[str, Any]:
    """An allow-list.

    The run's own `metadata_json` holds frozen inputs and provenance, and dumping it wholesale
    would publish whatever a future stage decides to put there. Detail adds named fields
    instead.
    """
    candidate: VideoCandidate | None = job.candidate
    summary = (job.metadata_json or {}).get("publication_summary") or {}

    payload = {
        "id": str(job.id),
        "worker_job_id": job.worker_job_id,
        "state": job.state.value if job.state else None,
        "pipeline_stage": job.pipeline_stage,
        "clip_mode": job.clip_mode,
        "video_ratio": job.video_ratio,
        "source_url": job.source_url,
        "retry_count": job.retry_count,
        "max_retries": job.max_retries,
        "created_at": _iso(job.created_at),
        "queued_at": _iso(job.queued_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        # What this run is ABOUT, so a console never has to show only an id.
        "title": (candidate.title if candidate else None) or job.source_url,
        "pipeline": {
            "id": str(job.pipeline_id) if job.pipeline_id else None,
            "name": job.pipeline.name if job.pipeline else None,
        },
        "candidate": (
            {
                "id": str(candidate.id),
                "title": candidate.title,
                "channel": candidate.channel,
                "url": candidate.url,
                "thumbnail_url": candidate.thumbnail_url,
                "duration_sec": candidate.duration_sec,
            }
            if candidate
            else None
        ),
        "publication": {
            "status": (job.metadata_json or {}).get("publication_status", "none"),
            "required": summary.get("required", 0),
            "succeeded": summary.get("succeeded", 0),
            "outstanding": summary.get("outstanding", 0),
            "attempts": attempt_counts,
        },
    }
    if detail:
        # A failure message written for an operator. Provider bodies never reach this column.
        payload["error_message"] = job.error_message
        payload["cooldown_until"] = _iso(job.cooldown_until)
        payload["admission_key"] = job.admission_key
    return payload


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
