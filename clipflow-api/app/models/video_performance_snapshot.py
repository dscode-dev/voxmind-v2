from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class VideoPerformanceSnapshot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One observation of a published video's counters at one moment.

    **Append-only.** A later collection inserts a new row; it never updates an earlier one.
    Overwriting would leave the system knowing a video has 410 views and nothing about how it
    got there, and the shape of that curve is the entire reason to collect anything.

        10:00  views=100
        12:00  views=180
        18:00  views=410

    **Absolute counters, not deltas.** What the provider reported, as reported. A delta is
    derivable from two snapshots; a missed collection makes a stored delta wrong for ever
    while leaving an absolute counter merely sparse.

    **Counters may go down.** YouTube removes spam views and deleted comments, so
    ``new >= old`` is not an invariant and is deliberately not enforced. A decrease is a valid
    observation, and rejecting it would replace a real measurement with a fiction.

    **NULL is not zero.** YouTube omits ``likeCount`` when the owner hides likes and
    ``commentCount`` when comments are disabled. Zero means "observed, and it was zero";
    NULL means "not disclosed". Collapsing them would invent data.

    Nothing here is scored. There is no engagement or performance figure, by design: this PR
    builds the measurement, and a derived number would be a decision dressed as an
    observation.
    """

    __tablename__ = "video_performance_snapshots"
    __table_args__ = (
        # One row per publication per capture slot. A collection that runs twice in the same
        # hour - two replicas, a retry, an operator pressing the button - records the same
        # observation once instead of littering the series with near-identical points.
        UniqueConstraint(
            "publish_attempt_id", "capture_slot",
            name="uq_video_performance_snapshot_slot",
        ),
        # The series for one publication, in order. Also serves "latest snapshot".
        Index("ix_performance_snapshots_attempt_time", "publish_attempt_id", "captured_at"),
        Index("ix_performance_snapshots_video", "external_video_id"),
        Index("ix_performance_snapshots_captured", "captured_at"),
    )

    # The publication this observes. Metrics hang off the PublishAttempt rather than the
    # PipelineJob because a run can publish several clips, and each is its own video with its
    # own audience - aggregating them at ingestion would destroy exactly the comparison the
    # data exists to make.
    publish_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("publish_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    publish_target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("publish_targets.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Denormalised deliberately: it is the provider's identity for the video, and a snapshot
    # must remain readable as an observation of *that* video even if the attempt row is
    # later reshaped.
    external_video_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="youtube")

    # When we asked. Timezone-aware UTC, never `datetime.utcnow` - the naive default on
    # TimestampMixin made the autopublish day boundary depend on the container's timezone,
    # and a metrics series would inherit the same defect.
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The bucket ``captured_at`` falls in, as an ISO hour ("2026-09-04T13"). Stored rather
    # than computed so the uniqueness constraint has something to hold, and so the rounding
    # rule is visible in the data instead of hidden in a query.
    capture_slot: Mapped[str] = mapped_column(String(32), nullable=False)

    # ------------------------------------------------------------------ counters
    # BIGINT: a successful video exceeds 2^31 views, and discovering that through an
    # overflow is not the way to find out.
    view_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    like_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # ------------------------------------------------------------------- status
    # What the provider said about the video itself: "ok", or why it could not be measured
    # ("not_returned", "unavailable"). A video the API declines to return is recorded as
    # such, never as zero views.
    availability: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    privacy_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Small and non-secret: the few provider fields worth keeping for an audit. Never a
    # token, never request headers, never a whole response body.
    provider_metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    attempt = relationship("PublishAttempt", back_populates="performance_snapshots")
