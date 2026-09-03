"""Admin endpoints for publishing. Every one of them is admin-only.

There is no public trigger, and there is no endpoint anywhere that publishes as a side effect
of something else. Publishing happens because a named admin asked for it, which is why each
route audits and why the manual publish route defaults to a dry run.

The OAuth callback is the one route without an admin dependency, for the reason every OAuth
callback is: the browser arriving from Google carries Google's cookies, not ours. Its
authorisation is the ``state`` parameter — unguessable, single-use, expiring, and bound to the
admin who started the flow. That check happens in ``PublishTargetService.consume_state`` and
the route cannot skip it.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db.session import get_db
from app.models.enums import PublishAttemptStatus, PublishPlatform
from app.models.content_topic import ContentTopic
from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt
from app.models.publish_target import PublishTarget
from app.models.user import User
from app.publishing.contracts import ProviderNotConfiguredError
from app.security.auth_middleware import get_current_admin
from app.security.secret_box import secret_box
from app.services.audit_service import AuditService
from app.services.autopublish_service import AutonomousPublicationService
from app.services.publish_resolution_service import (
    PublishResolutionService,
    ResolutionError,
    serialize_attempt,
)
from app.services.publish_target_service import ConnectError, PublishTargetService
from app.services.publish_runtime import runtime_snapshot
from app.services.publishing_service import EXECUTABLE_STATUSES, PublishingService

logger = logging.getLogger(__name__)

router = APIRouter()
audit_service = AuditService()


def _targets() -> PublishTargetService:
    return PublishTargetService()


def _publishing() -> PublishingService:
    return PublishingService()


def _resolution() -> PublishResolutionService:
    return PublishResolutionService()


def _autopublish() -> AutonomousPublicationService:
    return AutonomousPublicationService()


# =============================================================================
# Targets
# =============================================================================


class TargetUpdateInput(BaseModel):
    """What an operator may change on a target.

    Deliberately small, and containing no credential field: a token enters this system only
    through the OAuth callback, never through a request body an operator could paste into.
    """

    is_active: bool | None = None
    name: str | None = Field(default=None, max_length=255)
    default_privacy: str | None = Field(default=None, pattern="^(private|unlisted|public)$")
    default_category_id: str | None = Field(default=None, pattern=r"^\d{1,3}$")
    default_language: str | None = Field(default=None, max_length=16)
    made_for_kids: bool | None = None

    # Consent for this channel to be published to without a human deciding each time.
    # Separate from is_active, which only says the channel may be published to at all.
    autopublish_enabled: bool | None = None


@router.get("/admin/publish-targets")
def list_targets(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Every target, plus whether this deployment could publish at all."""
    targets = db.query(PublishTarget).order_by(PublishTarget.created_at.asc()).all()
    service = _targets()
    return {
        "publishing_enabled": settings.publishing_enabled,
        "provider_configured": service.oauth.configured,
        "secret_storage_available": secret_box.available,
        "targets": [PublishTargetService.serialize(target) for target in targets],
    }


@router.post("/admin/publish-targets/youtube/connect")
def connect_youtube(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Start the OAuth flow. Returns the URL the operator's browser must visit."""
    service = _targets()
    try:
        result = service.begin_connect(db, actor=admin)
    except ProviderNotConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConnectError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit_service.log(
        db,
        action="admin.publish_target.connect_started",
        outcome="success",
        actor_user=admin,
        target_type="publish_target",
        target_id=None,
        metadata={"provider": "youtube"},
    )
    db.commit()
    return result


@router.get("/auth/youtube/callback")
def youtube_callback(
    db: Session = Depends(get_db),
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """Where Google sends the operator back.

    Not admin-guarded, and it cannot be: the request is a browser redirect carrying Google's
    session, not ours. ``state`` is the authorisation — unguessable, single-use, expiring and
    bound to the admin who started the flow — and it is checked before the code is spent.
    """
    if error:
        # Google's own error code only. The description can echo request parameters.
        return JSONResponse(
            status_code=400,
            content={"status": "denied", "error": str(error)[:64]},
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")

    service = _targets()
    try:
        target = service.complete_connect(db, code=code, state_value=state)
    except ConnectError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProviderNotConfiguredError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit_service.log(
        db,
        action="admin.publish_target.connected",
        outcome="success",
        target_type="publish_target",
        target_id=str(target.id),
        metadata={"provider": "youtube", "channel_id": target.channel_id},
    )
    db.commit()
    db.refresh(target)
    return {
        "status": "connected",
        # Said explicitly: the operator has to enable it, and telling them here is what makes
        # that a deliberate choice rather than a surprise later.
        "note": (
            "target created disabled. Verify the channel below is correct, then enable it "
            "with PUT /admin/publish-targets/{id}."
        ),
        "target": PublishTargetService.serialize(target),
    }


@router.put("/admin/publish-targets/{target_id}")
def update_target(
    target_id: uuid.UUID,
    payload: TargetUpdateInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Enable, disable, or set publishing defaults."""
    target = PublishTargetService.get(db, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown publish target")

    changes = payload.model_dump(exclude_none=True)

    if "is_active" in changes and changes["is_active"]:
        if not target.is_publishable and not target.refresh_token_encrypted:
            raise HTTPException(
                status_code=409,
                detail="target has no stored credential; connect it before enabling",
            )

    if "name" in changes:
        target.name = changes["name"]
    if "is_active" in changes:
        target.is_active = bool(changes["is_active"])

    if "autopublish_enabled" in changes:
        enabling = bool(changes["autopublish_enabled"])
        if enabling and not target.is_publishable:
            raise HTTPException(
                status_code=409,
                detail=(
                    "target is not publishable (inactive or disconnected); it cannot be "
                    "enabled for automatic publication"
                ),
            )
        if enabling and not target.autopublish_enabled:
            # Stamped on the transition from off to on, and never moved afterwards. This is
            # the cutoff that stops enabling automation from publishing everything that was
            # already waiting: only runs that become ready after this moment are automatic.
            target.autopublish_enabled_at = datetime.now(timezone.utc)
        target.autopublish_enabled = enabling

    defaults = dict(target.config_json or {})
    for field_name, config_key in (
        ("default_privacy", "default_privacy"),
        ("default_category_id", "default_category_id"),
        ("default_language", "default_language"),
        ("made_for_kids", "made_for_kids"),
    ):
        if field_name in changes:
            defaults[config_key] = changes[field_name]
    target.config_json = defaults

    audit_service.log(
        db,
        action="admin.publish_target.updated",
        outcome="success",
        actor_user=admin,
        target_type="publish_target",
        target_id=str(target.id),
        metadata={"changes": changes},
    )
    db.commit()
    db.refresh(target)
    return PublishTargetService.serialize(target)


@router.post("/admin/publish-targets/{target_id}/disconnect")
def disconnect_target(
    target_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Detach the credential. Attempt history is kept; the ability to publish is not."""
    target = PublishTargetService.get(db, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown publish target")

    _targets().disconnect(db, target)
    audit_service.log(
        db,
        action="admin.publish_target.disconnected",
        outcome="success",
        actor_user=admin,
        target_type="publish_target",
        target_id=str(target.id),
        metadata={"provider": target.platform.value},
    )
    db.commit()
    db.refresh(target)
    return PublishTargetService.serialize(target)


# =============================================================================
# Publishing
# =============================================================================


class PublishInput(BaseModel):
    """One manual publish command.

    ``dry_run`` defaults to True. Publishing is irreversible from inside this system, so the
    safe operation is the one you get by accident.
    """

    target_id: uuid.UUID
    dry_run: bool = True
    # Which outputs of this run to publish. Absent means every generated final clip, which is
    # stated rather than implied because a run can produce several.
    video_indexes: list[int] | None = None
    privacy: str | None = Field(default=None, pattern="^(private|unlisted|public)$")
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=6000)
    tags: list[str] | None = None
    category_id: str | None = Field(default=None, pattern=r"^\d{1,3}$")
    language: str | None = Field(default=None, max_length=16)
    made_for_kids: bool | None = None

    def overrides(self) -> dict[str, Any]:
        return {
            "privacy": self.privacy,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "category_id": self.category_id,
            "language": self.language,
            "made_for_kids": self.made_for_kids,
        }


@router.post("/admin/pipeline-jobs/{job_id}/publish", status_code=202)
def publish_job(
    job_id: uuid.UUID,
    payload: PublishInput,
    response: Response,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Accept a publication, or validate that one could be accepted.

    **Asynchronous since PR-PUBLISH-QUEUE-01.** With ``dry_run=false`` this validates, creates
    or reuses the PublishAttempt rows, puts a command on the publish queue and returns 202 —
    it does not wait for the upload. A large clip took longer than the proxy timeout between
    the operator and this endpoint, and the publication carried on regardless, so the request
    was reporting a failure that had not happened.

    A dry run stays synchronous and returns 200: it touches no provider, so there is nothing
    to wait for.

    Still the only path that leads to a provider, and still nothing calls it automatically.
    """
    job = db.query(PipelineJob).filter(PipelineJob.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="unknown pipeline job")

    target = PublishTargetService.get(db, payload.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown publish target")
    if target.platform != PublishPlatform.YOUTUBE:
        raise HTTPException(
            status_code=400,
            detail=f"{target.platform.value} publishing is not implemented",
        )

    report = _publishing().publish(
        db,
        job=job,
        target=target,
        dry_run=payload.dry_run,
        overrides=payload.overrides(),
        media_selection=payload.video_indexes,
        actor=str(admin.id),
    )

    # A dry run answers now; an accepted publication answers later. Saying 202 for a
    # validation that already completed would be a lie, and saying 200 for work that has not
    # started would be a worse one.
    if payload.dry_run or report.status == "blocked":
        response.status_code = 200

    audit_service.log(
        db,
        action="admin.publish.requested",
        outcome=report.status,
        actor_user=admin,
        target_type="pipeline_job",
        target_id=str(job.id),
        metadata={
            "publish_target_id": str(target.id),
            "dry_run": payload.dry_run,
            "video_indexes": payload.video_indexes,
            "publication_status": report.publication_status,
            "blocked_by": report.blocked_by,
        },
    )
    db.commit()
    return report.as_dict()


class AutopublishRunInput(BaseModel):
    """One manual invocation of the publication policy.

    ``dry_run`` defaults to True, like the publish command and for the same reason: this
    endpoint can put videos on a channel, so the safe operation is the one you get by
    accident. It exists mainly to prove the policy before the scheduler is allowed to run it.
    """

    dry_run: bool = True
    topic_id: uuid.UUID | None = None
    limit: int | None = Field(default=None, ge=0, le=10)


@router.post("/admin/autopublish/run")
def autopublish_run(
    payload: AutopublishRunInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Evaluate the autopublish policy now, and optionally act on it."""
    topic = None
    if payload.topic_id is not None:
        topic = db.query(ContentTopic).filter(ContentTopic.id == payload.topic_id).first()
        if topic is None:
            raise HTTPException(status_code=404, detail="unknown topic")

    report = _autopublish().run(
        db,
        topic=topic,
        dry_run=payload.dry_run,
        limit=payload.limit,
        actor=str(admin.id),
    )

    if not payload.dry_run:
        # A human choosing to let the policy act is a decision worth recording. A dry run is
        # not: it changes nothing.
        audit_service.log(
            db,
            action="admin.autopublish.run",
            outcome=report.status,
            actor_user=admin,
            target_type="content_topic",
            target_id=str(topic.id) if topic else None,
            metadata={
                "autopublish_run_id": report.autopublish_run_id,
                "queued": report.queued,
                "blocked": report.blocked,
                "blocked_reasons": report.blocked_reasons,
            },
        )
        db.commit()
    return report.as_dict()


@router.get("/admin/autopublish/status")
def autopublish_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Whether the system may publish on its own, and what is standing in the way."""
    return _autopublish().status(db)


@router.get("/admin/publishing/runtime")
def publishing_runtime(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Is publishing actually running, and how much is waiting?

    Exists because PR-SCHEDULER-01 shipped a background loop whose liveness could only be
    inferred, and a dead publisher looks exactly like an empty queue: every manual publish
    accepted, none executed, nothing to point at. ``workers`` comes from heartbeats with a
    TTL, so a process that died stops being listed without anything having to notice.
    """
    snapshot = runtime_snapshot()
    snapshot["pending_enqueue"] = (
        db.query(PublishAttempt)
        .filter(
            PublishAttempt.enqueued_at.is_(None),
            PublishAttempt.status.in_(EXECUTABLE_STATUSES),
        )
        .count()
    )
    snapshot["unresolved"] = (
        db.query(PublishAttempt)
        .filter(
            PublishAttempt.status.in_(
                [PublishAttemptStatus.UNKNOWN,
                 PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION]
            )
        )
        .count()
    )
    return snapshot


@router.get("/admin/pipeline-jobs/{job_id}/publish-attempts")
def list_attempts(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """The publication history of one run."""
    job = db.query(PipelineJob).filter(PipelineJob.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="unknown pipeline job")

    attempts = (
        db.query(PublishAttempt)
        .filter(PublishAttempt.pipeline_job_id == job_id)
        .order_by(PublishAttempt.created_at.asc())
        .all()
    )
    metadata = job.metadata_json or {}
    return {
        "pipeline_job_id": str(job.id),
        "job_state": job.state.value,
        "publication_status": metadata.get("publication_status", "none"),
        "publication_eligibility": metadata.get("publication_eligibility"),
        "attempts": [serialize_attempt(attempt) for attempt in attempts],
    }


# =============================================================================
# Resolution
# =============================================================================


class ResolveInput(BaseModel):
    external_id: str = Field(min_length=5, max_length=64)
    # Skipping verification is possible but has to be asked for: the default writes an id
    # only after confirming the video exists and belongs to this target's channel.
    verify: bool = True


class NotPublishedInput(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.get("/admin/publish-attempts/unresolved")
def list_unresolved(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Everything waiting on a human. The queue an operator actually works from."""
    attempts = (
        db.query(PublishAttempt)
        .filter(
            PublishAttempt.status.in_(
                [
                    PublishAttemptStatus.UNKNOWN,
                    PublishAttemptStatus.NEEDS_MANUAL_RESOLUTION,
                ]
            )
        )
        .order_by(PublishAttempt.created_at.asc())
        .all()
    )
    return {"count": len(attempts),
            "attempts": [serialize_attempt(attempt) for attempt in attempts]}


@router.post("/admin/publish-attempts/{attempt_id}/reconcile")
def reconcile_attempt(
    attempt_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Ask the provider whether the ambiguous upload actually completed.

    Never uploads anything. If the session cannot answer, the attempt is marked as needing a
    person rather than being guessed either way.
    """
    attempt = _get_attempt(db, attempt_id)
    try:
        result = _resolution().reconcile(db, attempt)
    except ResolutionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit_service.log(
        db,
        action="admin.publish_attempt.reconciled",
        outcome=result.get("status", "unknown"),
        actor_user=admin,
        target_type="publish_attempt",
        target_id=str(attempt.id),
        metadata={"external_id": result.get("external_id")},
    )
    db.commit()
    return result


@router.post("/admin/publish-attempts/{attempt_id}/resolve")
def resolve_attempt(
    attempt_id: uuid.UUID,
    payload: ResolveInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Record the video id an operator confirmed on the channel."""
    attempt = _get_attempt(db, attempt_id)
    try:
        result = _resolution().resolve(
            db, attempt, external_id=payload.external_id, verify=payload.verify
        )
    except ResolutionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit_service.log(
        db,
        action="admin.publish_attempt.resolved",
        outcome="success",
        actor_user=admin,
        target_type="publish_attempt",
        target_id=str(attempt.id),
        metadata={"external_id": payload.external_id, "verified": payload.verify},
    )
    db.commit()
    return result


@router.post("/admin/publish-attempts/{attempt_id}/mark-not-published")
def mark_not_published(
    attempt_id: uuid.UUID,
    payload: NotPublishedInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Record that an operator checked and no video exists for this attempt."""
    attempt = _get_attempt(db, attempt_id)
    try:
        result = _resolution().mark_not_published(db, attempt, note=payload.note)
    except ResolutionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit_service.log(
        db,
        action="admin.publish_attempt.marked_not_published",
        outcome="success",
        actor_user=admin,
        target_type="publish_attempt",
        target_id=str(attempt.id),
        metadata={"note": payload.note},
    )
    db.commit()
    return result


@router.post("/admin/publish-attempts/{attempt_id}/cancel")
def cancel_attempt(
    attempt_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Cancel an attempt that has not started uploading.

    Does not remove anything already published; removing a live video is out of scope.
    """
    attempt = _get_attempt(db, attempt_id)
    try:
        result = _resolution().cancel(db, attempt)
    except ResolutionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit_service.log(
        db,
        action="admin.publish_attempt.canceled",
        outcome="success",
        actor_user=admin,
        target_type="publish_attempt",
        target_id=str(attempt.id),
    )
    db.commit()
    return result


def _get_attempt(db: Session, attempt_id: uuid.UUID) -> PublishAttempt:
    attempt = db.query(PublishAttempt).filter(PublishAttempt.id == attempt_id).first()
    if attempt is None:
        raise HTTPException(status_code=404, detail="unknown publish attempt")
    return attempt
