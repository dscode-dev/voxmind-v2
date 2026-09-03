"""publication runtime: enqueue window and crash-recovery evidence

Revision ID: a94e2c60b17d
Revises: f3c7a915d824
Create Date: PR-PUBLISH-QUEUE-01

Four columns, and deliberately only four. PublishAttempt is not becoming a queue table: the
queue lives in Redis and owns delivery. What the row needs is the *evidence* to answer one
question after a crash — did anything reach the provider? — plus the enqueue window that
moving execution out of the HTTP request reopens.
"""
from alembic import op
import sqlalchemy as sa

revision = "a94e2c60b17d"
down_revision = "f3c7a915d824"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "publish_attempts", sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "publish_attempts", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "publish_attempts", sa.Column("publisher_worker_id", sa.String(128), nullable=True)
    )
    # The column that makes a stuck IN_PROGRESS attempt classifiable rather than a guess.
    op.add_column(
        "publish_attempts",
        sa.Column("provider_started_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_publish_attempts_pending_enqueue",
        "publish_attempts",
        ["enqueued_at"],
        postgresql_where=sa.text("enqueued_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_publish_attempts_pending_enqueue", table_name="publish_attempts")
    for column in (
        "provider_started_at",
        "publisher_worker_id",
        "claimed_at",
        "enqueued_at",
    ):
        op.drop_column("publish_attempts", column)
