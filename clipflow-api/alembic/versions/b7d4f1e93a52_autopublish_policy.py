"""autopublish policy: per-target consent, activation cutoff, attempt provenance

Revision ID: b7d4f1e93a52
Revises: a94e2c60b17d
Create Date: PR-PUBLISH-02

Three columns. ``autopublish_enabled`` defaults to **false** on every existing row, which is
the entire point of the migration: deploying this PR must not turn an installation that had
manual publishing working into one that publishes by itself.

``initiator`` backfills to "manual" because every attempt that exists today was created by an
admin pressing publish, and that is true rather than a convenient default.
"""
from alembic import op
import sqlalchemy as sa

revision = "b7d4f1e93a52"
down_revision = "a94e2c60b17d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "publish_targets",
        sa.Column(
            "autopublish_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # NULL means automation was never switched on. It is set the moment it is, and read as
    # the cutoff that keeps a historical backlog out of automatic publication.
    op.add_column(
        "publish_targets",
        sa.Column("autopublish_enabled_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "publish_attempts",
        sa.Column(
            "initiator",
            sa.String(16),
            nullable=False,
            server_default="manual",
        ),
    )
    # Finding what automation published today, for the daily cap, without scanning history.
    op.create_index(
        "ix_publish_attempts_initiator_created",
        "publish_attempts",
        ["initiator", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_publish_attempts_initiator_created", table_name="publish_attempts")
    op.drop_column("publish_attempts", "initiator")
    op.drop_column("publish_targets", "autopublish_enabled_at")
    op.drop_column("publish_targets", "autopublish_enabled")
