"""Reading performance and lineage. Admin-only, and read-mostly.

Every route here is behind ``get_current_admin``, like the operational and publishing
surfaces: performance figures name a real channel's videos, and lineage exposes which source
the operation is built on. Neither belongs on a public endpoint.

**What is never returned**, on any route below: the target's refresh token, its ciphertext,
an access token, an upload session URI, or a raw provider error body. The read models are
built from columns chosen for that reason, not filtered afterwards.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.enums import PublishAttemptStatus
from app.models.publish_attempt import PublishAttempt
from app.models.video_performance_snapshot import VideoPerformanceSnapshot
from app.models.user import User
from app.security.auth_middleware import get_current_admin
from app.services.content_lineage_service import ContentLineageService
from app.services.metrics_ingestion_service import YouTubeMetricsIngestionService

router = APIRouter()


def _lineage() -> ContentLineageService:
    return ContentLineageService()


def _ingestion() -> YouTubeMetricsIngestionService:
    return YouTubeMetricsIngestionService()


def _attempt(db: Session, attempt_id: uuid.UUID) -> PublishAttempt:
    attempt = db.get(PublishAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publication not found"
        )
    return attempt


MAX_PAGE_SIZE = 100


@router.get("/admin/published-videos")
def list_published_videos(
    limit: int = Query(default=25, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Every cut that actually reached the channel, newest first.

    This is the list the performance screen is built on. It used to come from the evaluation
    dataset, which resolved five canonical windows per video to answer a question nobody was
    asking yet; what a person opening the screen wants is which videos went out and how they
    are doing, so that is what this returns.

    The counters are the *latest* measurement, carried verbatim from the snapshot. A video
    that has never been measured comes back with nulls, not zeros: never-measured and
    measured-as-zero are different facts, and only one of them is about the video.
    """
    base = (
        db.query(PublishAttempt)
        .filter(
            PublishAttempt.status == PublishAttemptStatus.SUCCEEDED,
            PublishAttempt.external_id.isnot(None),
        )
    )
    total = base.count()
    attempts = (
        base.order_by(PublishAttempt.finished_at.desc().nullslast())
        .offset(offset)
        .limit(limit)
        .all()
    )

    latest = _latest_snapshots(db, [attempt.id for attempt in attempts])
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_serialize_published(attempt, latest.get(attempt.id)) for attempt in attempts],
    }


def _latest_snapshots(db: Session, attempt_ids: list[uuid.UUID]) -> dict:
    """The newest snapshot per publication, in one query rather than one per row."""
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


def _serialize_published(attempt: PublishAttempt, snapshot) -> dict:
    return {
        "attempt_id": str(attempt.id),
        "pipeline_job_id": str(attempt.pipeline_job_id) if attempt.pipeline_job_id else None,
        "external_id": attempt.external_id,
        "external_url": (
            f"https://www.youtube.com/watch?v={attempt.external_id}"
            if attempt.external_id
            else None
        ),
        "media_identity": attempt.media_identity,
        "published_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
        # Absent, not zero, when nothing has been measured yet.
        "measured_at": snapshot.captured_at.isoformat() if snapshot else None,
        "views": snapshot.view_count if snapshot else None,
        "likes": snapshot.like_count if snapshot else None,
        "comments": snapshot.comment_count if snapshot else None,
        "privacy_status": snapshot.privacy_status if snapshot else None,
        "availability": snapshot.availability if snapshot else None,
    }


@router.get("/admin/published-videos/{attempt_id}/performance")
def video_performance(
    attempt_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """The temporal series for one published video.

    Returns the snapshots as recorded, including the ones where nothing could be measured.
    A gap in the series is information — it says the collection did not run or the video was
    not returned — and smoothing it over would hide exactly that.
    """
    return _lineage().performance(db, _attempt(db, attempt_id))


@router.get("/admin/published-videos/{attempt_id}/lineage")
def video_lineage(
    attempt_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Where this video came from: source, candidate, pipeline, job, publication.

    ``complete`` says whether every link is a real foreign key. Links that were never
    recorded come back as ``null`` rather than being reconstructed by matching on titles or
    timestamps, which would produce provenance that looks certain and is a guess.
    """
    return _lineage().lineage(db, _attempt(db, attempt_id))


@router.post("/admin/metrics/youtube/run")
def run_metrics_collection(
    dry_run: bool = Query(
        True,
        description=(
            "Report what would be collected without calling YouTube or writing snapshots."
        ),
    ),
    limit: int | None = Query(None, ge=1, le=1000),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Collect one round now.

    ``dry_run`` defaults to **true**: the safe reading of "run this" is "show me what it
    would do", and a real run spends the channel's YouTube quota. Turning it off is an
    explicit act, the same way every other autonomous behaviour in this system is.
    """
    return _ingestion().run(db, dry_run=dry_run, limit=limit).as_dict()


@router.get("/admin/metrics/status")
def metrics_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Whether collection is enabled, what is being tracked, and when it last worked."""
    return _ingestion().status(db)
