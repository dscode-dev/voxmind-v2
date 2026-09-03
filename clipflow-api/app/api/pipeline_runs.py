"""Internal endpoints for the authoritative run lifecycle.

The worker reports *facts* — which step it is on, that it failed, that it finished — and the
API decides what those facts mean for the state. That split is the point: the worker never
writes to the database, never picks the next state, and cannot bypass the transition table by
naming a state directly. It sends a step name; ``WORKER_STAGE_TO_STATE`` (which lives only in
the API) resolves it.

Delivery is at-least-once. Every endpoint here is safe to call twice with the same body: a
repeated step is a no-op, and a step that has been overtaken is refused as stale rather than
rolling the run backwards. All of them answer 200 with a classified outcome instead of an
error, because a duplicate report is a normal event, not a fault.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.ai_execution import AIExecution
from app.models.enums import AIExecutionStatus, PipelineEventType, PipelineState
from app.security.auth_middleware import require_internal_api_token
from app.services import event_bus
from app.services.pipeline_job_service import PipelineJobService
from app.services.pipeline_state_machine import (
    NOT_MAPPED,
    PipelineStateMachine,
    state_for_stage,
)

router = APIRouter()
pipeline_job_service = PipelineJobService()
state_machine = PipelineStateMachine()

# Anything longer is a log line, not a status. The worker's structured logs hold the detail.
_MAX_ERROR_CHARS = 500


class CreateRunInput(BaseModel):
    worker_job_id: str
    source_url: str | None = None
    source_storage_key: str | None = None
    pipeline_stage: str = "prepare"
    clip_mode: str = "short_serie"
    video_ratio: str = "portrait"
    preset_id: str | None = None
    origin: str = "api"
    metadata: dict | None = None


class StageReportInput(BaseModel):
    """A worker reporting the step it is executing.

    Note what is absent: a state. The worker does not get to choose one.
    """

    stage: str
    status: str = "started"
    worker_id: str | None = None
    attempt: int | None = None
    metadata: dict | None = None


class FailureReportInput(BaseModel):
    error_type: str
    error_message: str = Field(default="", max_length=4000)
    attempt: int | None = None
    worker_id: str | None = None
    retryable: bool = False


class CompletionReportInput(BaseModel):
    publication_eligible: bool = False
    publication_eligibility: dict | None = None
    worker_id: str | None = None
    attempt: int | None = None


class AIExecutionInput(BaseModel):
    provider: str
    model: str | None = None
    purpose: str | None = None
    status: AIExecutionStatus = AIExecutionStatus.SUCCEEDED
    latency_ms: int | None = None
    prompt_chars: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    fallback_used: bool = False
    error_message: str | None = None
    attempt: int | None = None


@router.post("/internal/pipeline-runs")
def create_run(
    payload: CreateRunInput,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    """Create a run and queue it. Called by a producer before it pushes to Redis.

    Idempotent on ``worker_job_id`` while a run for it is still in flight, so a producer that
    retries this call does not start two runs for one payload.
    """
    existing = pipeline_job_service.get_by_worker_job_id(db, payload.worker_job_id)
    if existing is not None and existing.state == PipelineState.QUEUED:
        return {"status": "exists", **pipeline_job_service.serialize(existing)}

    job = pipeline_job_service.create_for_enqueue(
        db,
        worker_job_id=payload.worker_job_id,
        source_url=payload.source_url,
        source_storage_key=payload.source_storage_key,
        pipeline_stage=payload.pipeline_stage,
        clip_mode=payload.clip_mode,
        video_ratio=payload.video_ratio,
        preset_id=payload.preset_id,
        origin=payload.origin,
        metadata=payload.metadata,
    )
    return {"status": "created", **pipeline_job_service.serialize(job)}


@router.post("/internal/pipeline-runs/{pipeline_job_id}/claimed")
def report_claim(
    pipeline_job_id: str,
    worker_id: str | None = None,
    attempt: int | None = None,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    """The worker has taken possession of the payload.

    Reported *after* the Redis claim, never before: the queue owns possession, and a state
    that says a worker is running a job it does not hold would be a lie.
    """
    job = pipeline_job_service.get_for_update(db, pipeline_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown pipeline job")

    outcome = state_machine.report(
        db,
        job,
        PipelineState.DOWNLOADING,
        message="claimed by worker",
        payload={"attempt": attempt, "claimed_at": _now()},
        worker_id=worker_id,
    )
    db.commit()
    return {"status": "ok", **outcome.as_dict()}


@router.post("/internal/pipeline-runs/{pipeline_job_id}/stage")
def report_stage(
    pipeline_job_id: str,
    payload: StageReportInput,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    """Translate a worker step into a lifecycle transition."""
    job = pipeline_job_service.get_for_update(db, pipeline_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown pipeline job")

    target = state_for_stage(payload.stage)
    if target is None:
        # A real step that simply does not move the lifecycle (a cache probe, an artifact
        # write). Worth recording, not worth a transition.
        event = event_bus.publish_event(
            db,
            service="worker",
            event_type=PipelineEventType.INFO,
            pipeline_job_id=job.id,
            stage=payload.stage,
            message=f"{payload.stage}:{payload.status}",
            payload=_stage_payload(payload),
            worker_id=payload.worker_id,
        )
        db.commit()
        return {
            "status": "ok",
            "outcome": NOT_MAPPED,
            "from_state": job.state.value,
            "to_state": None,
            "detail": f"step '{payload.stage}' does not map to a lifecycle state",
            "event_id": str(event.id),
        }

    outcome = state_machine.report(
        db,
        job,
        target,
        message=f"{payload.stage}:{payload.status}",
        payload=_stage_payload(payload),
        worker_id=payload.worker_id,
    )
    db.commit()
    return {"status": "ok", **outcome.as_dict()}


@router.post("/internal/pipeline-runs/{pipeline_job_id}/failed")
def report_failure(
    pipeline_job_id: str,
    payload: FailureReportInput,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    job = pipeline_job_service.get_for_update(db, pipeline_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown pipeline job")

    outcome = state_machine.fail(
        db,
        job,
        error_type=payload.error_type,
        error_message=payload.error_message[:_MAX_ERROR_CHARS],
        attempt=payload.attempt,
        worker_id=payload.worker_id,
    )
    if outcome.applied:
        metadata = dict(job.metadata_json or {})
        metadata["last_failure"] = {
            "error_type": payload.error_type,
            "retryable": payload.retryable,
            "attempt": payload.attempt,
            "worker_id": payload.worker_id,
            "failed_at": _now(),
        }
        job.metadata_json = metadata
    db.commit()
    return {"status": "ok", **outcome.as_dict()}


@router.post("/internal/pipeline-runs/{pipeline_job_id}/retrying")
def report_retry(
    pipeline_job_id: str,
    attempt: int | None = None,
    worker_id: str | None = None,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    """The reliable queue scheduled another attempt of the same run.

    The run is not recreated: one PipelineJob spans every attempt, and ``retry_count``
    records which one is current. Fragmenting a run across rows would scatter its history
    exactly where an operator needs it in one place.
    """
    job = pipeline_job_service.get_for_update(db, pipeline_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown pipeline job")

    outcome = state_machine.requeue(
        db, job, reason="retry", attempt=attempt, worker_id=worker_id
    )
    db.commit()
    return {"status": "ok", **outcome.as_dict()}


@router.post("/internal/pipeline-runs/{pipeline_job_id}/completed")
def report_completion(
    pipeline_job_id: str,
    payload: CompletionReportInput,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    """Settle a finished run.

    ``publication_eligible`` comes from PR-QA-01's fail-closed gate. It decides between
    READY_TO_PUBLISH and REVIEW_REQUIRED — and neither means published, because no publisher
    exists.
    """
    job = pipeline_job_service.get_for_update(db, pipeline_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown pipeline job")

    outcome = state_machine.complete(
        db,
        job,
        publication_eligible=payload.publication_eligible,
        publication_eligibility=payload.publication_eligibility,
        worker_id=payload.worker_id,
    )
    db.commit()
    return {"status": "ok", **outcome.as_dict()}


@router.post("/internal/pipeline-runs/{pipeline_job_id}/ai-executions")
def record_ai_execution(
    pipeline_job_id: str,
    payload: AIExecutionInput,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    """Persist one AI provider call against the run that made it.

    Token counts and cost are written only when the provider actually reported them. An
    invented number here would become a billing figure somewhere else.
    """
    job = pipeline_job_service.get(db, pipeline_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown pipeline job")

    execution = AIExecution(
        pipeline_job_id=job.id,
        provider=payload.provider,
        model=payload.model,
        purpose=payload.purpose,
        status=payload.status,
        latency_ms=payload.latency_ms,
        prompt_chars=payload.prompt_chars,
        tokens_in=payload.tokens_in,
        tokens_out=payload.tokens_out,
        cost_usd=payload.cost_usd,
        error_message=(payload.error_message or None) and payload.error_message[:_MAX_ERROR_CHARS],
        # No prompt text: the prompt is already an artifact in MinIO, and duplicating it into
        # a metrics table would put user content somewhere nothing expects to find it.
        payload_json={
            "fallback_used": payload.fallback_used,
            "attempt": payload.attempt,
        },
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return {"status": "ok", "ai_execution_id": str(execution.id)}


@router.get("/internal/pipeline-runs/{pipeline_job_id}")
def get_run(
    pipeline_job_id: str,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    job = pipeline_job_service.get(db, pipeline_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown pipeline job")
    return pipeline_job_service.serialize(job)


def _stage_payload(payload: StageReportInput) -> dict:
    """Keep event payloads small and free of content.

    Transcripts, prompts and AI responses are artifacts; a copy of them in an event row is
    unbounded growth and an unexpected place for user content to live.
    """
    body = {"stage": payload.stage, "step_status": payload.status}
    if payload.attempt is not None:
        body["attempt"] = payload.attempt
    metadata = payload.metadata or {}
    if metadata:
        body["metadata"] = {
            key: value
            for key, value in list(metadata.items())[:20]
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    return body


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
