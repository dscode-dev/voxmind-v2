from __future__ import annotations

import uuid

from sqlalchemy import Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import JobEventType


class JobEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "job_events"
    __table_args__ = (
        Index("ix_job_events_job_created", "job_id", "created_at"),
        Index("ix_job_events_type", "event_type"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clip_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[JobEventType] = mapped_column(
        Enum(JobEventType, name="job_event_type_enum"),
        nullable=False,
    )

    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)

    message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    job = relationship("ClipJob", back_populates="events")