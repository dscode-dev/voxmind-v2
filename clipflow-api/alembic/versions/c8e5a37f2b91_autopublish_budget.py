"""autopublish budget: an explicit, timezone-unambiguous charge date

Revision ID: c8e5a37f2b91
Revises: b7d4f1e93a52
Create Date: PR-AUTONOMY-HARDEN-01

One column and one index. Deliberately not a budget/quota table: the publications themselves
are the authority, and a counter beside them would be a second truth that can drift from the
first. What was missing was a way to ask "which automatic publications belong to today?"
exactly, rather than through a timestamp range whose meaning depends on the session timezone.

Existing rows are backfilled from ``created_at`` for automatic publications only. Manual ones
keep NULL, because they never consume the automatic budget and therefore have no day to be
charged to.
"""
from alembic import op
import sqlalchemy as sa

revision = "c8e5a37f2b91"
down_revision = "b7d4f1e93a52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("publish_attempts", sa.Column("budget_date", sa.Date(), nullable=True))

    # Backfilled at UTC, matching how the enforcement path will read it from here on. The
    # cast is explicit about the timezone rather than relying on the server's.
    op.execute(
        """
        UPDATE publish_attempts
           SET budget_date = (created_at AT TIME ZONE 'UTC')::date
         WHERE initiator = 'automatic'
        """
    )

    op.create_index(
        "ix_publish_attempts_budget",
        "publish_attempts",
        ["budget_date", "initiator"],
    )


def downgrade() -> None:
    op.drop_index("ix_publish_attempts_budget", table_name="publish_attempts")
    op.drop_column("publish_attempts", "budget_date")
