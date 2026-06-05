from __future__ import annotations

import uuid

from sqlalchemy import Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PipelineEventType


class PipelineEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Generic, service-agnostic event. Every V2 service (scheduler, discovery, worker, ai,
    publisher, api) publishes these; the frontend Ops Center consumes them via SSE. Distinct
    from the legacy ClipJob-bound JobEvent."""

    __tablename__ = "pipeline_events"
    __table_args__ = (
        Index("ix_pipeline_events_job_created", "pipeline_job_id", "created_at"),
        Index("ix_pipeline_events_service_created", "service", "created_at"),
        Index("ix_pipeline_events_type", "event_type"),
    )

    pipeline_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_jobs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    service: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)

    event_type: Mapped[PipelineEventType] = mapped_column(
        Enum(PipelineEventType, name="pipeline_event_type_enum"),
        nullable=False,
        default=PipelineEventType.INFO,
    )

    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    job = relationship("PipelineJob", back_populates="events")
