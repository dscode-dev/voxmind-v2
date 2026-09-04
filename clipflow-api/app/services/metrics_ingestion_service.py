"""Collecting what happened to published videos, and nothing more.

**Measurement, not optimization.** Nothing here reads a selection score and nothing here
writes one. No figure computed in this module reaches discovery, selection, admission, QA or
the publication policy — there is no feedback loop, deliberately, because a loop built before
the data exists optimises against a guess. This PR builds the empirical base; deciding what to
do with it is a later, separate argument.

**Never a production gate.** If Google is down, or a token expired, or this whole module
raises, discovery still runs, selection still ranks, admission still admits and the publisher
still publishes. Metrics are an observation of the system, not part of it.

**Grouped by target, because credentials are.** One refresh token per channel: a batch may
only contain videos from the channel whose token is being used. That is why collection is
per-target rather than one flat list of ids.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.metrics.contracts import (
    NOT_RETURNED,
    MetricsAuthError,
    VideoMetrics,
    VideoMetricsProvider,
)
from app.metrics.youtube_metrics import YouTubeVideoMetricsProvider
from app.models.enums import (
    PipelineEventType,
    PublishAttemptStatus,
    PublishTargetConnectionStatus,
)
from app.models.publish_attempt import PublishAttempt
from app.models.publish_target import PublishTarget
from app.models.video_performance_snapshot import VideoPerformanceSnapshot
from app.services import event_bus
from app.services.publish_target_service import (
    PublishTargetService,
    TargetNotPublishableError,
)

logger = logging.getLogger(__name__)

# Not the publisher's lock and not the budget's: this one only prevents two replicas spending
# quota on the same batch. A duplicated fetch is waste, not corruption - the unique capture
# slot makes the database correct either way.
METRICS_LOCK_KEY = 9_140_002

TARGET_UNAVAILABLE = "target_unavailable"
AUTH_FAILED = "auth_failed"
PROVIDER_ERROR = "provider_error"


@dataclass
class TargetResult:
    target_id: str
    target_name: str
    status: str = "ok"
    reason: str | None = None
    videos_due: int = 0
    videos_requested: int = 0
    videos_returned: int = 0
    snapshots_created: int = 0
    duplicates_skipped: int = 0
    missing: int = 0
    failed: int = 0
    provider_calls: int = 0
    video_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "publish_target_id": self.target_id,
            "target_name": self.target_name,
            "status": self.status,
            "reason": self.reason,
            "videos_due": self.videos_due,
            "videos_requested": self.videos_requested,
            "videos_returned": self.videos_returned,
            "snapshots_created": self.snapshots_created,
            "duplicates_skipped": self.duplicates_skipped,
            "missing": self.missing,
            "failed": self.failed,
            "provider_calls": self.provider_calls,
        }


@dataclass
class MetricsRunReport:
    metrics_run_id: str
    dry_run: bool
    status: str = "noop"
    targets: list[TargetResult] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def snapshots_created(self) -> int:
        return sum(target.snapshots_created for target in self.targets)

    @property
    def videos_due(self) -> int:
        return sum(target.videos_due for target in self.targets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics_run_id": self.metrics_run_id,
            "dry_run": self.dry_run,
            "status": self.status,
            "videos_due": self.videos_due,
            "snapshots_created": self.snapshots_created,
            "targets": [target.as_dict() for target in self.targets],
            "duration_ms": self.duration_ms,
        }


class YouTubeMetricsIngestionService:
    def __init__(
        self,
        provider: VideoMetricsProvider | None = None,
        targets: PublishTargetService | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider or YouTubeVideoMetricsProvider()
        self.targets = targets or PublishTargetService()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------------------------------------------------------------------- run

    def run(
        self, db: Session, *, dry_run: bool = True, limit: int | None = None
    ) -> MetricsRunReport:
        """Collect one round of metrics for every publication that is due."""
        started = time.monotonic()
        report = MetricsRunReport(metrics_run_id=str(uuid.uuid4()), dry_run=dry_run)

        with self._collection_lock(db, dry_run=dry_run) as acquired:
            if not acquired:
                # Another replica is already collecting. Skipped rather than queued: the
                # next tick is minutes away and a duplicated round would only spend quota
                # to write observations the winner is writing right now.
                report.status = "skipped_locked"
                report.duration_ms = _ms(started)
                return report
            self._run_locked(db, report, dry_run=dry_run, limit=limit)

        report.duration_ms = _ms(started)
        self._log(report)
        return report

    @contextmanager
    def _collection_lock(self, db: Session, *, dry_run: bool):
        """Try for the collection lock; never wait for it.

        ``try`` rather than a blocking acquire, unlike the autopublish budget: a replica that
        loses that race is entitled to its allocation and must wait for the truth, whereas a
        replica that loses this one has nothing to do — the winner is collecting the same
        publications into the same capture slots. Waiting would just line up a redundant
        round behind the useful one.

        A dry run takes no lock at all: it reads and reports without touching quota or rows,
        so it must not be able to block a real collection.
        """
        bind = db.bind
        if dry_run or bind is None or bind.dialect.name != "postgresql":
            # SQLite (the test harness) serialises writers by itself.
            yield True
            return

        connection = bind.connect()
        try:
            acquired = bool(
                connection.exec_driver_sql(
                    f"SELECT pg_try_advisory_lock({METRICS_LOCK_KEY})"
                ).scalar()
            )
        except DBAPIError:
            # The lock could not even be asked for. Metrics are never a production gate, so
            # this is a skipped round, not an error anybody is woken up for.
            connection.close()
            logger.warning("metrics_lock_unavailable")
            yield False
            return

        try:
            yield acquired
        finally:
            try:
                if acquired:
                    connection.exec_driver_sql(
                        f"SELECT pg_advisory_unlock({METRICS_LOCK_KEY})"
                    )
                    connection.exec_driver_sql("SELECT pg_advisory_unlock_all()")
            except DBAPIError:
                logger.warning("metrics_unlock_failed")
            finally:
                connection.close()

    def _run_locked(
        self,
        db: Session,
        report: MetricsRunReport,
        *,
        dry_run: bool,
        limit: int | None,
    ) -> None:
        now = self._clock()
        # Bounded by default so a backlog drains over several ticks instead of emptying the
        # day's quota in one.
        limit = limit or settings.metrics_max_videos_per_run

        due = self.due_publications(db, now=now, limit=limit)
        if not due:
            report.status = "noop"
            return

        if not dry_run:
            self._emit(db, "metrics.collection_started", report,
                       {"videos_due": sum(len(v) for v in due.values())})

        for target, attempts in due.items():
            report.targets.append(
                self._collect_target(db, target, attempts, now=now, dry_run=dry_run,
                                     report=report)
            )

        report.status = self._status(report)

        if not dry_run:
            self._emit(db, "metrics.collection_completed", report, {
                "snapshots_created": report.snapshots_created,
                "targets": len(report.targets),
            })

    # -------------------------------------------------------------- due query

    def due_publications(
        self, db: Session, *, now: datetime | None = None, limit: int | None = None
    ) -> dict[PublishTarget, list[PublishAttempt]]:
        """Successful publications whose next observation is due, grouped by target.

        One query for the publications and one for their latest snapshot times, joined in
        memory. Not a per-publication "when was this last collected?" - that is the N+1 this
        is written to avoid, and it would be one query per video on every tick.
        """
        now = now or self._clock()
        horizon = now - timedelta(days=max(1, settings.metrics_tracking_days))

        attempts = (
            db.query(PublishAttempt)
            .filter(
                # Only real publications. A PENDING, UNKNOWN or FAILED attempt has no video
                # to measure, and asking about one would spend quota to learn nothing.
                PublishAttempt.status == PublishAttemptStatus.SUCCEEDED,
                PublishAttempt.external_id.isnot(None),
                PublishAttempt.finished_at.isnot(None),
            )
            .all()
        )
        attempts = [a for a in attempts if _as_utc(a.finished_at) >= horizon]
        if not attempts:
            return {}

        latest = self._latest_capture_times(db, [a.id for a in attempts])

        grouped: dict[PublishTarget, list[PublishAttempt]] = {}
        for attempt in sorted(attempts, key=lambda a: _as_utc(a.finished_at), reverse=True):
            if not self._is_due(attempt, latest.get(attempt.id), now=now):
                continue
            target = attempt.target
            if target is None:
                continue
            grouped.setdefault(target, []).append(attempt)

        if limit:
            # Bounded for a manual run, newest first.
            remaining = limit
            bounded: dict[PublishTarget, list[PublishAttempt]] = {}
            for target, items in grouped.items():
                if remaining <= 0:
                    break
                bounded[target] = items[:remaining]
                remaining -= len(bounded[target])
            return bounded
        return grouped

    @staticmethod
    def _latest_capture_times(db: Session, attempt_ids: list[Any]) -> dict[Any, datetime]:
        """One aggregate query for every publication's most recent observation."""
        if not attempt_ids:
            return {}
        rows = (
            db.query(
                VideoPerformanceSnapshot.publish_attempt_id,
                func.max(VideoPerformanceSnapshot.captured_at),
            )
            .filter(VideoPerformanceSnapshot.publish_attempt_id.in_(attempt_ids))
            .group_by(VideoPerformanceSnapshot.publish_attempt_id)
            .all()
        )
        return {attempt_id: _as_utc(captured) for attempt_id, captured in rows if captured}

    def _is_due(
        self, attempt: PublishAttempt, last_capture: datetime | None, *, now: datetime
    ) -> bool:
        """Declarative cadence: newer videos move faster, so they are watched more closely.

        Deliberately a fixed table rather than an adaptive scheduler. Quota is the binding
        constraint and a simple rule is one an operator can predict; an adaptive one would be
        a second system to reason about before anyone has looked at the first day of data.
        """
        if last_capture is None:
            return True
        age = now - _as_utc(attempt.finished_at)
        interval = _interval_for(age)
        return (now - last_capture) >= interval

    # ---------------------------------------------------------------- one target

    def _collect_target(
        self,
        db: Session,
        target: PublishTarget,
        attempts: list[PublishAttempt],
        *,
        now: datetime,
        dry_run: bool,
        report: MetricsRunReport,
    ) -> TargetResult:
        result = TargetResult(
            target_id=str(target.id), target_name=target.name,
            videos_due=len(attempts),
            video_ids=[a.external_id for a in attempts if a.external_id],
        )

        if target.connection_status != PublishTargetConnectionStatus.CONNECTED or (
            not target.refresh_token_encrypted
        ):
            # No credential, so nothing can be read. Reported and skipped: one broken channel
            # must not cost the others their collection.
            result.status = "skipped"
            result.reason = TARGET_UNAVAILABLE
            return result

        if dry_run:
            result.status = "would_collect"
            result.videos_requested = len(result.video_ids)
            return result

        try:
            credential = self.targets.credential_for(target)
        except TargetNotPublishableError:
            result.status = "skipped"
            result.reason = TARGET_UNAVAILABLE
            return result

        result.videos_requested = len(result.video_ids)
        try:
            fetched = self.provider.fetch_metrics(result.video_ids, credential=credential)
        except MetricsAuthError as exc:
            # The same verdict the publisher reaches, through the same service: one notion of
            # a broken credential, not two that can disagree.
            if not exc.recoverable:
                self.targets.mark_reconnect_required(db, target, error_code=exc.code)
                db.commit()
            result.status = "failed"
            result.reason = AUTH_FAILED
            result.failed = len(result.video_ids)
            self._emit(db, "metrics.collection_failed", report, {
                "publish_target_id": str(target.id), "reason": AUTH_FAILED,
                "error_code": exc.code,
            })
            return result
        except Exception as exc:  # noqa: BLE001
            # Metrics are never a production gate. Only the type is kept - a provider
            # message can echo the request.
            logger.exception(
                "metrics_provider_crashed",
                extra={"publish_target_id": str(target.id),
                       "error_type": type(exc).__name__},
            )
            result.status = "failed"
            result.reason = PROVIDER_ERROR
            result.failed = len(result.video_ids)
            return result

        result.provider_calls = fetched.calls
        result.videos_returned = fetched.returned
        if fetched.error_code:
            result.reason = fetched.error_code

        for attempt in attempts:
            metrics = fetched.metrics.get(attempt.external_id)
            if metrics is None:
                metrics = VideoMetrics(external_video_id=attempt.external_id or "",
                                       availability=NOT_RETURNED)
            created = self._persist(db, attempt, target, metrics, now=now)
            if created:
                result.snapshots_created += 1
            else:
                result.duplicates_skipped += 1
            if metrics.availability != "ok":
                result.missing += 1

        db.commit()
        result.status = "ok" if result.snapshots_created else "noop"
        return result

    # ------------------------------------------------------------- persistence

    def _persist(
        self,
        db: Session,
        attempt: PublishAttempt,
        target: PublishTarget,
        metrics: VideoMetrics,
        *,
        now: datetime,
    ) -> bool:
        """Append one observation. Returns whether a new row was written.

        Each row is committed within its own savepoint so one bad item cannot discard the
        valid siblings from the same batch — a provider that returns 49 good videos and one
        surprise should leave 49 observations behind, not none.
        """
        snapshot = VideoPerformanceSnapshot(
            publish_attempt_id=attempt.id,
            publish_target_id=target.id,
            external_video_id=attempt.external_id or metrics.external_video_id,
            provider=getattr(self.provider, "provider", "youtube"),
            captured_at=now,
            capture_slot=capture_slot(now),
            view_count=metrics.view_count,
            like_count=metrics.like_count,
            comment_count=metrics.comment_count,
            availability=metrics.availability,
            privacy_status=metrics.privacy_status,
            provider_metadata_json=metrics.provider_metadata or None,
        )
        try:
            with db.begin_nested():
                db.add(snapshot)
        except IntegrityError:
            # The slot is already recorded: this publication was collected in this hour by
            # another replica or an earlier run. The observation exists, so there is nothing
            # to do and nothing wrong.
            if snapshot in db:
                db.expunge(snapshot)
            return False
        except DBAPIError:
            logger.warning(
                "metrics_snapshot_rejected",
                extra={"publish_attempt_id": str(attempt.id)},
            )
            return False
        return True

    # ------------------------------------------------------------------- status

    def status(self, db: Session) -> dict[str, Any]:
        """The read model: what is tracked, what is due, and when it last worked."""
        now = self._clock()
        horizon = now - timedelta(days=max(1, settings.metrics_tracking_days))

        tracked = (
            db.query(func.count(PublishAttempt.id))
            .filter(
                PublishAttempt.status == PublishAttemptStatus.SUCCEEDED,
                PublishAttempt.external_id.isnot(None),
                PublishAttempt.finished_at >= horizon.replace(tzinfo=None),
            )
            .scalar() or 0
        )
        latest = (
            db.query(func.max(VideoPerformanceSnapshot.captured_at)).scalar()
        )
        due = self.due_publications(db, now=now)

        targets = db.query(PublishTarget).all()
        return {
            "enabled": settings.metrics_collection_enabled,
            "provider": getattr(self.provider, "provider", "youtube"),
            "tracking_days": settings.metrics_tracking_days,
            "tracked_videos": int(tracked),
            "due_now": sum(len(items) for items in due.values()),
            "total_snapshots": int(
                db.query(func.count(VideoPerformanceSnapshot.id)).scalar() or 0
            ),
            "latest_capture_at": _iso(_as_utc(latest)) if latest else None,
            "cadence": [
                {"age_hours_under": 24, "every_hours": settings.metrics_interval_fresh_hours},
                {"age_hours_under": 24 * 7, "every_hours": settings.metrics_interval_recent_hours},
                {"age_hours_under": None, "every_hours": settings.metrics_interval_mature_hours},
            ],
            "targets": [
                {
                    "publish_target_id": str(target.id),
                    "name": target.name,
                    # No token, no ciphertext, no channel credential - only whether it can
                    # currently be read from.
                    "available": target.connection_status
                    == PublishTargetConnectionStatus.CONNECTED
                    and bool(target.refresh_token_encrypted),
                    "connection_status": target.connection_status.value,
                }
                for target in targets
            ],
        }

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _status(report: MetricsRunReport) -> str:
        if any(target.status == "failed" for target in report.targets):
            return "partial" if report.snapshots_created else "failed"
        if report.snapshots_created:
            return "completed"
        return "noop"

    def _emit(self, db: Session, stage: str, report: MetricsRunReport,
              payload: dict[str, Any]) -> None:
        try:
            event_bus.publish_event(
                db,
                service="metrics",
                event_type=PipelineEventType.INFO,
                pipeline_job_id=None,
                stage=stage,
                message=f"{stage} ({report.metrics_run_id[:8]})",
                payload={"metrics_run_id": report.metrics_run_id, **payload},
                commit=True,
            )
        except Exception:  # noqa: BLE001
            # Observability about observability must not be able to fail a collection.
            logger.warning("metrics_event_failed", extra={"stage": stage})

    @staticmethod
    def _log(report: MetricsRunReport) -> None:
        for target in report.targets:
            logger.info(
                "metrics_collection",
                extra={
                    "metrics_run_id": report.metrics_run_id,
                    "target_id": target.target_id,
                    "videos_due": target.videos_due,
                    "videos_requested": target.videos_requested,
                    "videos_returned": target.videos_returned,
                    "snapshots_created": target.snapshots_created,
                    "missing": target.missing,
                    "failed": target.failed,
                    "provider_calls": target.provider_calls,
                    "duration_ms": report.duration_ms,
                },
            )


# --------------------------------------------------------------------- helpers


def capture_slot(moment: datetime) -> str:
    """The hour bucket an observation belongs to, as ``2026-09-04T13``.

    Hourly because the fastest cadence is hourly: a finer bucket would let two replicas a
    minute apart both record, and a coarser one would collapse the fresh-video series that is
    the most interesting part of it. The rounding rule is stored on the row rather than
    implied by a query, so it is visible rather than remembered.
    """
    return _as_utc(moment).strftime("%Y-%m-%dT%H")


def _interval_for(age: timedelta) -> timedelta:
    """How often a video of this age is observed."""
    hours = age.total_seconds() / 3600
    if hours < 24:
        return timedelta(hours=max(1, settings.metrics_interval_fresh_hours))
    if hours < 24 * 7:
        return timedelta(hours=max(1, settings.metrics_interval_recent_hours))
    return timedelta(hours=max(1, settings.metrics_interval_mature_hours))


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
