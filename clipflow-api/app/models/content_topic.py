from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ContentTopic(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A theme the autonomous factory continuously produces content for."""

    __tablename__ = "content_topics"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    keywords_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Scheduling / pacing config (consumed by the V2 scheduler in Phase 8).
    schedule_hours_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    cooldown_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    max_daily_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=24)

    default_clip_mode: Mapped[str] = mapped_column(String(64), nullable=False, default="short_serie")
    default_video_ratio: Mapped[str] = mapped_column(String(32), nullable=False, default="portrait")

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    sources = relationship(
        "DiscoverySource",
        back_populates="topic",
        cascade="all, delete-orphan",
    )
    candidates = relationship(
        "VideoCandidate",
        back_populates="topic",
        cascade="all, delete-orphan",
    )
    jobs = relationship("PipelineJob", back_populates="topic")
