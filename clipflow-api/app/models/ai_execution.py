from __future__ import annotations

import uuid

from sqlalchemy import Enum, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AIExecutionStatus


class AIExecution(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single AI provider call (OpenAI today; Local/OpenClaw future). Records provider,
    model, tokens, latency and cost so the Ops Center can show live AI activity."""

    __tablename__ = "ai_executions"
    __table_args__ = (Index("ix_ai_executions_job", "pipeline_job_id"),)

    pipeline_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_jobs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="openai")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[AIExecutionStatus] = mapped_column(
        Enum(AIExecutionStatus, name="ai_execution_status_enum"),
        nullable=False,
        default=AIExecutionStatus.PENDING,
    )

    prompt_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    job = relationship("PipelineJob", back_populates="ai_executions")
