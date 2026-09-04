"""Admin endpoints for the autonomous loop.

Three operations an operator actually needs: see what the scheduler is doing, turn a topic's
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
from app.models.content_topic import ContentTopic
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
    """The per-topic automation settings an operator may change.

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
    """What the scheduler will do next, per topic."""
    topics = db.query(ContentTopic).order_by(ContentTopic.created_at.asc()).all()
    states = {
        state.topic_id: state for state in db.query(AutomationState).all()
    }

    backlog_rows = (
        db.query(VideoCandidate.topic_id, VideoCandidate.status)
        .filter(VideoCandidate.status == VideoCandidateStatus.SELECTED)
        .all()
    )
    backlog: dict[Any, int] = {}
    for topic_id, _ in backlog_rows:
        backlog[topic_id] = backlog.get(topic_id, 0) + 1

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
        "topics": [
            _serialize_topic_state(topic, states.get(topic.id), backlog.get(topic.id, 0))
            for topic in topics
        ],
    }


@router.put("/admin/automation/topics/{topic_id}")
def update_automation_config(
    topic_id: uuid.UUID,
    payload: AutomationConfigInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Change a topic's automation settings.

    Pausing a topic only stops *new cycles*. Candidates keep their statuses, running
    PipelineJobs keep running, and nothing is deleted — a pause must be reversible without
    having lost anything.
    """
    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if topic is None:
        raise HTTPException(status_code=404, detail="unknown topic")

    metadata = dict(topic.metadata_json or {})
    automation = dict(metadata.get("automation") or {})
    changes = payload.model_dump(exclude_none=True)
    automation.update(changes)
    metadata["automation"] = automation
    topic.metadata_json = metadata

    # A human changing whether the system may produce on its own is exactly what the audit
    # log is for. Scheduler ticks are not audited — they are events and logs.
    audit_service.log(
        db,
        action="admin.automation.configure",
        outcome="success",
        actor_user=admin,
        target_type="content_topic",
        target_id=str(topic.id),
        metadata={"changes": changes},
    )
    db.commit()
    db.refresh(topic)

    state = db.query(AutomationState).filter(AutomationState.topic_id == topic.id).first()
    return _serialize_topic_state(topic, state, _selected_backlog(db, topic))


@router.post("/admin/automation/topics/{topic_id}/run")
def run_topic_now(
    topic_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Run one full cycle now, ignoring the schedule.

    Goes through the scheduler rather than straight to the orchestrator, so a manual trigger
    takes the same per-topic lock and observes the same overlap guard as an automatic run.
    A manual run that skipped the lock could race an automatic one and break the caps both
    are meant to respect.

    The global kill switch still applies: if automation is off, it is off for everyone.
    """
    if not settings.autonomous_pipeline_enabled:
        raise HTTPException(
            status_code=409,
            detail="autonomous pipeline is disabled (AUTONOMOUS_PIPELINE_ENABLED=false)",
        )

    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if topic is None:
        raise HTTPException(status_code=404, detail="unknown topic")

    config = AutomationConfig.from_topic(topic)
    if not config.enabled:
        raise HTTPException(status_code=409, detail="automation is disabled for this topic")

    scheduler = _scheduler()
    # Forced: a manual trigger means "now", so the due check is bypassed — but the lock and
    # the overlap guard are not.
    outcome = scheduler.run_topic_if_due(db, topic=topic, now=datetime.now(timezone.utc), force=True)

    audit_service.log(
        db,
        action="admin.automation.manual_run",
        outcome="success",
        actor_user=admin,
        target_type="content_topic",
        target_id=str(topic.id),
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


def _serialize_topic_state(
    topic: ContentTopic, state: AutomationState | None, backlog: int
) -> dict[str, Any]:
    config = AutomationConfig.from_topic(topic)
    return {
        "topic_id": str(topic.id),
        "name": topic.name,
        "is_active": topic.is_active,
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


def _selected_backlog(db: Session, topic: ContentTopic) -> int:
    return (
        db.query(VideoCandidate)
        .filter(
            VideoCandidate.topic_id == topic.id,
            VideoCandidate.status == VideoCandidateStatus.SELECTED,
        )
        .count()
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
