from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AssetStatus, ClipAssetType


class ClipAsset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "clip_assets"
    __table_args__ = (
        Index("ix_clip_assets_job_type", "job_id", "asset_type"),
        Index("ix_clip_assets_job", "job_id"),
        CheckConstraint("start_sec >= 0", name="clip_assets_start_non_negative"),
        CheckConstraint("end_sec >= 0", name="clip_assets_end_non_negative"),
        CheckConstraint("end_sec >= start_sec", name="clip_assets_end_after_start"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clip_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    asset_type: Mapped[ClipAssetType] = mapped_column(
        Enum(ClipAssetType, name="clip_asset_type_enum"),
        nullable=False,
    )

    status: Mapped[AssetStatus] = mapped_column(
        Enum(AssetStatus, name="asset_status_enum"),
        nullable=False,
        default=AssetStatus.READY,
    )

    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    start_sec: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    end_sec: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)

    duration_sec: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)

    merge_group: Mapped[str | None] = mapped_column(String(120), nullable=True)

    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    public_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)

    thumbnail_text: Mapped[str | None] = mapped_column(String(255), nullable=True)

    hashtags_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    extra_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    job = relationship("ClipJob", back_populates="assets")