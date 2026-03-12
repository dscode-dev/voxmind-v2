from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, Integer, String, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import JobStatus


class JobQueue(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "job_queue"
    __table_args__ = (
        Index("ix_job_queue_status", "status"),
        Index("ix_job_queue_worker", "worker_id"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clip_jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_queue_status_enum"),
        nullable=False,
        default=JobStatus.QUEUED,
    )

    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
    )

    worker_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    last_error: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    job = relationship("ClipJob")