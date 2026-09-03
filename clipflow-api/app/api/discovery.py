"""Admin endpoints for discovery.

Discovery is an operator surface, not a customer one: every route requires an admin, reusing
the existing RBAC dependency rather than inventing a second notion of privilege.

The boundary the routes hold: discovery produces `VideoCandidate` rows in `DISCOVERED` and
stops. The one route that can start production — `POST /admin/video-candidates/{id}/select` —
is an explicit human action, marked as such in the audit log, and it goes through the same
service a future selector will use so there is exactly one way a PipelineJob comes into
existence.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db.session import get_db
from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.enums import DiscoverySourceKind, VideoCandidateStatus
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin
from app.services.audit_service import AuditService
from app.services.discovery_service import build_default_service
from app.selection.engine import SelectionEngine
from app.selection.semantic import build_evaluator
from app.services.admission_service import (
    HARD_MAX_ADMISSIONS_PER_RUN,
    ProductionAdmissionService,
)
from app.services.selection_service import (
    HARD_MAX_SELECTED_PER_RUN,
    METHOD_MANUAL,
    SelectionService,
)

router = APIRouter()
audit_service = AuditService()

MAX_PAGE_SIZE = 100


def _service():
    """Built per request so a key rotated in the environment takes effect on restart only —
    and so tests can construct the service directly with stub providers."""
    return build_default_service(
        settings.youtube_api_key,
        timeout_sec=settings.discovery_http_timeout_sec,
        max_results=settings.discovery_max_results,
        freshness_days=settings.discovery_freshness_days,
    )


def _selection_service() -> SelectionService:
    """Built per request so tests can substitute the evaluator, and so a key rotated in the
    environment takes effect on restart rather than being captured at import time."""
    evaluator = build_evaluator(
        settings.selection_openai_api_key,
        model=settings.selection_model,
        timeout_sec=settings.selection_timeout_sec,
    )
    return SelectionService(engine=SelectionEngine(evaluator=evaluator))


def _admission_service() -> ProductionAdmissionService:
    """One service for every admission caller — the run endpoint, the direct endpoint and any
    future scheduler. A second implementation would be a second place for the capacity check
    and the idempotency key to drift apart."""
    return ProductionAdmissionService()


class AdmissionRunInput(BaseModel):
    # Optional: omitting it admits across every topic, which is what a global scheduler wants.
    topic_id: uuid.UUID | None = None
    limit: int | None = Field(default=None, ge=1, le=HARD_MAX_ADMISSIONS_PER_RUN)
    # Committing starts real production and spends GPU time, so it has to be asked for.
    dry_run: bool = True


class SelectionRunInput(BaseModel):
    topic_id: uuid.UUID
    # None means "use the topic's configured cap". Whatever arrives is clamped server-side:
    # this is the last line before automation can act at scale.
    limit: int | None = Field(default=None, ge=1, le=HARD_MAX_SELECTED_PER_RUN)
    # Ranking and reasons with no state change — the way to calibrate the engine against real
    # data without committing to anything.
    dry_run: bool = True
    verbose: bool = False


class TopicInput(BaseModel):
    name: str
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)
    language: str | None = None
    region: str | None = None
    freshness_days: int | None = None
    is_active: bool = True


class SourceInput(BaseModel):
    topic_id: uuid.UUID
    kind: DiscoverySourceKind
    name: str | None = None
    is_active: bool = True
    # Provider-specific: `queries`, `language`, `region`, `max_results`, `freshness_days`
    # for YouTube; `feed_url` for RSS. Never a credential — those live in settings.
    config: dict[str, Any] = Field(default_factory=dict)


class RunInput(BaseModel):
    topic_id: uuid.UUID
    source_id: uuid.UUID | None = None
    max_results: int | None = None
    published_after: datetime | None = None


def serialize_candidate(candidate: VideoCandidate, *, detail: bool = False) -> dict[str, Any]:
    metadata = dict(candidate.metadata_json or {})
    normalized = dict(metadata.get("normalized") or {})
    payload = {
        "id": str(candidate.id),
        "topic_id": str(candidate.topic_id),
        "source_id": str(candidate.source_id) if candidate.source_id else None,
        "status": candidate.status.value,
        "provider": metadata.get("provider"),
        "external_id": candidate.external_id,
        "url": candidate.url,
        "title": candidate.title,
        "channel": candidate.channel,
        "thumbnail_url": candidate.thumbnail_url,
        "duration_sec": candidate.duration_sec,
        "published_at": candidate.published_at,
        "first_discovered_at": candidate.created_at,
        "last_seen_at": candidate.last_seen_at,
        "live_status": normalized.get("live_status"),
        "is_short": normalized.get("is_short"),
        "available": normalized.get("available"),
        "view_count": normalized.get("view_count"),
    }
    if detail:
        payload["dedup_key"] = metadata.get("dedup_key")
        payload["description"] = normalized.get("description")
        payload["channel_id"] = normalized.get("channel_id")
        payload["like_count"] = normalized.get("like_count")
        payload["comment_count"] = normalized.get("comment_count")
        payload["language"] = normalized.get("language")
        payload["unavailable_reason"] = normalized.get("unavailable_reason")
        payload["seen_via"] = metadata.get("seen_via")
        payload["raw_metadata"] = metadata.get("raw")
        payload["selected_at"] = candidate.selected_at
        payload["selection_method"] = (metadata.get("selection") or {}).get("method")
        payload["selection_score"] = (candidate.scores_json or {}).get("final_score")
        # Where this candidate ended up, once it was admitted. Added, not replacing anything.
        payload["production"] = metadata.get("production")
    return payload


# ---------------------------------------------------------------- topics


@router.post("/admin/content-topics")
def create_topic(
    payload: TopicInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    topic = ContentTopic(
        name=payload.name,
        description=payload.description,
        keywords_json=payload.keywords,
        is_active=payload.is_active,
        metadata_json={
            key: value
            for key, value in {
                "language": payload.language,
                "region": payload.region,
                "freshness_days": payload.freshness_days,
            }.items()
            if value is not None
        },
    )
    db.add(topic)
    audit_service.log(
        db,
        action="admin.discovery.topic.create",
        outcome="success",
        actor_user=admin,
        target_type="content_topic",
        metadata={"name": payload.name},
    )
    db.commit()
    db.refresh(topic)
    return _serialize_topic(topic)


@router.get("/admin/content-topics")
def list_topics(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    topics = db.query(ContentTopic).order_by(ContentTopic.created_at.desc()).all()
    return [_serialize_topic(topic) for topic in topics]


# ---------------------------------------------------------------- sources


@router.post("/admin/discovery-sources")
def create_source(
    payload: SourceInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    topic = db.query(ContentTopic).filter(ContentTopic.id == payload.topic_id).first()
    if topic is None:
        raise HTTPException(status_code=404, detail="unknown topic")

    source = DiscoverySource(
        topic_id=topic.id,
        kind=payload.kind,
        name=payload.name,
        is_active=payload.is_active,
        config_json=payload.config,
    )
    db.add(source)
    audit_service.log(
        db,
        action="admin.discovery.source.create",
        outcome="success",
        actor_user=admin,
        target_type="discovery_source",
        metadata={"kind": payload.kind.value, "topic_id": str(topic.id)},
    )
    db.commit()
    db.refresh(source)
    return _serialize_source(source)


@router.get("/admin/discovery-sources")
def list_sources(
    topic_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    query = db.query(DiscoverySource)
    if topic_id:
        query = query.filter(DiscoverySource.topic_id == topic_id)
    sources = query.order_by(DiscoverySource.created_at.desc()).all()
    return [_serialize_source(source) for source in sources]


# ---------------------------------------------------------------- run


@router.post("/admin/discovery/run")
def run_discovery(
    payload: RunInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Run discovery for a topic, or for one of its sources.

    A source that cannot run answers with a classified reason rather than an exception — an
    unconfigured provider is a state to report, not a crash.
    """
    topic = db.query(ContentTopic).filter(ContentTopic.id == payload.topic_id).first()
    if topic is None:
        raise HTTPException(status_code=404, detail="unknown topic")

    service = _service()

    if payload.source_id:
        source = (
            db.query(DiscoverySource)
            .filter(
                DiscoverySource.id == payload.source_id,
                DiscoverySource.topic_id == topic.id,
            )
            .first()
        )
        if source is None:
            raise HTTPException(status_code=404, detail="unknown source for this topic")
        results = [
            service.run_source(
                db,
                topic=topic,
                source=source,
                max_results=payload.max_results,
                published_after=payload.published_after,
            )
        ]
    else:
        results = service.run_topic(
            db,
            topic=topic,
            max_results=payload.max_results,
            published_after=payload.published_after,
        )

    audit_service.log(
        db,
        action="admin.discovery.run",
        outcome="success",
        actor_user=admin,
        target_type="content_topic",
        target_id=str(topic.id),
        metadata={
            "source_id": str(payload.source_id) if payload.source_id else None,
            "runs": len(results),
            "new_candidates": sum(r.new_candidates for r in results),
        },
    )
    db.commit()

    return {
        "topic_id": str(topic.id),
        "runs": [result.as_dict() for result in results],
        "totals": {
            "results_received": sum(r.results_received for r in results),
            "new_candidates": sum(r.new_candidates for r in results),
            "existing_candidates": sum(r.existing_candidates for r in results),
            "api_calls": sum(r.api_calls for r in results),
        },
    }


# ---------------------------------------------------------------- selection


@router.post("/admin/selection/run")
def run_selection(
    payload: SelectionRunInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Rank a topic's candidates and, unless this is a dry run, mark the winners SELECTED.

    Defaults to ``dry_run=true``. Committing is the exception that has to be asked for, not
    the default a mistyped request falls into.

    **No PipelineJob is created and nothing is enqueued.** Selection is an editorial decision;
    admitting it into production is the next PR's boundary, so enabling automatic selection
    cannot by itself start spending GPU time.
    """
    topic = db.query(ContentTopic).filter(ContentTopic.id == payload.topic_id).first()
    if topic is None:
        raise HTTPException(status_code=404, detail="unknown topic")

    report = _selection_service().run(
        db, topic=topic, limit=payload.limit, dry_run=payload.dry_run
    )

    audit_service.log(
        db,
        action="admin.selection.run",
        outcome="success",
        actor_user=admin,
        target_type="content_topic",
        target_id=str(topic.id),
        metadata={
            "selection_run_id": report.run_id,
            "dry_run": payload.dry_run,
            "selected": len(report.outcome.selected),
            "committed": report.committed,
        },
    )
    db.commit()

    return report.as_dict(verbose=payload.verbose)


# ---------------------------------------------------------------- admission


@router.post("/admin/admission/run")
def run_admission(
    payload: AdmissionRunInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admit selected candidates into production.

    Selection decided *what* is worth producing; this decides *whether we can start now* —
    capacity, the day's budget, and whether the run already exists. Defaults to a dry run:
    committing creates PipelineJobs and puts real work on the queue.
    """
    topic = None
    if payload.topic_id:
        topic = db.query(ContentTopic).filter(ContentTopic.id == payload.topic_id).first()
        if topic is None:
            raise HTTPException(status_code=404, detail="unknown topic")

    report = _admission_service().run(
        db,
        topic=topic,
        limit=payload.limit,
        dry_run=payload.dry_run,
        actor=str(admin.id),
    )

    audit_service.log(
        db,
        action="admin.admission.run",
        outcome="success",
        actor_user=admin,
        target_type="content_topic",
        target_id=str(topic.id) if topic else None,
        metadata={
            "admission_run_id": report.run_id,
            "dry_run": payload.dry_run,
            "admitted": report.as_dict()["counts"]["admitted"],
            "active_jobs": report.active_jobs,
        },
    )
    db.commit()
    return report.as_dict()


@router.post("/admin/video-candidates/{candidate_id}/admit")
def admit_candidate(
    candidate_id: uuid.UUID,
    dry_run: bool = False,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admit one named candidate.

    Calls the same service as the run endpoint, with the same idempotency key and the same
    capacity check — an operator shortcut, not a second code path around the limits.
    """
    candidate = db.query(VideoCandidate).filter(VideoCandidate.id == candidate_id).first()
    if candidate is None:
        raise HTTPException(status_code=404, detail="unknown candidate")

    decision = _admission_service().admit_candidate(
        db, candidate=candidate, dry_run=dry_run, actor=str(admin.id)
    )

    audit_service.log(
        db,
        action="admin.admission.candidate",
        outcome="success" if decision.outcome in ("admitted", "already_admitted") else "failed",
        actor_user=admin,
        target_type="video_candidate",
        target_id=str(candidate.id),
        metadata=decision.as_dict(),
    )
    db.commit()
    db.refresh(candidate)
    return {**decision.as_dict(), "candidate": serialize_candidate(candidate, detail=True)}


@router.post("/admin/admission/retry-pending")
def retry_pending_enqueue(
    limit: int = Query(default=10, ge=1, le=HARD_MAX_ADMISSIONS_PER_RUN),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Re-dispatch admissions that persisted but never reached the queue.

    The recovery for a Redis outage during admission: the runs exist, their payloads do not,
    and this republishes them. Idempotent — the admission key prevents a duplicate run — and
    bounded, because an unattended retry loop against a broken queue is a republish storm.
    """
    decisions = _admission_service().retry_pending_enqueue(db, limit=limit)
    audit_service.log(
        db,
        action="admin.admission.retry_pending",
        outcome="success",
        actor_user=admin,
        target_type="pipeline_job",
        metadata={"recovered": sum(1 for d in decisions if d.outcome == "admitted")},
    )
    db.commit()
    return {
        "recovered": [d.as_dict() for d in decisions if d.outcome == "admitted"],
        "still_failing": [d.as_dict() for d in decisions if d.outcome != "admitted"],
    }


# ---------------------------------------------------------------- candidates


@router.get("/admin/video-candidates")
def list_candidates(
    topic_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    status: VideoCandidateStatus | None = None,
    min_score: float | None = Query(default=None, ge=0.0, le=1.0),
    selection_method: str | None = None,
    published_after: datetime | None = None,
    published_before: datetime | None = None,
    discovered_after: datetime | None = None,
    discovered_before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Paginated, newest first.

    Ordered by ``published_at DESC`` with ``created_at`` as the tiebreaker: a feed can return
    items with no publish date, and those would otherwise sort unpredictably. This is
    recency, not ranking — there is no scoring in this PR.
    """
    query = db.query(VideoCandidate)
    if topic_id:
        query = query.filter(VideoCandidate.topic_id == topic_id)
    if source_id:
        query = query.filter(VideoCandidate.source_id == source_id)
    if status:
        query = query.filter(VideoCandidate.status == status)
    if min_score is not None:
        # relevance_score is a real column, so this filters in the database rather than
        # loading every row to compare a JSON field in Python.
        query = query.filter(VideoCandidate.relevance_score >= min_score)
    if published_after:
        query = query.filter(VideoCandidate.published_at >= published_after)
    if published_before:
        query = query.filter(VideoCandidate.published_at <= published_before)
    if discovered_after:
        query = query.filter(VideoCandidate.created_at >= discovered_after)
    if discovered_before:
        query = query.filter(VideoCandidate.created_at <= discovered_before)

    if selection_method:
        candidates_all = query.all()
        matching = [
            row.id
            for row in candidates_all
            if ((row.metadata_json or {}).get("selection") or {}).get("method")
            == selection_method
        ]
        query = query.filter(VideoCandidate.id.in_(matching or [uuid.uuid4()]))

    total = query.count()
    candidates = (
        query.order_by(
            VideoCandidate.published_at.desc().nullslast(),
            VideoCandidate.created_at.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [serialize_candidate(candidate) for candidate in candidates],
    }


@router.get("/admin/video-candidates/{candidate_id}")
def get_candidate(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    candidate = db.query(VideoCandidate).filter(VideoCandidate.id == candidate_id).first()
    if candidate is None:
        raise HTTPException(status_code=404, detail="unknown candidate")
    return serialize_candidate(candidate, detail=True)


@router.post("/admin/video-candidates/{candidate_id}/select")
def select_candidate(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Mark a candidate SELECTED by hand. **Selection only.**

    Until PR-ADMISSION-01 this route also created a PipelineJob — and never enqueued it, so
    every manual selection left a run that no worker would ever claim. Selection and admission
    are separate decisions, and mixing them produced exactly the orphaned job the admission
    service now exists to prevent. Starting production is
    ``POST /admin/video-candidates/{id}/admit``.

    A human may bypass the selection *policy* — caps, cooldown, score thresholds — but not the
    eligibility invariants, which is why the checks below stay in front of it.
    """
    candidate = db.query(VideoCandidate).filter(VideoCandidate.id == candidate_id).first()
    if candidate is None:
        raise HTTPException(status_code=404, detail="unknown candidate")
    if candidate.status == VideoCandidateStatus.SELECTED:
        raise HTTPException(status_code=409, detail="candidate already selected")
    if candidate.status == VideoCandidateStatus.CONSUMED:
        raise HTTPException(status_code=409, detail="candidate already admitted to production")
    if candidate.status == VideoCandidateStatus.REJECTED:
        raise HTTPException(status_code=409, detail="candidate was rejected")

    now = datetime.now(timezone.utc)
    candidate.status = VideoCandidateStatus.SELECTED
    candidate.selected_at = now
    metadata = dict(candidate.metadata_json or {})
    metadata["selection"] = {
        "method": METHOD_MANUAL,
        "selected_by": str(admin.id),
        "selected_at": now.isoformat(),
    }
    candidate.metadata_json = metadata

    audit_service.log(
        db,
        action="admin.discovery.candidate.select",
        outcome="success",
        actor_user=admin,
        target_type="video_candidate",
        target_id=str(candidate.id),
        metadata={"method": METHOD_MANUAL},
    )
    db.commit()
    db.refresh(candidate)

    return {
        "candidate": serialize_candidate(candidate, detail=True),
        "admitted": False,
        "note": "selected only; admission is POST /admin/video-candidates/{id}/admit",
    }


def _serialize_topic(topic: ContentTopic) -> dict[str, Any]:
    return {
        "id": str(topic.id),
        "name": topic.name,
        "description": topic.description,
        "keywords": topic.keywords_json or [],
        "is_active": topic.is_active,
        "default_clip_mode": topic.default_clip_mode,
        "default_video_ratio": topic.default_video_ratio,
        "last_run_at": topic.last_run_at,
        "metadata": topic.metadata_json or {},
    }


def _serialize_source(source: DiscoverySource) -> dict[str, Any]:
    return {
        "id": str(source.id),
        "topic_id": str(source.topic_id),
        "kind": source.kind.value,
        "name": source.name,
        "is_active": source.is_active,
        "config": source.config_json or {},
    }
