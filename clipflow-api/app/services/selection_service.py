"""Runs the selection engine against persisted candidates.

The engine itself is pure and knows nothing about SQLAlchemy. This layer does three things:
loads candidates into ``CandidateView``, holds a lock so two concurrent runs cannot both spend
the same cap, and writes the outcome back.

**The boundary this PR does not cross.** A committed run moves candidates to ``SELECTED`` and
stops there. No ``PipelineJob`` is created and nothing reaches Redis. Selection is an editorial
decision; admitting that decision into production is a separate step, deliberately left for the
next PR so that turning on automatic selection cannot, by itself, start spending GPU time.

``CONSUMED`` is likewise not written here: it means "already produced", which is a fact about
production, not about selection.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.enums import PipelineEventType, VideoCandidateStatus
from app.models.video_candidate import VideoCandidate
from app.selection.engine import (
    CandidateAssessment,
    CandidateView,
    SelectionEngine,
    SelectionOutcome,
    TopicView,
)
from app.selection.policy import PERMANENT_REASONS, SCORE_VERSION, SelectionConfig
from app.services import event_bus

logger = logging.getLogger(__name__)

# A server-side ceiling. A caller asking for 10,000 selections is either mistaken or a bug,
# and this is the last line before automation can act at scale — §55.
HARD_MAX_SELECTED_PER_RUN = 25

# How a candidate came to be selected. Both paths are legitimate; conflating them would make
# it impossible to audit what the engine did versus what a person did.
METHOD_POLICY = "policy"
METHOD_MANUAL = "manual"


@dataclass
class SelectionRunReport:
    run_id: str
    topic_id: str
    dry_run: bool
    outcome: SelectionOutcome
    committed: int = 0
    marked_rejected: int = 0
    marked_ranked: int = 0

    def as_dict(self, *, verbose: bool = False) -> dict[str, Any]:
        payload = self.outcome.as_dict(verbose=verbose)
        payload.update(
            {
                "selection_run_id": self.run_id,
                "dry_run": self.dry_run,
                "committed": self.committed,
                "marked_ranked": self.marked_ranked,
                "marked_rejected": self.marked_rejected,
            }
        )
        return payload


class SelectionService:
    def __init__(self, engine: SelectionEngine | None = None) -> None:
        self.engine = engine or SelectionEngine()

    # ------------------------------------------------------------------ run

    def run(
        self,
        db: Session,
        *,
        topic: ContentTopic,
        limit: int | None = None,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> SelectionRunReport:
        now = now or datetime.now(timezone.utc)
        run_id = str(uuid.uuid4())
        config = self._config_for(topic, limit)

        if not dry_run:
            # One selection run per topic at a time. Without this, two concurrent runs both
            # read "0 selected so far", both pass the cap check, and the topic gets double
            # its allowance — with each run believing it obeyed the limit. A transaction-scoped
            # advisory lock keyed on the topic is enough: it serialises runs for this topic
            # without blocking any other topic, and it is released on commit or rollback.
            self._lock_topic(db, topic.id)

        candidates = self._load_candidates(db, topic)
        topic_view = TopicView(
            topic_id=str(topic.id),
            name=topic.name,
            description=topic.description,
            keywords=list(topic.keywords_json or []),
        )

        outcome = self.engine.run(
            topic=topic_view,
            candidates=candidates,
            config=config,
            now=now,
            already_selected_today=self._selected_today(db, topic, now),
            channel_last_selected=self._recent_channel_selections(db, topic, config, now),
        )

        report = SelectionRunReport(
            run_id=run_id, topic_id=str(topic.id), dry_run=dry_run, outcome=outcome
        )

        if not dry_run:
            self._persist(db, topic, outcome, report, now)

        self._emit(db, topic, report)
        self._log(report)
        return report

    # ---------------------------------------------------------------- loading

    def _config_for(self, topic: ContentTopic, limit: int | None) -> SelectionConfig:
        metadata = dict(topic.metadata_json or {})
        config = SelectionConfig().with_overrides(metadata.get("selection"))
        if limit is not None:
            config = config.with_overrides(
                {"max_selected_per_run": max(1, min(int(limit), HARD_MAX_SELECTED_PER_RUN))}
            )
        elif config.max_selected_per_run > HARD_MAX_SELECTED_PER_RUN:
            # A topic cannot configure its way past the server-side ceiling either.
            config = config.with_overrides({"max_selected_per_run": HARD_MAX_SELECTED_PER_RUN})
        return config

    def _load_candidates(self, db: Session, topic: ContentTopic) -> list[CandidateView]:
        """Load everything not already finished.

        SELECTED and CONSUMED rows are excluded at the query rather than being loaded and
        rejected: they cannot be chosen again, and pulling them in only to discard them scales
        badly as the table grows. REJECTED is also excluded — a rediscovery does not resurrect
        a rejected candidate.
        """
        rows = (
            db.query(VideoCandidate)
            .filter(
                VideoCandidate.topic_id == topic.id,
                VideoCandidate.status.in_(
                    [VideoCandidateStatus.DISCOVERED, VideoCandidateStatus.RANKED]
                ),
            )
            .all()
        )
        source_configs = {
            source.id: dict(source.config_json or {})
            for source in db.query(DiscoverySource).filter(
                DiscoverySource.topic_id == topic.id
            )
        }
        return [self._to_view(row, source_configs) for row in rows]

    @staticmethod
    def _to_view(row: VideoCandidate, source_configs: dict) -> CandidateView:
        metadata = dict(row.metadata_json or {})
        normalized = dict(metadata.get("normalized") or {})
        return CandidateView(
            candidate_id=str(row.id),
            status=row.status.value,
            title=row.title,
            description=normalized.get("description"),
            channel=row.channel,
            channel_id=normalized.get("channel_id"),
            url=row.url,
            source_id=str(row.source_id) if row.source_id else None,
            source_config=source_configs.get(row.source_id, {}),
            published_at=_as_utc(row.published_at),
            duration_sec=row.duration_sec,
            view_count=normalized.get("view_count"),
            like_count=normalized.get("like_count"),
            comment_count=normalized.get("comment_count"),
            live_status=normalized.get("live_status"),
            available=normalized.get("available", True),
            discovery_query=metadata.get("discovery_query"),
        )

    def _selected_today(self, db: Session, topic: ContentTopic, now: datetime) -> int:
        """How much of today's allowance is already spent."""
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            db.query(func.count(VideoCandidate.id))
            .filter(
                VideoCandidate.topic_id == topic.id,
                VideoCandidate.selected_at.isnot(None),
                VideoCandidate.selected_at >= midnight,
            )
            .scalar()
            or 0
        )

    def _recent_channel_selections(
        self, db: Session, topic: ContentTopic, config: SelectionConfig, now: datetime
    ) -> dict[str, datetime]:
        """Channel cooldown carried across runs, not just within one.

        A per-run cap alone would let three consecutive runs pick three videos from the same
        channel; the cooldown is what makes the diversity rule hold over time.
        """
        cutoff = now - timedelta(hours=config.channel_cooldown_hours)
        rows = (
            db.query(VideoCandidate)
            .filter(
                VideoCandidate.topic_id == topic.id,
                VideoCandidate.selected_at.isnot(None),
                VideoCandidate.selected_at >= cutoff,
            )
            .all()
        )
        latest: dict[str, datetime] = {}
        for row in rows:
            normalized = dict((row.metadata_json or {}).get("normalized") or {})
            key = normalized.get("channel_id") or row.channel
            selected_at = _as_utc(row.selected_at)
            if not key or selected_at is None:
                continue
            if key not in latest or selected_at > latest[key]:
                latest[key] = selected_at
        return latest

    # ------------------------------------------------------------- persisting

    def _persist(
        self,
        db: Session,
        topic: ContentTopic,
        outcome: SelectionOutcome,
        report: SelectionRunReport,
        now: datetime,
    ) -> None:
        by_id = {
            str(row.id): row
            for row in db.query(VideoCandidate).filter(
                VideoCandidate.topic_id == topic.id
            )
        }

        # Everything that was ranked gets its breakdown written, selected or not — that is
        # what makes "why was this not chosen?" answerable after the run.
        for assessment in outcome.ranked:
            row = by_id.get(assessment.candidate.candidate_id)
            if row is None:
                continue
            self._write_scores(row, assessment)
            if row.status == VideoCandidateStatus.DISCOVERED:
                row.status = VideoCandidateStatus.RANKED
                report.marked_ranked += 1

        for assessment in outcome.selected:
            row = by_id.get(assessment.candidate.candidate_id)
            if row is None:
                continue
            row.status = VideoCandidateStatus.SELECTED
            row.selected_at = now
            metadata = dict(row.metadata_json or {})
            metadata["selection"] = {
                "method": METHOD_POLICY,
                "selection_run_id": report.run_id,
                "score_version": SCORE_VERSION,
                "selected_at": now.isoformat(),
                "reasons": list(assessment.reasons),
            }
            row.metadata_json = metadata
            report.committed += 1
            self._emit_candidate_selected(db, topic, assessment, report)

        # Permanently unusable candidates are the only ones marked REJECTED. A cap, a
        # cooldown or today's freshness window are temporary: rejecting on those would burn a
        # candidate that tomorrow's run should still be able to consider.
        for assessment in outcome.ineligible_items:
            if not assessment.eligibility.permanent:
                continue
            row = by_id.get(assessment.candidate.candidate_id)
            if row is None or row.status != VideoCandidateStatus.DISCOVERED:
                continue
            row.status = VideoCandidateStatus.REJECTED
            metadata = dict(row.metadata_json or {})
            metadata["rejection"] = {
                "reasons": list(assessment.eligibility.reasons),
                "selection_run_id": report.run_id,
                "rejected_at": now.isoformat(),
            }
            row.metadata_json = metadata
            report.marked_rejected += 1

    @staticmethod
    def _write_scores(row: VideoCandidate, assessment: CandidateAssessment) -> None:
        breakdown = assessment.breakdown()
        row.scores_json = breakdown

        # Columns exist for sorting; the breakdown is the explanation. Only the two whose
        # meaning this PR actually defines are written.
        relevance = assessment.signals.get("effective_relevance")
        row.relevance_score = relevance.value if relevance and relevance.measurable else None

        components = (assessment.composition.components if assessment.composition else {}) or {}
        row.trend_score = components.get("trend")

        # quality_score and duplicate_score stay NULL on purpose. Nothing here knows anything
        # about the video's actual quality — no download, no frames, no audio — and exact
        # deduplication is already handled by dedup_hash, so a "duplicate score" would be a
        # number with no method behind it. A column is not a reason to invent a value.

    # ---------------------------------------------------------------- locking

    @staticmethod
    def _lock_topic(db: Session, topic_id) -> None:
        """Serialise committed runs for one topic.

        PostgreSQL advisory lock, transaction-scoped. On any other backend (the test suite
        runs on SQLite) this is a no-op — SQLite serialises writers anyway, so there is no
        concurrency to guard against there.
        """
        if db.bind is None or db.bind.dialect.name != "postgresql":
            return
        key = int(uuid.UUID(str(topic_id)).int % (2**31))
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    # ---------------------------------------------------------- observability

    def _emit(self, db: Session, topic: ContentTopic, report: SelectionRunReport) -> None:
        outcome = report.outcome
        event_bus.publish_event(
            db,
            service="selection",
            event_type=PipelineEventType.INFO,
            pipeline_job_id=None,
            stage="selection.completed",
            message=(
                f"selection {'dry-run' if report.dry_run else 'run'} on '{topic.name}': "
                f"{len(outcome.selected)} selected of {outcome.eligible} eligible"
            ),
            # Aggregated. One event per candidate ranked would be dozens per run saying what
            # these counters already say; `candidate.selected` below is emitted individually
            # because it changes the domain.
            payload={
                "selection_run_id": report.run_id,
                "topic_id": str(topic.id),
                "dry_run": report.dry_run,
                "score_version": SCORE_VERSION,
                "considered": outcome.considered,
                "eligible": outcome.eligible,
                "ineligible": outcome.ineligible,
                "ranked": len(outcome.ranked),
                "selected": len(outcome.selected),
                "blocked": len(outcome.blocked),
                "semantic_evaluated": outcome.semantic_evaluated,
                "semantic_failures": outcome.semantic_failures,
                "semantic_provider": outcome.semantic_provider,
                "duration_ms": outcome.duration_ms,
            },
        )

    def _emit_candidate_selected(
        self,
        db: Session,
        topic: ContentTopic,
        assessment: CandidateAssessment,
        report: SelectionRunReport,
    ) -> None:
        event_bus.publish_event(
            db,
            service="selection",
            event_type=PipelineEventType.INFO,
            pipeline_job_id=None,
            stage="candidate.selected",
            message=f"selected: {(assessment.candidate.title or '')[:120]}",
            payload={
                "selection_run_id": report.run_id,
                "topic_id": str(topic.id),
                "candidate_id": assessment.candidate.candidate_id,
                "rank": assessment.rank,
                "score": assessment.final_score,
                "reasons": list(assessment.reasons),
                "method": METHOD_POLICY,
            },
        )

    @staticmethod
    def _log(report: SelectionRunReport) -> None:
        outcome = report.outcome
        logger.info(
            "selection_run",
            extra={
                "selection_run_id": report.run_id,
                "topic_id": report.topic_id,
                "dry_run": report.dry_run,
                "score_version": SCORE_VERSION,
                "candidates_considered": outcome.considered,
                "eligible": outcome.eligible,
                "ineligible": outcome.ineligible,
                "ranked": len(outcome.ranked),
                "selected": len(outcome.selected),
                "temporarily_blocked": len(outcome.blocked),
                "rejected": report.marked_rejected,
                "semantic_evaluations": outcome.semantic_evaluated,
                "semantic_failures": outcome.semantic_failures,
                "semantic_provider": outcome.semantic_provider,
                "duration_ms": outcome.duration_ms,
            },
        )


def _as_utc(value: datetime | None) -> datetime | None:
    """Timezone-aware, always.

    A ``DateTime(timezone=True)`` column comes back aware on PostgreSQL and naive on SQLite,
    and the difference only surfaces the first time something subtracts two of them — which
    is inside the cooldown check, at runtime, on a code path a happy-path test never reaches.
    Normalising here keeps the engine free to assume aware datetimes.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
