from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PipelineState


class PipelineJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Autonomous production job. Mirrors ClipJob's worker payload but carries no billing
    coupling — created by the scheduler from a VideoCandidate and tracked by the granular
    PipelineState machine."""

    __tablename__ = "pipeline_jobs"
    __table_args__ = (
        Index("ix_pipeline_jobs_state", "state"),
        Index("ix_pipeline_jobs_topic_state", "topic_id", "state"),
    )

    topic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_topics.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("video_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )

    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)

    preset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    clip_mode: Mapped[str] = mapped_column(String(64), nullable=False, default="short_serie")
    video_ratio: Mapped[str] = mapped_column(String(32), nullable=False, default="portrait")

    state: Mapped[PipelineState] = mapped_column(
        Enum(PipelineState, name="pipeline_state_enum"),
        nullable=False,
        default=PipelineState.DISCOVERED,
    )

    # Mirrors the worker's coarse stage ("prepare" | "finalize") for payload compatibility.
    pipeline_stage: Mapped[str] = mapped_column(String(64), nullable=False, default="prepare")
    worker_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    topic = relationship("ContentTopic", back_populates="jobs")
    candidate = relationship("VideoCandidate", back_populates="jobs")

    events = relationship(
        "PipelineEvent",
        back_populates="job",
        cascade="all, delete-orphan",
    )
    assets = relationship(
        "GeneratedAsset",
        back_populates="job",
        cascade="all, delete-orphan",
    )
    ai_executions = relationship(
        "AIExecution",
        back_populates="job",
        cascade="all, delete-orphan",
    )
    publish_attempts = relationship(
        "PublishAttempt",
        back_populates="job",
        cascade="all, delete-orphan",
    )
