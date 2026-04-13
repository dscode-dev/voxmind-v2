"""add script jobs

Revision ID: e7b9c4a2d601
Revises: d4a1f2c8e9b3
Create Date: 2026-04-13 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e7b9c4a2d601"
down_revision: Union[str, Sequence[str], None] = "d4a1f2c8e9b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "script_jobs",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("topic", sa.String(length=500), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("target_audience", sa.String(length=300), nullable=True),
        sa.Column("objective", sa.String(length=300), nullable=True),
        sa.Column("tone", sa.String(length=120), nullable=True),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("target_duration_sec", sa.Integer(), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=True),
        sa.Column("input_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_script_jobs_user_id"), "script_jobs", ["user_id"], unique=False)
    op.create_index("ix_script_jobs_user_status", "script_jobs", ["user_id", "status"], unique=False)
    op.create_index("ix_script_jobs_created_at", "script_jobs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_script_jobs_created_at", table_name="script_jobs")
    op.drop_index("ix_script_jobs_user_status", table_name="script_jobs")
    op.drop_index(op.f("ix_script_jobs_user_id"), table_name="script_jobs")
    op.drop_table("script_jobs")
