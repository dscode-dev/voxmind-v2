from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ScriptJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "script_jobs"
    __table_args__ = (
        Index("ix_script_jobs_user_status", "user_id", "status"),
        Index("ix_script_jobs_created_at", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    topic: Mapped[str] = mapped_column(String(500), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, default="instagram_reels")
    target_audience: Mapped[str | None] = mapped_column(String(300), nullable=True)
    objective: Mapped[str | None] = mapped_column(String(300), nullable=True)
    tone: Mapped[str | None] = mapped_column(String(120), nullable=True)
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="pt-BR")
    target_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=45)
    prompt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
