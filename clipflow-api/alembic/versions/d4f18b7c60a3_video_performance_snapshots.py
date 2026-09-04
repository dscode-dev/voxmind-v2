"""video performance snapshots: an append-only series per published video

Revision ID: d4f18b7c60a3
Revises: c8e5a37f2b91
Create Date: PR-METRICS-01

One table. Counters are BIGINT because a successful video exceeds 2^31 views and finding
that out through an overflow is not the way. They are nullable because YouTube omits
``likeCount`` when the owner hides likes and ``commentCount`` when comments are disabled -
NULL means "not disclosed", zero means "observed, and it was zero".

The unique constraint on (publish_attempt_id, capture_slot) is what makes a repeated
collection idempotent: two replicas, a retry, or an operator pressing the button in the same
hour record the same observation once.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d4f18b7c60a3"
down_revision = "c8e5a37f2b91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_performance_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "publish_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("publish_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "publish_target_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("publish_targets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("external_video_id", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="youtube"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_slot", sa.String(32), nullable=False),
        # BIGINT, and nullable: see the module docstring.
        sa.Column("view_count", sa.BigInteger(), nullable=True),
        sa.Column("like_count", sa.BigInteger(), nullable=True),
        sa.Column("comment_count", sa.BigInteger(), nullable=True),
        sa.Column("availability", sa.String(32), nullable=False, server_default="ok"),
        sa.Column("privacy_status", sa.String(32), nullable=True),
        sa.Column("provider_metadata_json", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "ix_video_performance_snapshots_publish_attempt_id",
        "video_performance_snapshots", ["publish_attempt_id"],
    )
    # The series for one publication, in order - and the index "latest snapshot" reads.
    op.create_index(
        "ix_performance_snapshots_attempt_time",
        "video_performance_snapshots", ["publish_attempt_id", "captured_at"],
    )
    op.create_index(
        "ix_performance_snapshots_video",
        "video_performance_snapshots", ["external_video_id"],
    )
    op.create_index(
        "ix_performance_snapshots_captured",
        "video_performance_snapshots", ["captured_at"],
    )
    # Idempotency: one observation per publication per capture slot.
    op.create_unique_constraint(
        "uq_video_performance_snapshot_slot",
        "video_performance_snapshots", ["publish_attempt_id", "capture_slot"],
    )


def downgrade() -> None:
    op.drop_table("video_performance_snapshots")
