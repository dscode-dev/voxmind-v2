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

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.settings import settings
from app.db.session import get_db
from app.models.ai_execution import AIExecution
from app.models.enums import AIExecutionStatus, PipelineState
from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin

router = APIRouter()

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
        joinedload(PipelineJob.topic),
    )
    if state is not None:
        query = query.filter(PipelineJob.state == state)
    elif active:
        query = query.filter(PipelineJob.state.in_(ACTIVE_STATES))

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


@router.get("/admin/pipeline-jobs/{job_id}")
def get_pipeline_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """One run, with the provenance a person needs to understand where it came from."""
    job = (
        db.query(PipelineJob)
        .options(joinedload(PipelineJob.candidate), joinedload(PipelineJob.topic))
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
        "topic": {
            "id": str(job.topic_id) if job.topic_id else None,
            "name": job.topic.name if job.topic else None,
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
