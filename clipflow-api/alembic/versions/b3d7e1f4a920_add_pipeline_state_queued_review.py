"""Add QUEUED and REVIEW_REQUIRED to pipeline_state_enum.

PR-STATE-01 makes PipelineJob the authoritative lifecycle, which exposed two gaps in the
state set:

* **QUEUED** — the machine had no way to say "accepted, waiting for a worker". A run's first
  observable state was DOWNLOADING, i.e. after the work had already started, so the window
  between enqueue and claim was invisible.
* **REVIEW_REQUIRED** — a run whose output did not clear PR-QA-01's technical gate has
  finished, but it is not ready to publish. Without this it would have to rest in
  READY_TO_PUBLISH, a state whose name asserts the opposite of what the gate decided.

Nothing else changes: no table is altered, no row is rewritten, and no history is fabricated
for jobs that ran before this PR.

Revision ID: b3d7e1f4a920
Revises: f1a2b3c4d5e6
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b3d7e1f4a920"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The labels in this type are the Python enum MEMBER NAMES, not their values: the original
# migration declared them as "DISCOVERED", "SELECTED", ... and SQLAlchemy's Enum() persists
# `.name` by default. Adding lowercase labels here would create a second, unusable set.
#
# Positioned next to the members they sit beside in Python, so `\dT+` reads in lifecycle
# order rather than append order.
_NEW_VALUES = (
    ("QUEUED", "SELECTED"),
    ("REVIEW_REQUIRED", "READY_TO_PUBLISH"),
)


def upgrade() -> None:
    for value, after in _NEW_VALUES:
        # IF NOT EXISTS keeps the migration re-runnable against a database where an earlier
        # partial run already added the label.
        op.execute(
            f"ALTER TYPE pipeline_state_enum ADD VALUE IF NOT EXISTS '{value}' AFTER '{after}'"
        )


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type. Reversing this means recreating the
    # type and rewriting every column that uses it — destructive, and pointless for two
    # additive labels. Left as a no-op deliberately rather than pretending to be reversible.
    pass
