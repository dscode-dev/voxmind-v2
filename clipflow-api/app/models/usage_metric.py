from __future__ import annotations

import uuid

from sqlalchemy import Enum, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import UsageMetricType


class UsageMetric(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "usage_metrics"
    __table_args__ = (
        Index("ix_usage_metrics_job", "job_id"),
        Index("ix_usage_metrics_user", "user_id"),
        Index("ix_usage_metrics_type", "metric_type"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clip_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    metric_type: Mapped[UsageMetricType] = mapped_column(
        Enum(UsageMetricType, name="usage_metric_type_enum"),
        nullable=False,
    )

    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)

    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    quantity: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)

    cost_usd: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)

    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    job = relationship("ClipJob")
    user = relationship("User")