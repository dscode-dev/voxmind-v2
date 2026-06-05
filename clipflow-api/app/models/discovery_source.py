from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import DiscoverySourceKind


class DiscoverySource(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A provider/feed a topic is discovered from. Provider integration is deferred (Phase 8);
    this table + the abstraction is all V2 prepares for now."""

    __tablename__ = "discovery_sources"
    __table_args__ = (Index("ix_discovery_sources_topic", "topic_id"),)

    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_topics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    kind: Mapped[DiscoverySourceKind] = mapped_column(
        Enum(DiscoverySourceKind, name="discovery_source_kind_enum"),
        nullable=False,
    )

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    config_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    topic = relationship("ContentTopic", back_populates="sources")
    candidates = relationship("VideoCandidate", back_populates="source")
