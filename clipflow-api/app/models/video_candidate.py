from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import VideoCandidateStatus


class VideoCandidate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A discovered video the intelligence layer ranks for possible production."""

    __tablename__ = "video_candidates"
    __table_args__ = (
        Index("ix_video_candidates_topic_status", "topic_id", "status"),
        # UNIQUE, and enforced by the database rather than by a read-then-write in the
        # service: two discovery runs finding the same video at the same moment would both
        # see nothing and both insert. Partial, so rows with no derivable identity are
        # exempt instead of colliding on NULL.
        Index(
            "uq_video_candidates_dedup_hash",
            "dedup_hash",
            unique=True,
            postgresql_where=text("dedup_hash IS NOT NULL"),
        ),
        Index("ix_video_candidates_published_at", "published_at"),
        Index("ix_video_candidates_source_status", "source_id", "status"),
    )

    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_topics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("discovery_sources.id", ondelete="SET NULL"),
        nullable=True,
    )

    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # sha256 of "<provider>:<external_id>" — see app/discovery/identity.py. Hashed so any
    # provider's id shape fits one bounded, indexable column; the readable key is kept in
    # metadata_json for humans.
    dedup_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[VideoCandidateStatus] = mapped_column(
        Enum(VideoCandidateStatus, name="video_candidate_status_enum"),
        nullable=False,
        default=VideoCandidateStatus.DISCOVERED,
    )

    # Intelligence-layer scores (Phase 8). Kept both as columns (for sorting) and a JSON blob.
    relevance_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    trend_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    duplicate_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    scores_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # When a source last returned this video. Distinct from updated_at, which moves on any
    # write: only this one answers "is this item still being published?".
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    topic = relationship("ContentTopic", back_populates="candidates")
    source = relationship("DiscoverySource", back_populates="candidates")
    jobs = relationship("PipelineJob", back_populates="candidate")
