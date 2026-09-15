"""The pipeline: the one object an operator creates, configures and watches.

Everything the product does hangs off one of these — the sources it searches, the candidates
it finds, the productions it starts and the channel those reach. So this is a real CRUD
surface rather than the create-and-list pair that used to live in the discovery module: you
cannot ask someone to operate a thing they cannot rename, retune or delete.

**Identity and policy are edited together, from one payload.** They were split across two
endpoints — `/admin/content-topics` owned the name and the keywords, `/admin/automation/...`
owned whether it ran — and an operator had to know which half of the object a field lived in.
They are both just "how this pipeline behaves".

**Deleting is a real delete.** Sources and candidates cascade, because they only ever meant
something in the context of the pipeline that found them. Productions do not: a video that
was made and published happened, and its row stays with a null pipeline rather than being
erased to tidy up a configuration change.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.automation_run import AutomationRun
from app.models.automation_state import AutomationState
from app.models.discovery_source import DiscoverySource
from app.models.enums import DiscoverySourceKind, PipelineState, VideoCandidateStatus
from app.models.pipeline import Pipeline
from app.models.pipeline_job import PipelineJob
from app.models.publish_target import PublishTarget
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin
from app.services.audit_service import AuditService
from app.services.automation_service import AutomationConfig

router = APIRouter()
audit_service = AuditService()

# Um teto para a listagem de execuções. Uma página sem limite é uma varredura
# da tabela esperando para acontecer.
MAX_PAGE_SIZE = 100


class AutomationInput(BaseModel):
    """The policy half. Every field optional: a PUT changes what it names and nothing else."""

    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=10_080)
    discovery_enabled: bool | None = None
    selection_enabled: bool | None = None
    admission_enabled: bool | None = None
    autopublish_enabled: bool | None = None
    autopublish_limit: int | None = Field(default=None, ge=0, le=50)
    selection_limit: int | None = Field(default=None, ge=0, le=50)
    admission_limit: int | None = Field(default=None, ge=0, le=50)
    max_selected_backlog: int | None = Field(default=None, ge=0, le=500)
    failure_backoff_minutes: int | None = Field(default=None, ge=1, le=1440)
    max_consecutive_failures: int | None = Field(default=None, ge=1, le=100)
    publish_target_id: uuid.UUID | None = None


class SourceInput(BaseModel):
    kind: DiscoverySourceKind
    name: str | None = Field(default=None, max_length=255)
    is_active: bool = True
    # Provider-specific. Validated per kind by `_validated_config` rather than by one schema
    # per kind: the shapes are small, and three near-identical models would drift.
    config: dict[str, Any] = Field(default_factory=dict)


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    config: dict[str, Any] | None = None


class PipelineInput(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    theme: str | None = Field(default=None, max_length=255)
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)
    is_active: bool = True
    telegram_chat_id: str | None = Field(default=None, max_length=64)
    default_clip_mode: str = Field(default="short_serie", max_length=64)
    default_video_ratio: str = Field(default="portrait", max_length=32)
    language: str | None = None
    region: str | None = None
    freshness_days: int | None = Field(default=None, ge=1, le=365)
    automation: AutomationInput | None = None


class PipelineUpdate(BaseModel):
    """A partial update. Absent means unchanged, which is not the same as cleared."""

    name: str | None = Field(default=None, min_length=2, max_length=255)
    theme: str | None = Field(default=None, max_length=255)
    description: str | None = None
    keywords: list[str] | None = None
    is_active: bool | None = None
    telegram_chat_id: str | None = Field(default=None, max_length=64)
    default_clip_mode: str | None = Field(default=None, max_length=64)
    default_video_ratio: str | None = Field(default=None, max_length=32)
    automation: AutomationInput | None = None


# =============================================================================
# CRUD
# =============================================================================


@router.post("/admin/pipelines", status_code=status.HTTP_201_CREATED)
def create_pipeline(
    payload: PipelineInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    _reject_duplicate_name(db, payload.name)
    pipeline = Pipeline(
        name=payload.name,
        theme=payload.theme,
        description=payload.description,
        keywords_json=payload.keywords,
        is_active=payload.is_active,
        telegram_chat_id=_clean(payload.telegram_chat_id),
        default_clip_mode=payload.default_clip_mode,
        default_video_ratio=payload.default_video_ratio,
        metadata_json=_discovery_metadata(payload),
    )
    _apply_automation(db, pipeline, payload.automation)

    db.add(pipeline)
    audit_service.log(
        db,
        action="admin.pipeline.create",
        outcome="success",
        actor_user=admin,
        target_type="pipeline",
        metadata={"name": payload.name},
    )
    db.commit()
    db.refresh(pipeline)
    return _serialize(db, pipeline, detail=True)


@router.get("/admin/pipelines")
def list_pipelines(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Every pipeline, with the counts that say whether it is doing anything.

    The counts are three grouped queries for the whole page, not three per row: a list that
    issues a query per pipeline is fine with two of them and unusable with twenty.
    """
    pipelines = db.query(Pipeline).order_by(Pipeline.created_at.desc()).all()
    ids = [pipeline.id for pipeline in pipelines]
    counts = _counts(db, ids)
    states = _automation_states(db, ids)
    return [
        _serialize(db, pipeline, counts=counts.get(pipeline.id), state=states.get(pipeline.id))
        for pipeline in pipelines
    ]


@router.get("/admin/pipelines/{pipeline_id}")
def get_pipeline(
    pipeline_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    pipeline = _require(db, pipeline_id)
    counts = _counts(db, [pipeline.id]).get(pipeline.id)
    state = _automation_states(db, [pipeline.id]).get(pipeline.id)
    return _serialize(db, pipeline, counts=counts, state=state, detail=True)


@router.put("/admin/pipelines/{pipeline_id}")
def update_pipeline(
    pipeline_id: uuid.UUID,
    payload: PipelineUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    pipeline = _require(db, pipeline_id)
    fields = payload.model_dump(exclude_unset=True, exclude={"automation", "keywords"})

    if "name" in fields and fields["name"] != pipeline.name:
        _reject_duplicate_name(db, fields["name"])
    if payload.keywords is not None:
        pipeline.keywords_json = payload.keywords
    if "telegram_chat_id" in fields:
        fields["telegram_chat_id"] = _clean(fields["telegram_chat_id"])

    for field, value in fields.items():
        setattr(pipeline, field, value)

    _apply_automation(db, pipeline, payload.automation)

    audit_service.log(
        db,
        action="admin.pipeline.update",
        outcome="success",
        actor_user=admin,
        target_type="pipeline",
        target_id=str(pipeline.id),
        metadata={
            "changed": sorted(
                set(fields) | ({"automation"} if payload.automation else set())
            )
        },
    )
    db.commit()
    db.refresh(pipeline)
    return _serialize(db, pipeline, detail=True)


@router.delete("/admin/pipelines/{pipeline_id}")
def delete_pipeline(
    pipeline_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Delete the pipeline and what only meant something inside it.

    Sources and candidates go: a candidate is "a video this pipeline found", and without the
    pipeline it is a URL nobody asked for. Productions stay, with a null pipeline — they
    describe work that really happened, and rewriting history to tidy up a configuration
    change would lose the only record that a published video was ever produced.
    """
    pipeline = _require(db, pipeline_id)
    counts = _counts(db, [pipeline.id]).get(pipeline.id, {})

    audit_service.log(
        db,
        action="admin.pipeline.delete",
        outcome="success",
        actor_user=admin,
        target_type="pipeline",
        target_id=str(pipeline.id),
        metadata={"name": pipeline.name, "counts": counts},
    )
    db.delete(pipeline)
    db.commit()
    return {
        "status": "deleted",
        "id": str(pipeline_id),
        "kept_productions": counts.get("productions", 0),
    }


# =============================================================================
# Sources
# =============================================================================
#
# Nested under the pipeline because that is where a source exists: "a feed" on its own is not
# something anyone operates. They used to be a flat `/admin/discovery-sources` pair with no
# way to edit or remove one, so a mistyped feed URL was permanent unless somebody opened psql.


@router.post("/admin/pipelines/{pipeline_id}/sources", status_code=status.HTTP_201_CREATED)
def create_source(
    pipeline_id: uuid.UUID,
    payload: SourceInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    pipeline = _require(db, pipeline_id)
    source = DiscoverySource(
        pipeline_id=pipeline.id,
        kind=payload.kind,
        name=payload.name,
        is_active=payload.is_active,
        config_json=_validated_config(payload.kind, payload.config),
    )
    db.add(source)
    audit_service.log(
        db,
        action="admin.pipeline.source.create",
        outcome="success",
        actor_user=admin,
        target_type="discovery_source",
        metadata={"kind": payload.kind.value, "pipeline_id": str(pipeline.id)},
    )
    db.commit()
    db.refresh(source)
    return _serialize_source(source)


@router.get("/admin/pipelines/{pipeline_id}/sources")
def list_sources(
    pipeline_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    _require(db, pipeline_id)
    sources = (
        db.query(DiscoverySource)
        .filter(DiscoverySource.pipeline_id == pipeline_id)
        .order_by(DiscoverySource.created_at.asc())
        .all()
    )
    return [_serialize_source(source) for source in sources]


@router.put("/admin/pipelines/{pipeline_id}/sources/{source_id}")
def update_source(
    pipeline_id: uuid.UUID,
    source_id: uuid.UUID,
    payload: SourceUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Everything except the kind.

    Changing the kind would change which provider reads the config, leaving a source whose
    settings mean nothing to whatever now fetches it. Replacing it is one more click and says
    what is really happening.
    """
    source = _require_source(db, pipeline_id, source_id)
    fields = payload.model_dump(exclude_unset=True)

    if fields.get("config") is not None:
        source.config_json = _validated_config(source.kind, fields["config"])
    if "name" in fields:
        source.name = fields["name"]
    if fields.get("is_active") is not None:
        source.is_active = fields["is_active"]

    audit_service.log(
        db,
        action="admin.pipeline.source.update",
        outcome="success",
        actor_user=admin,
        target_type="discovery_source",
        target_id=str(source.id),
        metadata={"changed": sorted(fields)},
    )
    db.commit()
    db.refresh(source)
    return _serialize_source(source)


@router.delete("/admin/pipelines/{pipeline_id}/sources/{source_id}")
def delete_source(
    pipeline_id: uuid.UUID,
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Remove the source. The videos it already found stay.

    They are candidates of the *pipeline*, not of the feed — deleting a feed because it went
    stale must not retract videos that were found, judged, and possibly already produced.
    """
    source = _require_source(db, pipeline_id, source_id)
    found = (
        db.query(func.count(VideoCandidate.id))
        .filter(VideoCandidate.source_id == source.id)
        .scalar()
    ) or 0

    audit_service.log(
        db,
        action="admin.pipeline.source.delete",
        outcome="success",
        actor_user=admin,
        target_type="discovery_source",
        target_id=str(source.id),
        metadata={"kind": source.kind.value, "candidates_kept": int(found)},
    )
    db.delete(source)
    db.commit()
    return {"status": "deleted", "id": str(source_id), "candidates_kept": int(found)}


# =============================================================================
# Runs
# =============================================================================


@router.get("/admin/pipelines/{pipeline_id}/runs")
def list_runs(
    pipeline_id: uuid.UUID,
    limit: int = 25,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """The cycles this pipeline has executed, newest first.

    Every execution appears, including the ones that decided to do nothing. A skipped cycle
    with its reason is the answer to "why is nothing happening?", and hiding it would leave
    the operator staring at an empty list with no way to tell the difference between "it never
    ran" and "it ran and found nothing".
    """
    _require(db, pipeline_id)
    runs = (
        db.query(AutomationRun)
        .filter(AutomationRun.pipeline_id == pipeline_id)
        .order_by(AutomationRun.started_at.desc())
        .limit(max(1, min(limit, MAX_PAGE_SIZE)))
        .all()
    )
    return [_serialize_run(run) for run in runs]


@router.get("/admin/pipelines/{pipeline_id}/runs/{run_id}")
def get_run(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """One cycle, with the stage-by-stage report and the productions it started."""
    _require(db, pipeline_id)
    run = (
        db.query(AutomationRun)
        .filter(AutomationRun.id == run_id, AutomationRun.pipeline_id == pipeline_id)
        .first()
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown run")

    payload = _serialize_run(run)
    payload["stages"] = run.stages_json or {}
    payload["productions"] = [
        {
            "id": str(job.id),
            "state": job.state.value if job.state else None,
            "title": job.source_url,
        }
        for job in (
            db.query(PipelineJob)
            .filter(PipelineJob.automation_run_id == run.id)
            .order_by(PipelineJob.created_at.asc())
            .all()
        )
    ]
    return payload


def _serialize_run(run: AutomationRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "pipeline_id": str(run.pipeline_id) if run.pipeline_id else None,
        "trigger": run.trigger,
        "actor": run.actor,
        "status": run.status,
        "skip_reason": run.skip_reason,
        # Separate from `status` on purpose: a cycle can have completed perfectly and still be
        # waiting on four renders.
        "production_status": run.production_status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "settled_at": run.settled_at,
        "duration_ms": run.duration_ms,
        "counts": {
            "discovered": run.discovered,
            "selected": run.selected,
            "admitted": run.admitted,
            "publications_queued": run.publications_queued,
            "published": run.published,
        },
    }


# =============================================================================
# Helpers
# =============================================================================


def _require(db: Session, pipeline_id: uuid.UUID) -> Pipeline:
    pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown pipeline"
        )
    return pipeline


def _reject_duplicate_name(db: Session, name: str) -> None:
    """Answered before the insert so the operator gets a sentence, not a constraint name."""
    if db.query(Pipeline).filter(func.lower(Pipeline.name) == name.strip().lower()).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a pipeline with this name already exists",
        )


def _require_source(
    db: Session, pipeline_id: uuid.UUID, source_id: uuid.UUID
) -> DiscoverySource:
    """Scoped to the pipeline in the path, never looked up by id alone.

    Otherwise one pipeline URL could edit another pipeline source, and the audit entry would
    name the wrong pipeline.
    """
    _require(db, pipeline_id)
    source = (
        db.query(DiscoverySource)
        .filter(
            DiscoverySource.id == source_id,
            DiscoverySource.pipeline_id == pipeline_id,
        )
        .first()
    )
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown source")
    return source


def _validated_config(kind: DiscoverySourceKind, config: dict[str, Any]) -> dict[str, Any]:
    """What this kind needs, checked now rather than at the next discovery run.

    A feed with no URL is accepted silently today and fails an hour later inside a scheduled
    run, where the operator who typed it is not watching.
    """
    raw = config or {}
    cleaned = {key: value for key, value in raw.items() if value not in (None, "")}

    if kind == DiscoverySourceKind.RSS:
        if not str(cleaned.get("feed_url") or "").strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="uma fonte RSS precisa de um feed_url",
            )
        cleaned["feed_url"] = str(cleaned["feed_url"]).strip()

    # An explicit empty list is meaningful here — it means "take everything this feed
    # publishes", and the discovery service reads it that way — so it survives the pruning
    # above instead of falling back to the pipeline keywords.
    if "queries" in raw:
        cleaned["queries"] = [
            str(item).strip() for item in (raw["queries"] or []) if str(item or "").strip()
        ]

    for numeric in ("max_results", "freshness_days"):
        if numeric in cleaned:
            try:
                cleaned[numeric] = max(1, int(cleaned[numeric]))
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{numeric} precisa ser um numero inteiro",
                ) from None
    return cleaned


def _serialize_source(source: DiscoverySource) -> dict[str, Any]:
    return {
        "id": str(source.id),
        "pipeline_id": str(source.pipeline_id),
        "kind": source.kind.value,
        "name": source.name,
        "is_active": source.is_active,
        "config": source.config_json or {},
        "created_at": source.created_at,
    }


def _clean(value: str | None) -> str | None:
    """Empty means "no channel", never an empty string that looks configured."""
    cleaned = str(value or "").strip()
    return cleaned or None


def _discovery_metadata(payload: PipelineInput) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "language": payload.language,
            "region": payload.region,
            "freshness_days": payload.freshness_days,
        }.items()
        if value is not None
    }


def _apply_automation(
    db: Session, pipeline: Pipeline, payload: AutomationInput | None
) -> None:
    """Merge the named policy fields into the pipeline's automation config.

    Goes through `AutomationConfig` rather than writing the JSON directly, so the bounds it
    enforces (selection and admission limits, backoff) apply here exactly as they do when the
    scheduler reads it back.
    """
    if payload is None:
        return

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return

    if changes.get("publish_target_id") is not None:
        target = (
            db.query(PublishTarget)
            .filter(PublishTarget.id == changes["publish_target_id"])
            .first()
        )
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="unknown publish target"
            )
        changes["publish_target_id"] = str(target.id)

    current = AutomationConfig.from_pipeline(pipeline).as_dict()
    current.update({key: value for key, value in changes.items() if value is not None})

    metadata = dict(pipeline.metadata_json or {})
    metadata["automation"] = AutomationConfig(**_known(current)).as_dict()
    pipeline.metadata_json = metadata


def _known(values: dict[str, Any]) -> dict[str, Any]:
    fields = set(AutomationConfig.__dataclass_fields__)
    return {key: value for key, value in values.items() if key in fields}


def _counts(db: Session, pipeline_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    if not pipeline_ids:
        return {}

    out: dict[uuid.UUID, dict[str, int]] = {
        pipeline_id: {"sources": 0, "candidates": 0, "selected": 0, "productions": 0,
                      "published": 0}
        for pipeline_id in pipeline_ids
    }

    for pipeline_id, total in (
        db.query(DiscoverySource.pipeline_id, func.count(DiscoverySource.id))
        .filter(DiscoverySource.pipeline_id.in_(pipeline_ids))
        .group_by(DiscoverySource.pipeline_id)
        .all()
    ):
        out[pipeline_id]["sources"] = int(total)

    for pipeline_id, candidate_status, total in (
        db.query(
            VideoCandidate.pipeline_id, VideoCandidate.status, func.count(VideoCandidate.id)
        )
        .filter(VideoCandidate.pipeline_id.in_(pipeline_ids))
        .group_by(VideoCandidate.pipeline_id, VideoCandidate.status)
        .all()
    ):
        out[pipeline_id]["candidates"] += int(total)
        if candidate_status == VideoCandidateStatus.SELECTED:
            out[pipeline_id]["selected"] = int(total)

    for pipeline_id, job_state, total in (
        db.query(PipelineJob.pipeline_id, PipelineJob.state, func.count(PipelineJob.id))
        .filter(PipelineJob.pipeline_id.in_(pipeline_ids))
        .group_by(PipelineJob.pipeline_id, PipelineJob.state)
        .all()
    ):
        out[pipeline_id]["productions"] += int(total)
        if job_state == PipelineState.PUBLISHED:
            out[pipeline_id]["published"] = int(total)

    return out


def _automation_states(
    db: Session, pipeline_ids: list[uuid.UUID]
) -> dict[uuid.UUID, AutomationState]:
    if not pipeline_ids:
        return {}
    rows = (
        db.query(AutomationState)
        .filter(AutomationState.pipeline_id.in_(pipeline_ids))
        .all()
    )
    return {row.pipeline_id: row for row in rows}


def _serialize(
    db: Session,
    pipeline: Pipeline,
    *,
    counts: dict[str, int] | None = None,
    state: AutomationState | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    automation = AutomationConfig.from_pipeline(pipeline).as_dict()
    payload: dict[str, Any] = {
        "id": str(pipeline.id),
        "name": pipeline.name,
        "theme": pipeline.theme,
        # What discovery, selection and the model are actually told. Resolved here so no
        # caller has to reimplement the fallback and get it subtly different.
        "subject": pipeline.subject,
        "description": pipeline.description,
        "keywords": pipeline.keywords_json or [],
        "is_active": pipeline.is_active,
        "telegram_chat_id": pipeline.telegram_chat_id,
        "default_clip_mode": pipeline.default_clip_mode,
        "default_video_ratio": pipeline.default_video_ratio,
        "last_run_at": pipeline.last_run_at,
        "automation": automation,
        "counts": counts or {},
        "created_at": pipeline.created_at,
    }

    if state is not None:
        payload["state"] = {
            "next_due_at": state.next_due_at,
            "last_started_at": state.last_started_at,
            "last_completed_at": state.last_completed_at,
            "last_status": state.last_status,
            "running_since": state.running_since,
            "consecutive_failures": state.consecutive_failures,
        }

    if detail:
        payload["metadata"] = pipeline.metadata_json or {}
        payload["channel"] = _channel(db, automation.get("publish_target_id"))
    return payload


def _channel(db: Session, target_id: str | None) -> dict[str, Any] | None:
    """The channel this pipeline publishes to, by name. Never a credential."""
    if not target_id:
        return None
    try:
        parsed = uuid.UUID(str(target_id))
    except (TypeError, ValueError):
        return None
    target = db.query(PublishTarget).filter(PublishTarget.id == parsed).first()
    if target is None:
        return None
    return {
        "id": str(target.id),
        "name": target.name,
        "channel_title": target.channel_title,
        "platform": target.platform.value if target.platform else None,
        "connection_status": (
            target.connection_status.value if target.connection_status else None
        ),
        "is_publishable": target.is_publishable,
    }
