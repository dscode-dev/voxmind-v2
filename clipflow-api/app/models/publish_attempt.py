from __future__ import annotations

import uuid

from sqlalchemy import Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PublishAttemptStatus


class PublishAttempt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One publication attempt of a PipelineJob to a PublishTarget. History + retries."""

    __tablename__ = "publish_attempts"
    __table_args__ = (Index("ix_publish_attempts_job", "pipeline_job_id"),)

    pipeline_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("publish_targets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[PublishAttemptStatus] = mapped_column(
        Enum(PublishAttemptStatus, name="publish_attempt_status_enum"),
        nullable=False,
        default=PublishAttemptStatus.PENDING,
    )

    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    job = relationship("PipelineJob", back_populates="publish_attempts")
    target = relationship("PublishTarget", back_populates="attempts")
