"""Give pipeline_jobs an admission identity, enforced by the database.

PR-ADMISSION-01 lets a selected candidate start production automatically. That makes duplicate
admission a real hazard: a client that times out and retries, two operators clicking at once,
or two scheduled runs overlapping would each read "no run exists" and each insert one.

``SELECT then INSERT`` cannot prevent that — the window between the two statements is exactly
where the other request commits. Only a constraint can, so admissions carry a deterministic
key and the database refuses the second one.

Two columns:

* **admission_key** — ``admit:<candidate_id>:<profile>``, unique where present. Runs created
  by the API, Telegram or the scheduler have no candidate and therefore no key; the index is
  partial so they are unaffected rather than colliding with each other on NULL.
* **enqueued_at** — when the payload actually reached Redis. A row with an admission key and
  no ``enqueued_at`` is an admission that persisted but never dispatched, which is precisely
  the state a recovery pass needs to find. Without it, "queued in the database" and "queued in
  Redis" are indistinguishable.

Revision ID: d5f1c93a72e4
Revises: c4e8a2f60b13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5f1c93a72e4"
down_revision: Union[str, Sequence[str], None] = "c4e8a2f60b13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pipeline_jobs",
        sa.Column("admission_key", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "pipeline_jobs",
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Partial: only admissions have a key, and two runs without one are not duplicates.
    op.create_index(
        "uq_pipeline_jobs_admission_key",
        "pipeline_jobs",
        ["admission_key"],
        unique=True,
        postgresql_where=sa.text("admission_key IS NOT NULL"),
    )

    # Runs that persisted but never reached the queue — the recovery query.
    op.create_index(
        "ix_pipeline_jobs_pending_enqueue",
        "pipeline_jobs",
        ["enqueued_at"],
        unique=False,
        postgresql_where=sa.text("enqueued_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_jobs_pending_enqueue", table_name="pipeline_jobs")
    op.drop_index("uq_pipeline_jobs_admission_key", table_name="pipeline_jobs")
    op.drop_column("pipeline_jobs", "enqueued_at")
    op.drop_column("pipeline_jobs", "admission_key")
