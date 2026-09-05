"""Editorial metadata for each clip a run is about to publish.

**Per clip, not per run.** A four-clip run is four videos with four subjects, and one
description applied to all of them would be wrong about at least three. The context assembled
below is the clip's — its own working title, hook and transcript — with the run's shared facts
(topic, source video, channel) around it.

**Before the attempt, never during the upload.** Generation happens while the publication is
being prepared, so a slow model delays a decision rather than a byte stream, and a failed one
costs nothing that has already been sent.

**Failure is not a lost video.** Every path out of here either returns metadata or returns
nothing at all; nothing raises. Without a key, without a network, with an unparseable answer,
the run publishes under the technical fallback the publishing contract already resolves. A
metadata problem must never require a re-render.

**Written once and kept.** The result is stored on the job, so a retry after a failed upload
republishes under the same title rather than generating a new one — the metadata frozen on the
first attempt is what the second must send.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.ai_execution import AIExecution
from app.models.content_topic import ContentTopic
from app.models.pipeline_job import PipelineJob
from app.models.video_candidate import VideoCandidate
from app.publishing.metadata_ai import (
    SCHEMA_VERSION,
    ClipContext,
    MetadataResult,
    build_metadata_generator,
)
from app.services import event_bus
from app.models.enums import AIExecutionStatus, PipelineEventType

logger = logging.getLogger(__name__)

# Where the result lives on the job. Beside the manifest and the provenance, not in a second
# table: it is derived from the run and dies with it.
METADATA_KEY = "editorial_metadata"

# Enough transcript for the model to know what the clip is about, bounded because the whole
# point is one cheap call per clip.
MAX_TRANSCRIPT_CHARS = 2400


class PublicationMetadataService:
    def __init__(self, generator=None, artifacts=None, session_factory=None) -> None:
        self._generator = generator
        self._artifacts = artifacts
        # Only used to persist results; see `_commit_results`.
        self._session_factory = session_factory

    @property
    def generator(self):
        if self._generator is None:
            self._generator = build_metadata_generator(
                # The account-wide key, or a metadata-specific one if a deployment sets it.
                settings.resolve_openai_key(),
                model=settings.publication_metadata_model,
                timeout_sec=settings.publication_metadata_timeout_sec,
            )
        return self._generator

    @property
    def artifacts(self):
        if self._artifacts is None:
            from app.services.artifact_content_service import ArtifactContentService

            self._artifacts = ArtifactContentService()
        return self._artifacts

    # ------------------------------------------------------------------ ensure

    def ensure(self, db: Session, job: PipelineJob, items: list[Any]) -> dict[int, dict]:
        """Generate what is missing and return everything known for this run.

        Idempotent: a clip that already has metadata is never regenerated, so a retry costs
        nothing and cannot change what a partially published run is committed to sending.
        """
        stored = self._load(job)
        missing = [item for item in items if str(item.video_index) not in stored]
        if not missing or not self.generator.is_available():
            if missing and not self.generator.is_available():
                logger.info(
                    "publication_metadata_unavailable",
                    extra={"pipeline_job_id": str(job.id), "pending": len(missing)},
                )
            return {int(k): v for k, v in stored.items()}

        package = self._package(job)
        shared = self._shared_context(db, job)

        generated = 0
        outcomes: list[tuple[int, Any]] = []
        for item in missing:
            context = self._clip_context(item, shared, package, total=len(items))
            result = self.generator.generate(context)
            outcomes.append((item.video_index, result))

            if not result.ok:
                # Reported, not raised. The publication proceeds on the fallback.
                logger.warning(
                    "publication_metadata_failed",
                    extra={
                        "pipeline_job_id": str(job.id),
                        "video_index": item.video_index,
                        "status": result.status,
                        "reason": result.error,
                    },
                )
                continue

            stored[str(item.video_index)] = {
                "title": result.metadata.title,
                "description": result.metadata.description,
                "tags": result.metadata.tags,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                **result.provenance(),
            }
            generated += 1

        # Committed on a session of its own, deliberately. A dry run never commits — it
        # decides nothing — so writing through the caller would discard metadata that was
        # really generated and paid for, and the next real publish would call OpenAI again
        # for the same clips. It would also mean a recorded call never reaches ai_executions,
        # leaving the operations console unable to show evidence of a call that happened.
        self._commit_results(job.id, stored if generated else None, outcomes, generated)

        if generated:
            # Keep the caller's in-memory view consistent with what was just written, so a
            # publish in the same request sees its own metadata.
            metadata = dict(job.metadata_json or {})
            metadata[METADATA_KEY] = stored
            job.metadata_json = metadata
            logger.info(
                "publication_metadata_generated",
                extra={"pipeline_job_id": str(job.id), "clips": generated},
            )

        return {int(k): v for k, v in stored.items()}

    def _commit_results(
        self,
        job_id: Any,
        stored: dict[str, dict] | None,
        outcomes: list[tuple[int, Any]],
        generated: int,
    ) -> None:
        """Write the metadata, the AI executions and the events, and commit them.

        The whole record of the generation lands here, including the ``started`` event: the
        three rows are written in order but share one commit, so the feed reads correctly
        while nothing about the generation can be persisted half-way.

        Never raises: everything here is a record of work already done, and losing the record
        must not lose the work.
        """
        from app.db.session import SessionLocal

        factory = self._session_factory or SessionLocal
        db = factory()
        try:
            job = db.query(PipelineJob).filter(PipelineJob.id == job_id).first()
            if job is None:
                return

            if stored is not None:
                metadata = dict(job.metadata_json or {})
                metadata[METADATA_KEY] = stored
                job.metadata_json = metadata

            self._emit(db, job, "metadata_generation_started", {"clips": len(outcomes)})
            for index, result in outcomes:
                self._record_execution(db, job, result, index)
                self._emit(
                    db,
                    job,
                    "metadata_generation_succeeded" if result.ok
                    else "metadata_generation_failed",
                    {
                        "video_index": index,
                        "status": result.status,
                        **({} if result.ok else {"reason": result.error}),
                    },
                )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.warning(
                "publication_metadata_not_persisted",
                extra={"pipeline_job_id": str(job_id), "clips": generated},
            )
        finally:
            db.close()

    # ------------------------------------------------------------- observability

    PURPOSE = "publication_metadata"

    def _record_execution(self, db: Session, job: PipelineJob, result, index: int) -> None:
        """One AIExecution row per call, succeeded or failed.

        The table already exists and is what the operations surface reads to answer "is the
        AI working?". Writing here means that question is answered from recorded calls rather
        than from configuration — a key being present says nothing about whether it works.

        Never the prompt, never the response, never the key: provider, model, outcome and how
        long it took.
        """
        try:
            db.add(
                AIExecution(
                    pipeline_job_id=job.id,
                    provider=result.provider or "openai",
                    model=result.model,
                    purpose=self.PURPOSE,
                    status=(
                        AIExecutionStatus.SUCCEEDED if result.ok
                        else AIExecutionStatus.FAILED
                    ),
                    latency_ms=result.latency_ms,
                    # A code or an exception class name, already sanitised by the adapter.
                    error_message=None if result.ok else result.error,
                    payload_json={
                        "video_index": index,
                        "schema_version": SCHEMA_VERSION,
                    },
                )
            )
        except Exception:  # noqa: BLE001
            # Observability about the generation must not be able to fail the generation.
            logger.warning(
                "publication_metadata_execution_not_recorded",
                extra={"pipeline_job_id": str(job.id)},
            )

    # ----------------------------------------------------------------- context

    def _shared_context(self, db: Session, job: PipelineJob) -> dict[str, Any]:
        """Facts the whole run shares. Read from what is persisted, never reconstructed."""
        candidate = (
            db.query(VideoCandidate).filter(VideoCandidate.id == job.candidate_id).first()
            if job.candidate_id
            else None
        )
        topic = (
            db.query(ContentTopic).filter(ContentTopic.id == job.topic_id).first()
            if job.topic_id
            else None
        )
        frozen = dict((job.metadata_json or {}).get("snapshot") or {})
        return {
            "topic_name": (topic.name if topic else None) or frozen.get("topic_name"),
            "topic_keywords": list(topic.keywords_json or []) if topic else None,
            "source_title": candidate.title if candidate else None,
            "source_channel": candidate.channel if candidate else None,
            "clip_mode": frozen.get("clip_mode") or job.clip_mode,
        }

    def _clip_context(
        self, item: Any, shared: dict[str, Any], package: dict[str, Any], *, total: int
    ) -> ClipContext:
        video = getattr(item, "video", None) or {}
        post = video.get("post") or {}
        clip = video.get("final_clip") or {}
        return ClipContext(
            video_index=item.video_index,
            total_clips=total,
            topic_name=shared.get("topic_name"),
            topic_keywords=shared.get("topic_keywords"),
            source_title=shared.get("source_title") or package.get("primary_title"),
            source_channel=shared.get("source_channel"),
            clip_mode=shared.get("clip_mode"),
            # The worker's own editorial fields, when it produced any. They are the closest
            # thing to a human description of this specific cut.
            clip_title=post.get("title"),
            clip_hook=post.get("hook"),
            clip_description=post.get("description"),
            duration_sec=_number(clip.get("duration") or video.get("duration")),
            transcript_excerpt=self._transcript(video),
        )

    @staticmethod
    def _transcript(video: dict[str, Any]) -> str | None:
        """The clip's own words, when the package carries them.

        Read from the clip rather than from the source video's full transcript: a clip about
        one moment must not be described using the other twenty minutes.
        """
        for key in ("transcript", "spoken_text", "text"):
            value = video.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:MAX_TRANSCRIPT_CHARS]

        segments = video.get("transcript_segments") or video.get("segments")
        if isinstance(segments, list):
            parts = [
                str(segment.get("text", "")).strip()
                for segment in segments
                if isinstance(segment, dict) and segment.get("text")
            ]
            joined = " ".join(part for part in parts if part).strip()
            if joined:
                return joined[:MAX_TRANSCRIPT_CHARS]
        return None

    def _package(self, job: PipelineJob) -> dict[str, Any]:
        data = self.artifacts.load_json(f"jobs/{job.worker_job_id}/publish_package.json")
        return data if isinstance(data, dict) else {}

    # --------------------------------------------------------------- persistence

    @staticmethod
    def _load(job: PipelineJob) -> dict[str, dict]:
        raw = (job.metadata_json or {}).get(METADATA_KEY)
        return dict(raw) if isinstance(raw, dict) else {}

    def _emit(self, db: Session, job: PipelineJob, stage: str, payload: dict) -> None:
        try:
            event_bus.publish_event(
                db,
                service="publishing",
                event_type=PipelineEventType.INFO,
                pipeline_job_id=job.id,
                stage=stage,
                message=stage.replace("_", " "),
                # Never the prompt, never the key, never the provider body — only what
                # happened and to how many clips.
                payload={"schema_version": SCHEMA_VERSION, **payload},
            )
        except Exception:  # noqa: BLE001
            logger.warning("publication_metadata_event_failed", extra={"stage": stage})


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
