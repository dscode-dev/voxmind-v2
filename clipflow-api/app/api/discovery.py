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
from datetime import datetime
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
from app.services.pipeline_job_service import PipelineJobService

router = APIRouter()
audit_service = AuditService()
pipeline_job_service = PipelineJobService()

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


# ---------------------------------------------------------------- candidates


@router.get("/admin/video-candidates")
def list_candidates(
    topic_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    status: VideoCandidateStatus | None = None,
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
    if published_after:
        query = query.filter(VideoCandidate.published_at >= published_after)
    if published_before:
        query = query.filter(VideoCandidate.published_at <= published_before)
    if discovered_after:
        query = query.filter(VideoCandidate.created_at >= discovered_after)
    if discovered_before:
        query = query.filter(VideoCandidate.created_at <= discovered_before)

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
    """Promote a candidate into a real run. **Explicitly a human action.**

    This is not a selection engine and does not pretend to be one: it applies no policy, no
    scoring and no ranking. It exists so the discovery-to-production boundary can be
    exercised end to end before an automatic selector exists, and it creates the PipelineJob
    through the same service that selector will use — so there stays exactly one way a run
    begins.

    Discovery itself never calls this.
    """
    candidate = db.query(VideoCandidate).filter(VideoCandidate.id == candidate_id).first()
    if candidate is None:
        raise HTTPException(status_code=404, detail="unknown candidate")
    if candidate.status == VideoCandidateStatus.SELECTED:
        raise HTTPException(status_code=409, detail="candidate already selected")
    if candidate.status == VideoCandidateStatus.REJECTED:
        raise HTTPException(status_code=409, detail="candidate was rejected")

    topic = db.query(ContentTopic).filter(ContentTopic.id == candidate.topic_id).first()
    worker_job_id = str(uuid.uuid4())

    run = pipeline_job_service.create_for_enqueue(
        db,
        worker_job_id=worker_job_id,
        source_url=candidate.url,
        pipeline_stage="prepare",
        clip_mode=(topic.default_clip_mode if topic else "short_serie"),
        video_ratio=(topic.default_video_ratio if topic else "portrait"),
        origin="manual_selection",
        metadata={
            "video_candidate_id": str(candidate.id),
            "topic_id": str(candidate.topic_id),
            "selected_by": str(admin.id),
        },
        commit=False,
    )
    run.topic_id = candidate.topic_id
    run.candidate_id = candidate.id

    candidate.status = VideoCandidateStatus.SELECTED
    candidate.selected_at = run.queued_at

    audit_service.log(
        db,
        action="admin.discovery.candidate.select",
        outcome="success",
        actor_user=admin,
        target_type="video_candidate",
        target_id=str(candidate.id),
        metadata={"pipeline_job_id": str(run.id), "worker_job_id": worker_job_id},
    )
    db.commit()
    db.refresh(candidate)

    # The run is created and QUEUED in the state machine, but NOT pushed to Redis here: this
    # endpoint proves the boundary, it is not the production entry point.
    return {
        "candidate": serialize_candidate(candidate, detail=True),
        "pipeline_job_id": str(run.id),
        "worker_job_id": worker_job_id,
        "enqueued": False,
        "note": "run created in QUEUED; dispatch is the selector's job, not discovery's",
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
