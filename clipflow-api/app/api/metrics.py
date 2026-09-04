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
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.publish_attempt import PublishAttempt
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
    """Where this video came from: source, candidate, topic, job, publication.

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
