"""Building a reproducible evaluation dataset out of snapshots that already exist.

**Evaluation, not optimization.** Like the ingestion layer beneath it, nothing here can change
what gets discovered, selected, produced or published. It reads; it never writes. The
boundary is structural — no production module imports `app.evaluation`, and a test fails if
one ever does.

**Nothing is materialized.** The rows below are derived on demand rather than stored, and that
is a deliberate choice rather than a shortcut. Reproducibility normally argues for freezing a
dataset, but here it does not need to: snapshots are append-only and never backfilled, so
``(as_of, semantic version, window policy, filters)`` already determines the output exactly.
A stored copy would add a second truth that can drift from the snapshots it was derived from,
two tables to migrate, and a question — "why does the frozen row disagree with the series?" —
that has no good answer. What is emitted instead is a manifest: enough to rebuild the identical
dataset, and an id that changes when any input to it changes.

**No provider call, ever.** Ingestion talks to YouTube; evaluation talks to the database. They
are different boundaries with different failure modes, and a dataset build that could spend
quota would be a dataset build nobody dares run twice.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Sequence

from sqlalchemy.orm import Session, joinedload

from app.evaluation.schema import (
    DATASET_SEMANTIC_VERSION,
    EXPORT_SCHEMA_VERSION,
    DecisionContext,
    EvaluationRow,
    PublicationContext,
    export_columns,
    schema_contract,
)
from app.evaluation.windows import (
    AVAILABLE,
    MISSING_SNAPSHOT,
    NOT_MATURE,
    VIDEO_NOT_RETURNED,
    WINDOW_POLICY_VERSION,
    policy_description,
    resolve_all,
    windows_in_order,
)
from app.models.enums import PublishAttemptStatus, PublishPlatform
from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt
from app.models.video_candidate import VideoCandidate
from app.models.video_performance_snapshot import VideoPerformanceSnapshot

logger = logging.getLogger(__name__)

# Why a considered publication produced no row. Every exclusion is counted and reported;
# a dataset that quietly drops rows is a dataset whose size means nothing.
MISSING_EXTERNAL_ID = "missing_external_id"
MISSING_PUBLISHED_AT = "missing_published_at"
MISSING_LINEAGE = "missing_lineage"

EXCLUSION_REASONS = (MISSING_EXTERNAL_ID, MISSING_PUBLISHED_AT, MISSING_LINEAGE)

# PostgreSQL will take far more, but an unbounded IN list is how a query plan degrades
# without anyone noticing.
_CHUNK = 1000


@dataclass(frozen=True)
class DatasetFilters:
    topic_id: str | None = None
    published_from: datetime | None = None
    published_to: datetime | None = None
    initiator: str | None = None
    privacy: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "published_from": _iso(self.published_from),
            "published_to": _iso(self.published_to),
            "initiator": self.initiator,
            "privacy": self.privacy,
        }


@dataclass
class DatasetManifest:
    """Everything needed to rebuild this dataset, and to tell it apart from another."""

    dataset_id: str
    semantic_version: str
    window_policy_version: str
    export_schema_version: str
    as_of: datetime
    filters: DatasetFilters
    generated_at: datetime
    row_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "semantic_version": self.semantic_version,
            "window_policy_version": self.window_policy_version,
            "export_schema_version": self.export_schema_version,
            "as_of": _iso(self.as_of),
            "filters": self.filters.as_dict(),
            "row_count": self.row_count,
            # Deliberately NOT part of dataset_id: when the build ran says nothing about what
            # it contains, and folding it in would make every rebuild look like a new dataset.
            "generated_at": _iso(self.generated_at),
            "windows": policy_description(),
        }


@dataclass
class DataQuality:
    considered: int = 0
    included: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    # window name -> state -> count
    window_states: dict[str, dict[str, int]] = field(default_factory=dict)
    # window name -> how many available observations had a NULL counter
    metric_null: dict[str, dict[str, int]] = field(default_factory=dict)

    def coverage(self) -> dict[str, Any]:
        """Available observations over publications old enough to have one.

        A measure of the *collector*, not of the content. Low coverage at 24h means the loop
        was not running, or the tolerance is too tight — never that the videos did badly.
        """
        report: dict[str, Any] = {}
        for window in windows_in_order():
            states = self.window_states.get(window.name, {})
            available = states.get(AVAILABLE, 0)
            mature = (
                available
                + states.get(MISSING_SNAPSHOT, 0)
                + states.get(VIDEO_NOT_RETURNED, 0)
            )
            report[window.name] = {
                "mature": mature,
                "available": available,
                "missing_snapshot": states.get(MISSING_SNAPSHOT, 0),
                "video_not_returned": states.get(VIDEO_NOT_RETURNED, 0),
                "not_mature": states.get(NOT_MATURE, 0),
                # NULL when nothing is mature yet: a ratio over zero publications is
                # undefined, and reporting 0% would read as a broken collector.
                "coverage": round(available / mature, 4) if mature else None,
            }
        return report

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "included": self.included,
            "excluded_total": sum(self.excluded.values()),
            "excluded": dict(self.excluded),
            "window_states": {
                name: dict(states) for name, states in self.window_states.items()
            },
            "metric_null": {
                name: dict(counts) for name, counts in self.metric_null.items()
            },
            "coverage": self.coverage(),
        }


@dataclass
class Dataset:
    manifest: DatasetManifest
    rows: list[EvaluationRow]
    quality: DataQuality

    def summary(self) -> dict[str, Any]:
        """Structural statistics only.

        Distributions and counts, never "top performers". Ranking publications would be an
        editorial claim this dataset cannot yet support: private and public videos do not
        receive comparable exposure, topics differ, and nothing here establishes causation.
        """
        topics: set[str] = set()
        privacy: dict[str, int] = defaultdict(int)
        initiator: dict[str, int] = defaultdict(int)
        for row in self.rows:
            if row.decision_context.topic_id:
                topics.add(row.decision_context.topic_id)
            privacy[row.publication_context.accepted_privacy
                    or row.publication_context.requested_privacy
                    or "unknown"] += 1
            initiator[row.publication_context.initiator or "unknown"] += 1
        return {
            "rows": len(self.rows),
            "topics": len(topics),
            "privacy_distribution": dict(privacy),
            "initiator_distribution": dict(initiator),
        }

    def as_dict(self, *, include_rows: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "manifest": self.manifest.as_dict(),
            "summary": self.summary(),
            "data_quality": self.quality.as_dict(),
            "schema": schema_contract(),
        }
        if include_rows:
            payload["rows"] = [row.as_dict() for row in self.rows]
        return payload


class PerformanceDatasetService:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------- build

    def build(
        self,
        db: Session,
        *,
        as_of: datetime | None = None,
        filters: DatasetFilters | None = None,
    ) -> Dataset:
        """Resolve every eligible publication against the canonical windows.

        ``as_of`` is the whole reproducibility story. Snapshots captured after it are
        invisible, so rebuilding tomorrow — with a week of new observations in the table —
        returns byte-identical rows. Without it, "the dataset I analysed on Tuesday" would be
        unrecoverable by Wednesday.
        """
        now = self._clock()
        as_of = _as_utc(as_of) if as_of else now
        filters = filters or DatasetFilters()

        attempts = self._eligible(db, filters=filters, as_of=as_of)
        quality = DataQuality(considered=len(attempts))

        rows: list[EvaluationRow] = []
        keep: list[PublishAttempt] = []
        for attempt in attempts:
            reason = self._exclusion(attempt)
            if reason:
                quality.excluded[reason] = quality.excluded.get(reason, 0) + 1
                continue
            keep.append(attempt)

        snapshots = self._snapshots(db, [a.id for a in keep], as_of=as_of)

        for attempt in keep:
            row = self._row(attempt, snapshots.get(attempt.id, []), as_of=as_of)
            rows.append(row)
            self._account(quality, row)

        quality.included = len(rows)
        rows.sort(key=lambda row: (row.publication_context.published_at or _EPOCH,
                                   row.publish_attempt_id))

        manifest = DatasetManifest(
            dataset_id=_dataset_id(as_of, filters),
            semantic_version=DATASET_SEMANTIC_VERSION,
            window_policy_version=WINDOW_POLICY_VERSION,
            export_schema_version=EXPORT_SCHEMA_VERSION,
            as_of=as_of,
            filters=filters,
            generated_at=now,
            row_count=len(rows),
        )
        logger.info(
            "evaluation_dataset_built",
            extra={
                "dataset_id": manifest.dataset_id,
                "as_of": _iso(as_of),
                "considered": quality.considered,
                "rows": len(rows),
                "excluded": sum(quality.excluded.values()),
            },
        )
        return Dataset(manifest=manifest, rows=rows, quality=quality)

    def evaluate_one(
        self, db: Session, attempt: PublishAttempt, *, as_of: datetime | None = None
    ) -> dict[str, Any]:
        """The same resolution, for a single publication.

        Shares `_row` with the dataset build rather than reimplementing it: a per-video read
        model that disagreed with the dataset would be worse than no read model at all.
        """
        as_of = _as_utc(as_of) if as_of else self._clock()
        reason = self._exclusion(attempt)
        if reason:
            return {
                "publish_attempt_id": str(attempt.id),
                "evaluable": False,
                "reason": reason,
                "as_of": _iso(as_of),
            }
        snapshots = self._snapshots(db, [attempt.id], as_of=as_of).get(attempt.id, [])
        row = self._row(attempt, snapshots, as_of=as_of)
        return {
            "evaluable": True,
            "as_of": _iso(as_of),
            "window_policy_version": WINDOW_POLICY_VERSION,
            "windows": policy_description(),
            **row.as_dict(),
        }

    # -------------------------------------------------------------- eligibility

    def _eligible(
        self, db: Session, *, filters: DatasetFilters, as_of: datetime
    ) -> list[PublishAttempt]:
        """One query, with lineage joined in.

        Eager loading rather than a lookup per row: a thousand-row dataset that resolved its
        own lineage would issue five thousand queries, and the second one would be someone
        wondering why the export takes four minutes.
        """
        query = (
            db.query(PublishAttempt)
            .join(PipelineJob, PublishAttempt.pipeline_job_id == PipelineJob.id)
            .options(
                joinedload(PublishAttempt.target),
                joinedload(PublishAttempt.job)
                .joinedload(PipelineJob.candidate)
                .joinedload(VideoCandidate.source),
                joinedload(PublishAttempt.job)
                .joinedload(PipelineJob.candidate)
                .joinedload(VideoCandidate.topic),
            )
            .filter(
                PublishAttempt.status == PublishAttemptStatus.SUCCEEDED,
                PublishAttempt.external_id.isnot(None),
                # Publications that finished after `as_of` did not exist yet, as far as this
                # dataset is concerned. Without this a rebuild would grow new rows, not just
                # new observations.
                PublishAttempt.finished_at.isnot(None),
            )
        )
        if filters.topic_id:
            topic_id = _uuid(filters.topic_id)
            if topic_id is None:
                # A malformed id matches nothing rather than raising: a filter is a question
                # about the data, and "no publications for that topic" is a valid answer.
                return []
            query = query.filter(PipelineJob.topic_id == topic_id)
        if filters.initiator:
            query = query.filter(PublishAttempt.initiator == filters.initiator)

        attempts = [
            attempt for attempt in query.all()
            if _as_utc(attempt.finished_at) <= as_of
        ]

        if filters.published_from:
            start = _as_utc(filters.published_from)
            attempts = [a for a in attempts if _as_utc(a.finished_at) >= start]
        if filters.published_to:
            end = _as_utc(filters.published_to)
            attempts = [a for a in attempts if _as_utc(a.finished_at) <= end]

        # YouTube only, for now: the window policy and the snapshot semantics below are the
        # Data API's, and quietly folding another platform in would compare figures that are
        # not the same measurement.
        attempts = [
            a for a in attempts
            if a.target is not None and a.target.platform == PublishPlatform.YOUTUBE
        ]

        if filters.privacy:
            # Applied in Python because the value lives in frozen JSON. A filter, not an
            # exclusion: rows removed here were never candidates for this dataset, so they
            # must not inflate the "considered" count that exclusions are measured against.
            attempts = [a for a in attempts if _privacy(a) == filters.privacy]
        return attempts

    @staticmethod
    def _exclusion(attempt: PublishAttempt) -> str | None:
        """Why this publication cannot become a row. Never a silent drop."""
        if not attempt.external_id:
            return MISSING_EXTERNAL_ID
        if attempt.finished_at is None:
            return MISSING_PUBLISHED_AT
        job = attempt.job
        if job is None:
            return MISSING_LINEAGE
        provenance = _provenance(job)
        if job.candidate is None and not provenance.get("video_candidate_id"):
            # No decision context at all. Included, it would be a row of NULL features that
            # any later analysis would treat as a measurement of "no selection score" rather
            # than as an absence of record.
            return MISSING_LINEAGE
        return None

    # ------------------------------------------------------------------ loading

    @staticmethod
    def _snapshots(
        db: Session, attempt_ids: Sequence[Any], *, as_of: datetime
    ) -> dict[Any, list[VideoPerformanceSnapshot]]:
        """Every snapshot for every row, in one query per chunk, grouped in memory.

        Not one query per window per video, which for five windows and a thousand rows would
        be five thousand round trips to answer a question the database can answer once.
        """
        grouped: dict[Any, list[VideoPerformanceSnapshot]] = defaultdict(list)
        for chunk in _chunks(list(attempt_ids), _CHUNK):
            rows = (
                db.query(VideoPerformanceSnapshot)
                .filter(
                    VideoPerformanceSnapshot.publish_attempt_id.in_(chunk),
                    # The look-ahead guard, applied in SQL as well as in the resolver. Belt
                    # and braces: this one keeps the rows out of memory, the resolver's keeps
                    # the invariant true for any caller that assembles snapshots itself.
                    VideoPerformanceSnapshot.captured_at <= as_of,
                )
                .order_by(VideoPerformanceSnapshot.captured_at.asc())
                .all()
            )
            for row in rows:
                grouped[row.publish_attempt_id].append(row)
        return grouped

    # ---------------------------------------------------------------------- row

    def _row(
        self,
        attempt: PublishAttempt,
        snapshots: list[VideoPerformanceSnapshot],
        *,
        as_of: datetime,
    ) -> EvaluationRow:
        job = attempt.job
        candidate = job.candidate if job else None
        provenance = _provenance(job)
        frozen = _frozen(job)

        published_at = _as_utc(attempt.finished_at)
        observations = resolve_all(published_at, snapshots, as_of=as_of)

        return EvaluationRow(
            publish_attempt_id=str(attempt.id),
            pipeline_job_id=str(attempt.pipeline_job_id) if attempt.pipeline_job_id else None,
            video_candidate_id=(
                str(candidate.id) if candidate is not None
                else provenance.get("video_candidate_id")
            ),
            external_video_id=attempt.external_id,
            decision_context=self._decision_context(candidate, provenance, frozen),
            publication_context=self._publication_context(attempt, published_at),
            observations=observations,
        )

    @staticmethod
    def _decision_context(
        candidate: VideoCandidate | None,
        provenance: dict[str, Any],
        frozen: dict[str, Any],
    ) -> DecisionContext:
        """What the system knew before it produced this video.

        Frozen provenance first, current objects only where nothing frozen exists. Admission
        writes ``selection_score``, ``score_version`` and ``selection_method`` onto the job at
        the moment of the decision, so those are read from there rather than recomputed from a
        candidate that a later ranking could in principle have touched.

        ``relevance_score`` and ``trend_score`` have no frozen copy, and are read from the
        candidate. That is safe here for a specific, checked reason: the selection service
        loads only DISCOVERED and RANKED candidates for scoring, so a row that has reached
        SELECTED or CONSUMED — which every published candidate has — can no longer be
        rescored. It is frozen by exclusion rather than by copy.
        """
        source = candidate.source if candidate is not None else None
        topic = candidate.topic if candidate is not None else None
        selected_at = _parse(provenance.get("selected_at")) or (
            _as_utc(candidate.selected_at)
            if candidate is not None and candidate.selected_at else None
        )
        source_published = (
            _as_utc(candidate.published_at)
            if candidate is not None and candidate.published_at else None
        )

        return DecisionContext(
            topic_id=provenance.get("topic_id")
            or (str(topic.id) if topic is not None else None),
            topic_name=frozen.get("topic_name") or (topic.name if topic else None),
            selection_method=provenance.get("selection_method"),
            selection_run_id=provenance.get("selection_run_id"),
            selection_score=_number(provenance.get("selection_score")),
            score_version=provenance.get("score_version"),
            selected_at=selected_at,
            relevance_score=_number(
                candidate.relevance_score if candidate is not None else None
            ),
            trend_score=_number(candidate.trend_score if candidate is not None else None),
            clip_mode=frozen.get("clip_mode"),
            video_ratio=frozen.get("video_ratio"),
            source_provider=(
                source.kind.value if source is not None and source.kind else None
            ),
            source_channel=candidate.channel if candidate is not None else None,
            source_external_id=provenance.get("external_id")
            or (candidate.external_id if candidate is not None else None),
            source_duration_sec=candidate.duration_sec if candidate is not None else None,
            source_published_at=source_published,
            candidate_age_at_selection_sec=(
                int((selected_at - source_published).total_seconds())
                if selected_at and source_published and selected_at >= source_published
                else None
            ),
        )

    @staticmethod
    def _publication_context(
        attempt: PublishAttempt, published_at: datetime
    ) -> PublicationContext:
        provider_metadata = attempt.provider_metadata_json or {}
        return PublicationContext(
            publish_target_id=str(attempt.target_id) if attempt.target_id else None,
            target_name=attempt.target.name if attempt.target else None,
            initiator=attempt.initiator,
            requested_privacy=_privacy(attempt),
            accepted_privacy=provider_metadata.get("privacy_status"),
            published_at=published_at,
            media_bytes=attempt.media_bytes,
        )

    @staticmethod
    def _account(quality: DataQuality, row: EvaluationRow) -> None:
        for name, observation in row.observations.items():
            states = quality.window_states.setdefault(name, {})
            states[observation.availability] = states.get(observation.availability, 0) + 1
            if observation.availability != AVAILABLE:
                continue
            nulls = quality.metric_null.setdefault(name, {})
            for metric, value in (
                ("view_count", observation.view_count),
                ("like_count", observation.like_count),
                ("comment_count", observation.comment_count),
            ):
                if value is None:
                    nulls[metric] = nulls.get(metric, 0) + 1

    # ------------------------------------------------------------------- export

    @staticmethod
    def to_csv(dataset: Dataset) -> Iterator[str]:
        """Stream the dataset as CSV, one row at a time.

        stdlib ``csv`` rather than a dataframe library: the job is to write declared columns
        in a declared order, and adding a heavyweight dependency to do that would cost every
        container that imports this module.

        NULL is the empty field, uniformly. A literal ``None`` or ``NaN`` in the file would be
        read back as a string by half the tools that open it.
        """
        columns = export_columns()
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer, fieldnames=columns, restval="", extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        yield _drain(buffer)

        for row in dataset.rows:
            flat = row.as_flat()
            writer.writerow({key: _csv_value(flat.get(key)) for key in columns})
            yield _drain(buffer)


# --------------------------------------------------------------------- helpers

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _dataset_id(as_of: datetime, filters: DatasetFilters) -> str:
    """Deterministic in its inputs, so the same request names the same dataset.

    Two builds an hour apart with the same ``as_of`` and filters produce the same id because
    they produce the same rows — which is exactly the claim reproducibility makes. Changing
    the window policy, the semantic version, the cut-off or any filter changes the id, so a
    dataset can never silently become a different one under the same name.
    """
    payload = json.dumps(
        {
            "semantic_version": DATASET_SEMANTIC_VERSION,
            "window_policy_version": WINDOW_POLICY_VERSION,
            "as_of": _iso(as_of),
            "filters": filters.as_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _provenance(job: PipelineJob | None) -> dict[str, Any]:
    if job is None:
        return {}
    return dict((job.metadata_json or {}).get("provenance") or {})


def _frozen(job: PipelineJob | None) -> dict[str, Any]:
    if job is None:
        return {}
    return dict((job.metadata_json or {}).get("snapshot") or {})


def _privacy(attempt: PublishAttempt) -> str | None:
    """The privacy that was requested, from the metadata frozen when the attempt was made."""
    metadata = (attempt.payload_json or {}).get("metadata") or {}
    value = metadata.get("privacy")
    return str(value) if value else None


def _uuid(value: str) -> uuid.UUID | None:
    """Filters arrive as strings; the column is a UUID.

    Kept as a string on `DatasetFilters` so the dataset id stays a digest of exactly what the
    caller asked for, and coerced here where the comparison actually happens.
    """
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _number(value: Any) -> float | None:
    """Numeric columns arrive as Decimal, and JSON has no Decimal."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _drain(buffer: io.StringIO) -> str:
    chunk = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return chunk


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
