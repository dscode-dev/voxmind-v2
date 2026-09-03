"""publishing foundation: oauth state, target credentials, attempt identity

Revision ID: f3c7a915d824
Revises: e6b2d84c1f37
Create Date: PR-PUBLISH-01

``publish_targets`` and ``publish_attempts`` existed as contract-only tables with no writer
anywhere in the codebase. This migration gives them the columns a real publisher needs.

The status enum is extended rather than replaced, in an ``autocommit_block``. PostgreSQL
accepts ``ALTER TYPE ... ADD VALUE`` inside a transaction but refuses to *use* the new label
until it is committed, and the partial index below has those labels in its predicate. Adding
them in their own committed block is what makes the index creation legal.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f3c7a915d824"
down_revision = "e6b2d84c1f37"
branch_labels = None
depends_on = None


# The enum stores Python member NAMES, so these are uppercase.
NEW_ATTEMPT_STATUSES = (
    "FAILED_RETRYABLE",
    "FAILED_FINAL",
    "UNKNOWN",
    "NEEDS_MANUAL_RESOLUTION",
)


def upgrade() -> None:
    # ------------------------------------------------------------------ enums
    # Outside the migration's transaction: the labels have to be committed before
    # ix_publish_attempts_unresolved can name them in its WHERE clause.
    with op.get_context().autocommit_block():
        for label in NEW_ATTEMPT_STATUSES:
            op.execute(
                f"ALTER TYPE publish_attempt_status_enum ADD VALUE IF NOT EXISTS '{label}'"
            )

    retryability = postgresql.ENUM(
        "RETRYABLE",
        "NOT_RETRYABLE",
        "REQUIRES_MANUAL_RESOLUTION",
        name="publish_retryability_enum",
    )
    retryability.create(op.get_bind(), checkfirst=True)

    connection_status = postgresql.ENUM(
        "DISCONNECTED",
        "CONNECTED",
        "RECONNECT_REQUIRED",
        name="publish_target_connection_status_enum",
    )
    connection_status.create(op.get_bind(), checkfirst=True)

    # --------------------------------------------------------- oauth_states
    op.create_table(
        "oauth_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default="youtube"),
        sa.Column("state", sa.String(128), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redirect_uri", sa.String(500), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Unique because single-use is enforced by finding exactly one row, and indexed because
    # the callback looks a state up on every request.
    op.create_index("ix_oauth_states_state", "oauth_states", ["state"], unique=True)

    # -------------------------------------------------------- publish_targets
    op.add_column("publish_targets", sa.Column("channel_id", sa.String(255), nullable=True))
    op.add_column("publish_targets", sa.Column("channel_title", sa.String(255), nullable=True))
    op.add_column(
        "publish_targets",
        sa.Column(
            "connection_status",
            connection_status,
            nullable=False,
            server_default="DISCONNECTED",
        ),
    )
    op.add_column(
        "publish_targets", sa.Column("refresh_token_encrypted", sa.Text(), nullable=True)
    )
    op.add_column("publish_targets", sa.Column("granted_scopes", sa.Text(), nullable=True))
    op.add_column(
        "publish_targets", sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "publish_targets", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "publish_targets", sa.Column("last_error_code", sa.String(128), nullable=True)
    )
    # Partial: several targets may exist with no channel yet (never connected), but only one
    # per actual channel.
    op.create_index(
        "uq_publish_targets_channel",
        "publish_targets",
        ["platform", "channel_id"],
        unique=True,
        postgresql_where=sa.text("channel_id IS NOT NULL"),
    )

    # ------------------------------------------------------- publish_attempts
    op.add_column(
        "publish_attempts", sa.Column("idempotency_key", sa.String(300), nullable=True)
    )
    op.add_column(
        "publish_attempts", sa.Column("media_identity", sa.String(255), nullable=True)
    )
    op.add_column("publish_attempts", sa.Column("media_storage_key", sa.Text(), nullable=True))
    op.add_column("publish_attempts", sa.Column("media_bytes", sa.BigInteger(), nullable=True))
    op.add_column(
        "publish_attempts", sa.Column("retryability", retryability, nullable=True)
    )
    op.add_column(
        "publish_attempts",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "publish_attempts", sa.Column("external_id_source", sa.String(32), nullable=True)
    )
    op.add_column(
        "publish_attempts", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "publish_attempts", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("publish_attempts", sa.Column("error_code", sa.String(128), nullable=True))
    op.add_column(
        "publish_attempts",
        sa.Column("upload_session_uri_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "publish_attempts", sa.Column("bytes_uploaded", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "publish_attempts",
        sa.Column("provider_metadata_json", postgresql.JSONB, nullable=True),
    )

    # The identity guarantee. Two concurrent publish requests for the same job/target/media
    # both insert; exactly one commits and the other re-reads the winner.
    op.create_index(
        "uq_publish_attempts_idempotency",
        "publish_attempts",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_publish_attempts_unresolved",
        "publish_attempts",
        ["status"],
        postgresql_where=sa.text("status IN ('UNKNOWN', 'NEEDS_MANUAL_RESOLUTION')"),
    )


def downgrade() -> None:
    op.drop_index("ix_publish_attempts_unresolved", table_name="publish_attempts")
    op.drop_index("uq_publish_attempts_idempotency", table_name="publish_attempts")
    for column in (
        "provider_metadata_json",
        "bytes_uploaded",
        "upload_session_uri_encrypted",
        "error_code",
        "finished_at",
        "started_at",
        "external_id_source",
        "max_attempts",
        "retryability",
        "media_bytes",
        "media_storage_key",
        "media_identity",
        "idempotency_key",
    ):
        op.drop_column("publish_attempts", column)

    op.drop_index("uq_publish_targets_channel", table_name="publish_targets")
    for column in (
        "last_error_code",
        "last_used_at",
        "connected_at",
        "granted_scopes",
        "refresh_token_encrypted",
        "connection_status",
        "channel_title",
        "channel_id",
    ):
        op.drop_column("publish_targets", column)

    op.drop_index("ix_oauth_states_state", table_name="oauth_states")
    op.drop_table("oauth_states")

    postgresql.ENUM(name="publish_target_connection_status_enum").drop(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(name="publish_retryability_enum").drop(op.get_bind(), checkfirst=True)
    # The labels added to publish_attempt_status_enum are deliberately NOT removed:
    # PostgreSQL cannot drop an enum value, and recreating the type would require rewriting
    # every row that uses it. A downgrade leaves four unused labels behind, which is inert.
