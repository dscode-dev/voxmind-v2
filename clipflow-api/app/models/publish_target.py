from __future__ import annotations

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PublishPlatform


class PublishTarget(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A destination the factory publishes to (e.g. the Voxmind YouTube channel via OpenClaw).
    Provider implementation is deferred (Phase 7) — this is contract/config only."""

    __tablename__ = "publish_targets"

    platform: Mapped[PublishPlatform] = mapped_column(
        Enum(PublishPlatform, name="publish_platform_enum"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    config_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    attempts = relationship("PublishAttempt", back_populates="target")
