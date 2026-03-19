"""add private scheduler tables

Revision ID: d4a1f2c8e9b3
Revises: c2f9e6a7b1d4
Create Date: 2026-03-19 13:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d4a1f2c8e9b3"
down_revision: Union[str, Sequence[str], None] = "c2f9e6a7b1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "private_scheduler_profiles",
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("topic_label", sa.String(length=255), nullable=False),
        sa.Column("discovery_mode", sa.String(length=32), nullable=False),
        sa.Column("clip_mode", sa.String(length=32), nullable=False),
        sa.Column("video_ratio", sa.String(length=32), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("seed_urls_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("schedule_hours_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["billing_products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_private_scheduler_profiles_active", "private_scheduler_profiles", ["is_active"], unique=False)
    op.create_index("ix_private_scheduler_profiles_owner", "private_scheduler_profiles", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_private_scheduler_profiles_owner_user_id"), "private_scheduler_profiles", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_private_scheduler_profiles_product_id"), "private_scheduler_profiles", ["product_id"], unique=False)

    op.create_table(
        "private_scheduler_runs",
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scheduled_slot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("topic_label", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["clip_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["profile_id"], ["private_scheduler_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_private_scheduler_runs_profile_status", "private_scheduler_runs", ["profile_id", "status"], unique=False)
    op.create_index("ix_private_scheduler_runs_slot", "private_scheduler_runs", ["scheduled_slot_at"], unique=False)
    op.create_index(op.f("ix_private_scheduler_runs_job_id"), "private_scheduler_runs", ["job_id"], unique=False)
    op.create_index(op.f("ix_private_scheduler_runs_profile_id"), "private_scheduler_runs", ["profile_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_private_scheduler_runs_profile_id"), table_name="private_scheduler_runs")
    op.drop_index(op.f("ix_private_scheduler_runs_job_id"), table_name="private_scheduler_runs")
    op.drop_index("ix_private_scheduler_runs_slot", table_name="private_scheduler_runs")
    op.drop_index("ix_private_scheduler_runs_profile_status", table_name="private_scheduler_runs")
    op.drop_table("private_scheduler_runs")

    op.drop_index(op.f("ix_private_scheduler_profiles_product_id"), table_name="private_scheduler_profiles")
    op.drop_index(op.f("ix_private_scheduler_profiles_owner_user_id"), table_name="private_scheduler_profiles")
    op.drop_index("ix_private_scheduler_profiles_owner", table_name="private_scheduler_profiles")
    op.drop_index("ix_private_scheduler_profiles_active", table_name="private_scheduler_profiles")
    op.drop_table("private_scheduler_profiles")
