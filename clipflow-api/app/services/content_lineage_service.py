"""Answering "where did this video come from?" and "what happened to it?".

Two read models over data other PRs already wrote. Nothing here creates, scores or decides
anything — it is the join that was always implicit in the schema, made explicit so a person
can follow one published video back to the source item that produced it without opening five
tables by hand.

**No invented links.** A publication whose job has no candidate has an *unknown* origin, and
that is what is reported. The tempting alternative — matching on title, or on the nearest
candidate by time — would manufacture provenance that looks authoritative and is a guess.
Lineage is only worth having if every link in it is a foreign key somebody actually wrote.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt
from app.models.video_candidate import VideoCandidate
from app.models.video_performance_snapshot import VideoPerformanceSnapshot


class ContentLineageService:
    # ------------------------------------------------------------------ lineage

    def lineage(self, db: Session, attempt: PublishAttempt) -> dict[str, Any]:
        """The full chain: source, candidate, topic, job, publication, video.

        Walked backwards from the publication, because that is the direction the question is
        always asked in: someone is looking at a video and wants to know how it got there.
        """
        job: PipelineJob | None = attempt.job
        candidate: VideoCandidate | None = job.candidate if job else None
        topic: ContentTopic | None = None
        source: DiscoverySource | None = None

        if candidate is not None:
            topic = candidate.topic
            source = candidate.source
        elif job is not None and job.topic_id is not None:
            # The job knows its topic even when the candidate link is missing (a manually
            # created job, for instance). Reported as the partial truth it is.
            topic = db.get(ContentTopic, job.topic_id)

        complete = all(item is not None for item in (job, candidate, topic))

        return {
            "publish_attempt_id": str(attempt.id),
            # Whether every link in the chain is a real foreign key. A consumer that needs
            # provenance it can rely on should check this rather than the presence of the
            # fields below, which are populated only as far as the data actually goes.
            "complete": complete,
            "source": self._source(source),
            "candidate": self._candidate(candidate),
            "topic": self._topic(topic),
            "job": self._job(job),
            "publication": self._publication(attempt),
        }

    @staticmethod
    def _source(source: DiscoverySource | None) -> dict[str, Any] | None:
        if source is None:
            return None
        return {
            "discovery_source_id": str(source.id),
            "kind": source.kind.value if source.kind else None,
            "name": source.name,
            # config_json can hold the source's provider credentials. Never serialised.
        }

    @staticmethod
    def _candidate(candidate: VideoCandidate | None) -> dict[str, Any] | None:
        if candidate is None:
            return None
        return {
            "video_candidate_id": str(candidate.id),
            "external_id": candidate.external_id,
            "url": candidate.url,
            "title": candidate.title,
            "channel": candidate.channel,
            "duration_sec": candidate.duration_sec,
            "published_at": _iso(candidate.published_at),
            "status": candidate.status.value if candidate.status else None,
            # The scores that led to selection, reported as history: what this candidate was
            # judged to be worth at the time. Nothing in the metrics path feeds back into
            # them, which is the whole point of this PR.
            "relevance_score": _number(candidate.relevance_score),
            "trend_score": _number(candidate.trend_score),
            "scores": candidate.scores_json,
            "selected_at": _iso(candidate.selected_at),
        }

    @staticmethod
    def _topic(topic: ContentTopic | None) -> dict[str, Any] | None:
        if topic is None:
            return None
        return {"content_topic_id": str(topic.id), "name": topic.name}

    @staticmethod
    def _job(job: PipelineJob | None) -> dict[str, Any] | None:
        if job is None:
            return None
        return {
            "pipeline_job_id": str(job.id),
            "state": job.state.value if job.state else None,
            "clip_mode": job.clip_mode,
            "video_ratio": job.video_ratio,
            "finished_at": _iso(job.finished_at),
        }

    @staticmethod
    def _publication(attempt: PublishAttempt) -> dict[str, Any]:
        return {
            "publish_attempt_id": str(attempt.id),
            "publish_target_id": str(attempt.target_id),
            "target_name": attempt.target.name if attempt.target else None,
            "status": attempt.status.value if attempt.status else None,
            "initiator": attempt.initiator,
            "external_video_id": attempt.external_id,
            "video_url": _watch_url(attempt.external_id),
            "media_identity": attempt.media_identity,
            "published_at": _iso(attempt.finished_at),
            # Deliberately absent: upload_session_uri_encrypted, the full payload, and
            # anything derived from the target's credential.
        }

    # -------------------------------------------------------------- performance

    def performance(self, db: Session, attempt: PublishAttempt) -> dict[str, Any]:
        """The temporal series for one published video, oldest first."""
        snapshots = (
            db.query(VideoPerformanceSnapshot)
            .filter(VideoPerformanceSnapshot.publish_attempt_id == attempt.id)
            .order_by(VideoPerformanceSnapshot.captured_at.asc())
            .all()
        )

        series = [self._point(snapshot) for snapshot in snapshots]
        measured = [item for item in snapshots if item.availability == "ok"]

        return {
            "publish_attempt_id": str(attempt.id),
            "external_video_id": attempt.external_id,
            "video_url": _watch_url(attempt.external_id),
            "published_at": _iso(attempt.finished_at),
            "snapshot_count": len(series),
            "first_captured_at": _iso(snapshots[0].captured_at) if snapshots else None,
            "latest_captured_at": _iso(snapshots[-1].captured_at) if snapshots else None,
            # The latest *measurement*, which is not always the latest snapshot: a video that
            # has since gone private still has a last known figure, and reporting the
            # not_returned row's NULLs as the current value would look like a collapse to
            # zero.
            "latest": self._point(measured[-1]) if measured else None,
            "series": series,
        }

    @staticmethod
    def _point(snapshot: VideoPerformanceSnapshot) -> dict[str, Any]:
        return {
            "captured_at": _iso(snapshot.captured_at),
            "capture_slot": snapshot.capture_slot,
            "availability": snapshot.availability,
            "privacy_status": snapshot.privacy_status,
            # NULL stays NULL all the way to the API. A consumer that wants to draw a zero
            # can decide that for itself; the read model will not decide it for them.
            "view_count": snapshot.view_count,
            "like_count": snapshot.like_count,
            "comment_count": snapshot.comment_count,
        }


# --------------------------------------------------------------------- helpers


def _watch_url(external_id: str | None) -> str | None:
    return f"https://www.youtube.com/watch?v={external_id}" if external_id else None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.isoformat()


def _number(value: Any) -> float | None:
    """Numeric columns come back as Decimal, and JSON has no Decimal."""
    return float(value) if value is not None else None
