"""Runs discovery for a topic: fetch, normalise, deduplicate, persist.

    ContentTopic ─ queries, language, freshness
         └─> DiscoverySource ─ kind + config
                  └─> Provider ──> DiscoveredVideo[]
                                        └─> dedup ──> VideoCandidate

The boundary this service must not cross: it produces candidates in ``DISCOVERED`` and
nothing else. It does not score them, does not select them and does not create a PipelineJob.
Discovery answers "what content exists?"; selection answers "what should we produce?", and
collapsing the two would mean a source change silently starts spending GPU time.

**Idempotency.** Running the same discovery twice must not double the rows. Identity is
``provider:external_id`` (see ``app/discovery/identity.py``), enforced by a unique index, and
a repeat run updates the mutable metadata of the row it already has. ``created_at`` is never
rewritten — a video rediscovered on its fifth day is not new — while ``last_seen_at`` moves
every time.

**Status is never reset.** A candidate a human rejected stays rejected when the same search
returns it tomorrow. Undoing a decision because a feed repeated itself would make rejection
meaningless.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.discovery import identity
from app.discovery.contracts import (
    DiscoveredVideo,
    DiscoveryFetch,
    DiscoveryRequest,
    ProviderError,
    ProviderUnavailable,
)
from app.discovery.rss_provider import RssDiscoveryProvider
from app.discovery.youtube_provider import YouTubeSearchProvider
from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.enums import (
    DiscoverySourceKind,
    PipelineEventType,
    VideoCandidateStatus,
)
from app.models.video_candidate import VideoCandidate
from app.services import event_bus

logger = logging.getLogger(__name__)

# Which provider serves which source kind. NEWS and MANUAL have no provider: NEWS would need
# one written, MANUAL means a human supplied the URL.
_PROVIDER_FOR_KIND = {
    DiscoverySourceKind.YOUTUBE_SEARCH: "youtube",
    DiscoverySourceKind.YOUTUBE_TRENDING: "youtube",
    DiscoverySourceKind.RSS: "rss",
}


@dataclass
class DiscoveryRunResult:
    """What one source-run did. The unit an operator asks questions about."""

    run_id: str
    topic_id: str
    source_id: str
    source_kind: str
    provider: str
    status: str = "completed"
    queries: list[str] = field(default_factory=list)
    results_received: int = 0
    new_candidates: int = 0
    existing_candidates: int = 0
    unavailable_candidates: int = 0
    api_calls: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "discovery_run_id": self.run_id,
            "topic_id": self.topic_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "provider": self.provider,
            "status": self.status,
            "queries": self.queries,
            "results_received": self.results_received,
            "new_candidates": self.new_candidates,
            "existing_candidates": self.existing_candidates,
            "unavailable_candidates": self.unavailable_candidates,
            "api_calls": self.api_calls,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
        }


class DiscoveryService:
    """The one place a discovery run happens."""

    def __init__(
        self,
        *,
        youtube_provider: Any | None = None,
        rss_provider: Any | None = None,
        default_max_results: int = 25,
        default_freshness_days: int = 7,
    ) -> None:
        self._providers = {
            "youtube": youtube_provider,
            "rss": rss_provider,
        }
        self.default_max_results = default_max_results
        self.default_freshness_days = default_freshness_days

    # ------------------------------------------------------------------ run

    def run_source(
        self,
        db: Session,
        *,
        topic: ContentTopic,
        source: DiscoverySource,
        max_results: int | None = None,
        published_after: datetime | None = None,
        commit: bool = True,
    ) -> DiscoveryRunResult:
        """Discover for one source. Never raises: a failed source is a reported outcome.

        A source that throws must not take down a run over the other sources of the same
        topic, and must not vanish either — the failure is classified, logged and returned.
        """
        started = time.monotonic()
        provider_name = _PROVIDER_FOR_KIND.get(source.kind)
        result = DiscoveryRunResult(
            run_id=str(uuid.uuid4()),
            topic_id=str(topic.id),
            source_id=str(source.id),
            source_kind=source.kind.value,
            provider=provider_name or "none",
        )

        provider = self._providers.get(provider_name) if provider_name else None
        if provider is None:
            result.status = "unsupported"
            result.errors.append({
                "error_type": "not_configured",
                "message": f"no provider implements source kind '{source.kind.value}'",
                "retryable": False,
            })
            self._finish(db, topic, source, result, started, commit)
            return result

        request = self.build_request(
            topic, source, max_results=max_results, published_after=published_after
        )
        result.queries = list(request.queries)

        self._emit(db, topic, source, result, "discovery.started", PipelineEventType.INFO)

        try:
            fetch = provider.discover(request)
        except ProviderUnavailable as exc:
            result.status = "unavailable"
            result.errors.append(exc.as_dict())
            self._finish(db, topic, source, result, started, commit)
            return result
        except ProviderError as exc:
            result.status = "failed"
            result.errors.append(exc.as_dict())
            self._finish(db, topic, source, result, started, commit)
            return result
        except Exception as exc:  # noqa: BLE001 - an unclassified provider bug
            result.status = "failed"
            # The exception text is not echoed: a provider that interpolates its request into
            # an error would put the API key into a stored event.
            result.errors.append({
                "error_type": "unexpected_provider_error",
                "message": type(exc).__name__,
                "retryable": False,
            })
            logger.exception("discovery_provider_crashed", extra={"provider": result.provider})
            self._finish(db, topic, source, result, started, commit)
            return result

        self._persist(db, topic, source, fetch, result)
        result.api_calls = fetch.api_calls
        result.errors.extend(fetch.errors)
        if fetch.errors and not fetch.videos:
            result.status = "failed"
        elif fetch.errors:
            result.status = "partial"

        self._finish(db, topic, source, result, started, commit)
        return result

    def run_topic(
        self,
        db: Session,
        *,
        topic: ContentTopic,
        max_results: int | None = None,
        published_after: datetime | None = None,
        commit: bool = True,
    ) -> list[DiscoveryRunResult]:
        """Discover across every active source of a topic."""
        sources = [source for source in topic.sources if source.is_active]
        results = [
            self.run_source(
                db,
                topic=topic,
                source=source,
                max_results=max_results,
                published_after=published_after,
                commit=commit,
            )
            for source in sources
        ]
        topic.last_run_at = datetime.now(timezone.utc)
        if commit:
            db.commit()
        return results

    # -------------------------------------------------------------- request

    def build_request(
        self,
        topic: ContentTopic,
        source: DiscoverySource,
        *,
        max_results: int | None = None,
        published_after: datetime | None = None,
    ) -> DiscoveryRequest:
        """Assemble the provider request from the topic and the source config.

        Queries come from configuration, never from the provider. A term compiled into the
        fetching code cannot be changed without a deploy, and 'football' would quietly become
        the only thing the system can find.
        """
        config = dict(source.config_json or {})
        topic_metadata = dict(topic.metadata_json or {})

        # A source that declares `queries` owns them, INCLUDING an empty list. That is how a
        # curated feed says "take everything": the channel itself is the filter, and falling
        # back to the topic's keywords there would silently discard most of what it publishes.
        # Only a source that is silent on the matter inherits the topic's keywords.
        raw_queries = config["queries"] if "queries" in config else topic.keywords_json
        queries = [
            str(item).strip()
            for item in (raw_queries or [])
            if str(item or "").strip()
        ]

        freshness_days = _positive_int(
            config.get("freshness_days") or topic_metadata.get("freshness_days"),
            self.default_freshness_days,
        )
        if published_after is None:
            published_after = datetime.now(timezone.utc) - timedelta(days=freshness_days)

        return DiscoveryRequest(
            queries=queries,
            published_after=published_after,
            published_before=None,
            language=config.get("language") or topic_metadata.get("language"),
            region=config.get("region") or topic_metadata.get("region"),
            max_results=_positive_int(
                max_results or config.get("max_results"), self.default_max_results
            ),
            config=config,
        )

    # -------------------------------------------------------------- persist

    def _persist(
        self,
        db: Session,
        topic: ContentTopic,
        source: DiscoverySource,
        fetch: DiscoveryFetch,
        result: DiscoveryRunResult,
    ) -> None:
        seen_in_this_run: set[str] = set()

        for video in fetch.videos:
            result.results_received += 1
            key = identity.dedup_hash(video.provider, video.external_id)

            # The same video can appear twice within one fetch — two overlapping queries
            # return it, or a feed lists it twice. Collapse before touching the database.
            if key in seen_in_this_run:
                result.existing_candidates += 1
                continue
            seen_in_this_run.add(key)

            created = self._upsert(db, topic, source, video, key)
            if created:
                result.new_candidates += 1
            else:
                result.existing_candidates += 1
            if not video.available:
                result.unavailable_candidates += 1

    def _upsert(
        self,
        db: Session,
        topic: ContentTopic,
        source: DiscoverySource,
        video: DiscoveredVideo,
        key: str,
    ) -> bool:
        """Insert or refresh one candidate. Returns True when a row was created.

        The IntegrityError branch is the concurrency case: another run inserted the same
        identity between this one's lookup and its flush. The unique index catches it, and the
        row that won is updated instead — which is the same outcome as if the two runs had
        been sequential.
        """
        existing = (
            db.query(VideoCandidate)
            .filter(VideoCandidate.dedup_hash == key)
            .first()
        )
        if existing is not None:
            self._refresh(existing, video, source)
            return False

        candidate = self._build(topic, source, video, key)
        try:
            # The insert happens inside a SAVEPOINT so a constraint violation rolls back only
            # this row. Adding it outside would leave the failed object pending in the
            # session, and every later statement in the run would fail on it.
            with db.begin_nested():
                db.add(candidate)
                db.flush()
        except IntegrityError:
            if candidate in db:
                db.expunge(candidate)
            winner = (
                db.query(VideoCandidate)
                .filter(VideoCandidate.dedup_hash == key)
                .first()
            )
            if winner is None:
                # The constraint fired for something other than the dedup identity, so this
                # is a real error rather than a race that resolved itself.
                raise
            self._refresh(winner, video, source)
            return False
        return True

    def _build(
        self,
        topic: ContentTopic,
        source: DiscoverySource,
        video: DiscoveredVideo,
        key: str,
    ) -> VideoCandidate:
        now = datetime.now(timezone.utc)
        return VideoCandidate(
            topic_id=topic.id,
            source_id=source.id,
            external_id=video.external_id,
            url=video.canonical_url,
            title=_clip(video.title, 500),
            channel=_clip(video.channel_name, 255),
            thumbnail_url=video.thumbnail_url,
            duration_sec=video.duration_sec,
            published_at=video.published_at,
            dedup_hash=key,
            # Always DISCOVERED. Selection is a later PR, and a discovery run that could
            # produce SELECTED would be a discovery run that can start production.
            status=VideoCandidateStatus.DISCOVERED,
            last_seen_at=now,
            metadata_json={
                "dedup_key": identity.dedup_key(video.provider, video.external_id),
                "provider": video.provider,
                "discovered_via": source.kind.value,
                # Seeded with the source that found it first, so the list is complete rather
                # than only recording sources that arrived after the row existed.
                "seen_via": [source.kind.value],
                "normalized": video.normalized_fields(),
                "raw": video.raw_metadata,
            },
        )

    def _refresh(
        self,
        candidate: VideoCandidate,
        video: DiscoveredVideo,
        source: DiscoverySource,
    ) -> None:
        """Update what can legitimately change; leave identity and decisions alone.

        View counts move, titles get edited, a video becomes unavailable. None of that makes
        the candidate new, and none of it may reset a status a human set.
        """
        candidate.last_seen_at = datetime.now(timezone.utc)

        if video.title:
            candidate.title = _clip(video.title, 500)
        if video.channel_name:
            candidate.channel = _clip(video.channel_name, 255)
        if video.thumbnail_url:
            candidate.thumbnail_url = video.thumbnail_url
        if video.duration_sec is not None:
            candidate.duration_sec = video.duration_sec
        if video.published_at is not None:
            candidate.published_at = video.published_at
        # `created_at` is deliberately untouched: it is when this video was FIRST discovered,
        # and rewriting it on every sighting would erase how long it has been known.

        metadata = dict(candidate.metadata_json or {})
        metadata["normalized"] = video.normalized_fields()
        if video.raw_metadata:
            metadata["raw"] = video.raw_metadata
        metadata.setdefault("dedup_key", identity.dedup_key(video.provider, video.external_id))
        metadata.setdefault("provider", video.provider)
        # Which sources have surfaced this video is itself a signal worth keeping.
        seen_via = set(metadata.get("seen_via") or [])
        seen_via.add(source.kind.value)
        metadata["seen_via"] = sorted(seen_via)
        candidate.metadata_json = metadata

        # status is NOT reassigned. A rediscovery does not un-reject a candidate.

    # -------------------------------------------------------- observability

    def _finish(
        self,
        db: Session,
        topic: ContentTopic,
        source: DiscoverySource,
        result: DiscoveryRunResult,
        started: float,
        commit: bool,
    ) -> None:
        result.duration_ms = int((time.monotonic() - started) * 1000)

        event_type = {
            "completed": PipelineEventType.INFO,
            "partial": PipelineEventType.WARNING,
            "unavailable": PipelineEventType.WARNING,
            "unsupported": PipelineEventType.WARNING,
            "failed": PipelineEventType.ERROR,
        }.get(result.status, PipelineEventType.INFO)
        name = "discovery.failed" if result.status == "failed" else "discovery.completed"
        self._emit(db, topic, source, result, name, event_type)

        # One structured line per run, carrying every field an operator filters on. Queries
        # are included; the API key is not, and never reaches this layer.
        logger.info(
            "discovery_run",
            extra={
                "discovery_run_id": result.run_id,
                "topic_id": result.topic_id,
                "source_id": result.source_id,
                "provider": result.provider,
                "status": result.status,
                "results_received": result.results_received,
                "new_candidates": result.new_candidates,
                "existing_candidates": result.existing_candidates,
                "api_calls": result.api_calls,
                "errors": len(result.errors),
                "duration_ms": result.duration_ms,
            },
        )

        if commit:
            db.commit()

    def _emit(
        self,
        db: Session,
        topic: ContentTopic,
        source: DiscoverySource,
        result: DiscoveryRunResult,
        name: str,
        event_type: PipelineEventType,
    ) -> None:
        """One event per run, not one per candidate.

        A run can return fifty videos; fifty ``candidate.discovered`` events would bury the
        operational feed to say what the counters already say. The aggregate is the event.
        """
        payload = result.as_dict()
        # `pipeline_job_id` is None on purpose: discovery precedes any run, and a candidate is
        # not yet attached to one. That is the boundary this PR is preserving.
        event_bus.publish_event(
            db,
            service="discovery",
            event_type=event_type,
            pipeline_job_id=None,
            stage=name,
            message=f"{name} {result.provider} ({result.status})",
            payload={key: payload[key] for key in payload if key != "errors"}
            | {"error_types": sorted({str(e.get("error_type")) for e in result.errors})},
        )


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def build_default_service(
    api_key: str | None,
    *,
    timeout_sec: float,
    max_results: int,
    freshness_days: int,
) -> DiscoveryService:
    """The service as production wires it.

    The YouTube provider is constructed with whatever key exists, including none — it reports
    itself unavailable in that case. There is no fake provider standing in: a system with no
    credential must say so, not return invented videos that look real in the database.
    """
    return DiscoveryService(
        youtube_provider=YouTubeSearchProvider(api_key, timeout_sec=timeout_sec),
        rss_provider=RssDiscoveryProvider(timeout_sec=timeout_sec),
        default_max_results=max_results,
        default_freshness_days=freshness_days,
    )
