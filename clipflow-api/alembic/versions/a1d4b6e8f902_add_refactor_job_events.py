"""add refactor job events

Revision ID: a1d4b6e8f902
Revises: 8f3c2d1ab4c7
Create Date: 2026-03-19 00:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a1d4b6e8f902"
down_revision = "8f3c2d1ab4c7"
branch_labels = None
depends_on = None


NEW_JOB_EVENT_TYPES = [
    "DIARIZATION_STARTED",
    "DIARIZATION_FINISHED",
    "QA_STARTED",
    "QA_FINISHED",
    "DELIVERY_PACKAGE_READY",
]


def upgrade() -> None:
    for event_type in NEW_JOB_EVENT_TYPES:
        op.execute(
            sa.text(
                f"ALTER TYPE job_event_type_enum ADD VALUE IF NOT EXISTS '{event_type}'"
            )
        )


def downgrade() -> None:
    pass
