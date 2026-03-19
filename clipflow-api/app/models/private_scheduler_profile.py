from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PrivateSchedulerProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "private_scheduler_profiles"
    __table_args__ = (
        Index("ix_private_scheduler_profiles_active", "is_active"),
        Index("ix_private_scheduler_profiles_owner", "owner_user_id"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    topic_label: Mapped[str] = mapped_column(String(255), nullable=False)
    discovery_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="seed_urls")
    clip_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="short")
    video_ratio: Mapped[str] = mapped_column(String(32), nullable=False, default="portrait")
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Recife")

    seed_urls_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    schedule_hours_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    owner = relationship("User")
    product = relationship("BillingProduct")
    runs = relationship("PrivateSchedulerRun", back_populates="profile", cascade="all, delete-orphan")
