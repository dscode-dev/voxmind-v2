"""Give video_candidates a real dedup identity and a last-seen timestamp.

PR-DISCOVERY-01 makes these tables load-bearing for the first time — nothing read or wrote
them before. Two gaps then matter:

* **``dedup_hash`` had a non-unique index.** Deduplicating with SELECT-then-INSERT and no
  constraint is a race: two discovery runs finding the same video at the same time both see
  nothing and both insert. The uniqueness has to be enforced by the database, which is also
  what makes an upsert (ON CONFLICT) possible. Partial, so rows without an identity are
  unaffected rather than colliding with each other on NULL.

* **There was no ``last_seen_at``.** ``updated_at`` moves on any write, so it cannot answer
  "when did a source last return this video?" — the two are different questions, and only the
  second tells you whether an item is still being published.

Two indexes are added for the queries the candidate list actually runs (recency ordering, and
filtering by source). Nothing else is touched: no table is redesigned and no existing row is
rewritten.

Revision ID: c4e8a2f60b13
Revises: b3d7e1f4a920
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a2f60b13"
down_revision: Union[str, Sequence[str], None] = "b3d7e1f4a920"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "video_candidates",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Existing rows (if any) have been seen exactly once, when they were created.
    op.execute("UPDATE video_candidates SET last_seen_at = created_at WHERE last_seen_at IS NULL")

    # The non-unique index is replaced by a unique partial one. Partial because a candidate
    # with no derivable identity should not be forced to collide with every other such row on
    # NULL — Postgres would allow that anyway, but stating WHERE NOT NULL makes the intent
    # explicit and keeps the index smaller.
    op.drop_index("ix_video_candidates_dedup", table_name="video_candidates")
    op.create_index(
        "uq_video_candidates_dedup_hash",
        "video_candidates",
        ["dedup_hash"],
        unique=True,
        postgresql_where=sa.text("dedup_hash IS NOT NULL"),
    )

    # Recency is the default ordering of the candidate list.
    op.create_index(
        "ix_video_candidates_published_at",
        "video_candidates",
        ["published_at"],
        unique=False,
    )
    op.create_index(
        "ix_video_candidates_source_status",
        "video_candidates",
        ["source_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_video_candidates_source_status", table_name="video_candidates")
    op.drop_index("ix_video_candidates_published_at", table_name="video_candidates")
    op.drop_index("uq_video_candidates_dedup_hash", table_name="video_candidates")
    op.create_index(
        "ix_video_candidates_dedup", "video_candidates", ["dedup_hash"], unique=False
    )
    op.drop_column("video_candidates", "last_seen_at")
