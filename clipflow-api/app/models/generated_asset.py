from __future__ import annotations

import uuid

from sqlalchemy import Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import GeneratedAssetKind


class GeneratedAsset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An artifact produced for a PipelineJob (clip, final video, thumbnail/metadata, …).
    Parallel to the billing-coupled ClipAsset, for the autonomous lineage."""

    __tablename__ = "generated_assets"
    __table_args__ = (Index("ix_generated_assets_job_kind", "pipeline_job_id", "kind"),)

    pipeline_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    kind: Mapped[GeneratedAssetKind] = mapped_column(
        Enum(GeneratedAssetKind, name="generated_asset_kind_enum"),
        nullable=False,
    )

    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    public_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)

    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    job = relationship("PipelineJob", back_populates="assets")
