"""Admin endpoints for the autonomous loop.

Three operations an operator actually needs: see what the scheduler is doing, turn a pipeline's
automation on or off, and force a cycle now.

The manual trigger calls the same ``AutonomousPipelineService`` the scheduler calls — directly,
not over HTTP to this same API. A loopback request would add a network hop, a second auth
check and a whole class of "the scheduler cannot reach itself" failures to invoke a function
already in the process.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db.session import get_db
from app.models.automation_state import AutomationState
from app.models.pipeline import Pipeline
from app.models.enums import VideoCandidateStatus
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.publishing.identity import AutomationHeartbeat
from app.security.auth_middleware import get_current_admin
from app.services.audit_service import AuditService
from app.services.automation_scheduler import AutomationScheduler
from app.services.automation_service import (
    HARD_MAX_ADMISSION_LIMIT,
    HARD_MAX_SELECTION_LIMIT,
    MIN_INTERVAL_MINUTES,
    AutomationConfig,
    AutonomousPipelineService,
)

router = APIRouter()
audit_service = AuditService()


def _pipeline() -> AutonomousPipelineService:
    return AutonomousPipelineService()


def _scheduler() -> AutomationScheduler:
    return AutomationScheduler()


class AutomationConfigInput(BaseModel):
    """The per-pipeline automation settings an operator may change.

    Bounded at the edge as well as in the service: a limit of 100000 is a mistake, and the
    earliest place to say so is the request.
    """

    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=MIN_INTERVAL_MINUTES, le=10_080)
    discovery_enabled: bool | None = None
    selection_enabled: bool | None = None
    admission_enabled: bool | None = None
    selection_limit: int | None = Field(default=None, ge=0, le=HARD_MAX_SELECTION_LIMIT)
    admission_limit: int | None = Field(default=None, ge=0, le=HARD_MAX_ADMISSION_LIMIT)
    max_selected_backlog: int | None = Field(default=None, ge=0, le=500)


@router.get("/admin/automation/status")
def automation_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """What the scheduler will do next, per pipeline."""
    pipelines = db.query(Pipeline).order_by(Pipeline.created_at.asc()).all()
    states = {
        state.pipeline_id: state for state in db.query(AutomationState).all()
    }

    backlog_rows = (
        db.query(VideoCandidate.pipeline_id, VideoCandidate.status)
        .filter(VideoCandidate.status == VideoCandidateStatus.SELECTED)
        .all()
    )
    backlog: dict[Any, int] = {}
    for pipeline_id, _ in backlog_rows:
        backlog[pipeline_id] = backlog.get(pipeline_id, 0) + 1

    runners = AutomationHeartbeat.alive()
    return {
        # The kill switch, and whether this process is the one ticking.
        "enabled": settings.autonomous_pipeline_enabled,
        # Configuration: this process was TOLD to run a loop.
        "runner_enabled": settings.automation_runner_enabled,
        # Evidence: a loop has actually ticked recently. PR-SCHEDULER-01 had only the line
        # above, so a dead task and a quiet one looked identical.
        "runners_alive": len(runners),
        "runner_state": _runner_state(runners),
        "last_tick_at": max(
            (r.get("last_tick_at") for r in runners if r.get("last_tick_at")),
            default=None,
        ),
        "runners": [
            {"runner_id": r.get("worker_id"), "state": r.get("state"),
             "last_tick_at": r.get("last_tick_at"),
             "last_heartbeat_at": r.get("last_heartbeat_at")}
            for r in runners
        ],
        "poll_interval_sec": settings.automation_poll_interval_sec,
        "pipelines": [
            _serialize_pipeline_state(pipeline, states.get(pipeline.id), backlog.get(pipeline.id, 0))
            for pipeline in pipelines
        ],
    }


@router.put("/admin/automation/pipelines/{pipeline_id}")
def update_automation_config(
    pipeline_id: uuid.UUID,
    payload: AutomationConfigInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Change a pipeline's automation settings.

    Pausing a pipeline only stops *new cycles*. Candidates keep their statuses, running
    PipelineJobs keep running, and nothing is deleted — a pause must be reversible without
    having lost anything.
    """
    pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if pipeline is None:
        raise HTTPException(status_code=404, detail="unknown pipeline")

    metadata = dict(pipeline.metadata_json or {})
    automation = dict(metadata.get("automation") or {})
    changes = payload.model_dump(exclude_none=True)
    automation.update(changes)
    metadata["automation"] = automation
    pipeline.metadata_json = metadata

    # A human changing whether the system may produce on its own is exactly what the audit
    # log is for. Scheduler ticks are not audited — they are events and logs.
    audit_service.log(
        db,
        action="admin.automation.configure",
        outcome="success",
        actor_user=admin,
        target_type="pipeline",
        target_id=str(pipeline.id),
        metadata={"changes": changes},
    )
    db.commit()
    db.refresh(pipeline)

    state = db.query(AutomationState).filter(AutomationState.pipeline_id == pipeline.id).first()
    return _serialize_pipeline_state(pipeline, state, _selected_backlog(db, pipeline))


@router.post("/admin/automation/pipelines/{pipeline_id}/run")
def run_pipeline_now(
    pipeline_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Run one full cycle now, ignoring the schedule.

    Goes through the scheduler rather than straight to the orchestrator, so a manual trigger
    takes the same per-pipeline lock and observes the same overlap guard as an automatic run.
    A manual run that skipped the lock could race an automatic one and break the caps both
    are meant to respect.

    The global kill switch still applies: if automation is off, it is off for everyone.
    """
    if not settings.autonomous_pipeline_enabled:
        raise HTTPException(
            status_code=409,
            detail="autonomous pipeline is disabled (AUTONOMOUS_PIPELINE_ENABLED=false)",
        )

    pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if pipeline is None:
        raise HTTPException(status_code=404, detail="unknown pipeline")

    config = AutomationConfig.from_pipeline(pipeline)
    if not config.enabled:
        raise HTTPException(status_code=409, detail="automation is disabled for this pipeline")

    scheduler = _scheduler()
    # Forced: a manual trigger means "now", so the due check is bypassed — but the lock and
    # the overlap guard are not.
    outcome = scheduler.run_pipeline_if_due(
        db,
        pipeline=pipeline,
        now=datetime.now(timezone.utc),
        force=True,
        # Recorded on the run: "did this happen because I pressed the button, or on its
        # own?" is the first question asked about a surprising cycle.
        trigger="manual",
        actor=str(admin.phone_number),
    )

    audit_service.log(
        db,
        action="admin.automation.manual_run",
        outcome="success",
        actor_user=admin,
        target_type="pipeline",
        target_id=str(pipeline.id),
        metadata={"forced": True},
    )
    db.commit()

    if isinstance(outcome, dict):
        return {"status": "skipped", **outcome}
    return outcome.as_dict()


@router.post("/admin/automation/tick")
def run_tick(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Run one scheduler pass immediately.

    The same code the background loop calls, exposed so a tick can be observed on demand
    instead of waiting for the timer — which is what makes the behaviour testable in a live
    environment rather than only in unit tests.
    """
    report = _scheduler().tick(db)
    return report.as_dict()


def _runner_state(runners: list[dict]) -> str:
    """Three states, because two would hide the interesting one.

    ``disabled``  nobody was asked to run a loop.
    ``live``      a loop reported a tick within its heartbeat TTL.
    ``stale``     one was expected and none is reporting - the case that used to be
                  indistinguishable from ``live``.
    """
    if not settings.automation_runner_enabled:
        return "disabled"
    return "live" if runners else "stale"


def _serialize_pipeline_state(
    pipeline: Pipeline, state: AutomationState | None, backlog: int
) -> dict[str, Any]:
    config = AutomationConfig.from_pipeline(pipeline)
    return {
        "pipeline_id": str(pipeline.id),
        "name": pipeline.name,
        "is_active": pipeline.is_active,
        "automation": config.as_dict(),
        "selected_backlog": backlog,
        "next_due_at": _iso(state.next_due_at) if state else None,
        "last_started_at": _iso(state.last_started_at) if state else None,
        "last_completed_at": _iso(state.last_completed_at) if state else None,
        "last_status": state.last_status if state else None,
        "last_automation_run_id": state.last_automation_run_id if state else None,
        "running_since": _iso(state.running_since) if state else None,
        "consecutive_failures": state.consecutive_failures if state else 0,
    }


def _selected_backlog(db: Session, pipeline: Pipeline) -> int:
    return (
        db.query(VideoCandidate)
        .filter(
            VideoCandidate.pipeline_id == pipeline.id,
            VideoCandidate.status == VideoCandidateStatus.SELECTED,
        )
        .count()
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
