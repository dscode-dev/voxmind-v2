from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Pipeline(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One configured flow, from finding a video to publishing cuts of it.

    This is the object an operator creates and the only one they have to understand. Everything
    else in the system hangs off it: the sources it searches, the candidates it finds, the
    productions it starts, and the channel those end up on. Two pipelines share nothing —
    that is the point of them being two.

    It was called ``ContentTopic``, which described the one field that made it up. What it
    actually holds now is a whole operating configuration, and calling it a topic made the
    product look like a tagging system.
    """

    __tablename__ = "pipelines"

    # What the operator calls it. Unique because it is how they refer to it out loud.
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # What it is *about* — the subject handed to discovery, to selection and to the model
    # that writes titles. Separate from `name` because the two drift apart the moment a
    # pipeline is called something operational: "Série A — canal principal" is a fine name
    # and a terrible thing to tell a model the videos are about. Falls back to `name`.
    theme: Mapped[str | None] = mapped_column(String(255), nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    keywords_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Where this pipeline talks. Per pipeline rather than per deployment: two pipelines
    # feeding two channels are usually watched by two different people, and a single global
    # chat turns both streams into one unreadable one.
    #
    # Empty means silent — never a fallback to some other pipeline's chat.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Scheduling / pacing.
    schedule_hours_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    cooldown_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    max_daily_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=24)

    default_clip_mode: Mapped[str] = mapped_column(
        String(64), nullable=False, default="short_serie"
    )
    default_video_ratio: Mapped[str] = mapped_column(
        String(32), nullable=False, default="portrait"
    )

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Automation policy, typed at the edge by `AutomationConfig`. It stays JSON because it is
    # policy that changes shape as the product learns; the columns above are identity, which
    # does not.
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    @property
    def subject(self) -> str:
        """What to tell discovery, selection and the model this pipeline is about."""
        return (self.theme or "").strip() or self.name

    sources = relationship(
        "DiscoverySource",
        back_populates="pipeline",
        cascade="all, delete-orphan",
    )
    candidates = relationship(
        "VideoCandidate",
        back_populates="pipeline",
        cascade="all, delete-orphan",
    )
    jobs = relationship("PipelineJob", back_populates="pipeline")
    runs = relationship("AutomationRun", back_populates="pipeline")
