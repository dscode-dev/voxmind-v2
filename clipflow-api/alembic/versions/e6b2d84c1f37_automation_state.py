"""Scheduling state for the autonomous loop.

One row per ContentTopic, holding only what the next tick needs to decide: when the topic is
due, whether a run is currently in flight, how the last one ended, and any per-source cooldown.

A table rather than more keys in ``ContentTopic.metadata_json`` because ``next_due_at`` is the
scheduler's only hot query and has to be indexed, because a JSONB blob is read-modify-write and
loses concurrent updates to different keys, and because machine bookkeeping rewritten every
tick does not belong in the same column an operator edits by hand.

Deliberately not a workflow-engine schema: no run history, no task graph, no step records.
Automation runs are reported through PipelineEvent and structured logs.

Revision ID: e6b2d84c1f37
Revises: d5f1c93a72e4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e6b2d84c1f37"
down_revision: Union[str, Sequence[str], None] = "d5f1c93a72e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "automation_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=True),
        sa.Column("last_automation_run_id", sa.String(length=64), nullable=True),
        sa.Column("running_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("running_run_id", sa.String(length=64), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_cooldowns_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["topic_id"], ["content_topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One scheduling state per topic, enforced rather than assumed: a second row would
        # mean two schedules for one topic and a coin flip over which one the tick reads.
        sa.UniqueConstraint("topic_id", name="uq_automation_states_topic"),
    )
    op.create_index(
        op.f("ix_automation_states_topic_id"), "automation_states", ["topic_id"], unique=False
    )
    op.create_index(
        "ix_automation_states_next_due", "automation_states", ["next_due_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_automation_states_next_due", table_name="automation_states")
    op.drop_index(op.f("ix_automation_states_topic_id"), table_name="automation_states")
    op.drop_table("automation_states")
