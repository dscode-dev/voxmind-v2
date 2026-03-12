from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, Integer, Numeric, String, Text, DateTime
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import JobInputMode, JobSourceType, JobStatus


class ClipJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "clip_jobs"
    __table_args__ = (
        Index("ix_clip_jobs_user_status", "user_id", "status"),
        Index("ix_clip_jobs_purchase_status", "purchase_id", "status"),
        CheckConstraint("video_duration_sec >= 0", name="clip_jobs_video_duration_non_negative"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    purchase_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("purchases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status_enum"),
        nullable=False,
        default=JobStatus.QUEUED,
    )

    input_mode: Mapped[JobInputMode] = mapped_column(
        Enum(JobInputMode, name="job_input_mode_enum"),
        nullable=False,
        default=JobInputMode.MANUAL_PROMPT,
    )

    source_type: Mapped[JobSourceType] = mapped_column(
        Enum(JobSourceType, name="job_source_type_enum"),
        nullable=False,
        default=JobSourceType.YOUTUBE_URL,
    )

    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)

    video_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    video_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    pipeline_stage: Mapped[str] = mapped_column(String(64), nullable=False, default="prepare")
    worker_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    prompt_mode: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    llm_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    requested_shorts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    merge_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)

    transcript_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    prompt_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ai_response_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    processing_cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)

    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    user = relationship("User", back_populates="jobs")
    purchase = relationship("Purchase", back_populates="jobs")
    product = relationship("BillingProduct", back_populates="jobs")

    assets = relationship("ClipAsset", back_populates="job", cascade="all, delete-orphan")
    events = relationship("JobEvent", back_populates="job", cascade="all, delete-orphan")