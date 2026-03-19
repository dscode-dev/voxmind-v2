from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PrivateSchedulerRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "private_scheduler_runs"
    __table_args__ = (
        Index("ix_private_scheduler_runs_profile_status", "profile_id", "status"),
        Index("ix_private_scheduler_runs_slot", "scheduled_slot_at"),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("private_scheduler_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clip_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    scheduled_slot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    topic_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    profile = relationship("PrivateSchedulerProfile", back_populates="runs")
    job = relationship("ClipJob")
