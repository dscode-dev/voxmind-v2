from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AutomationState(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Scheduling state for one ContentTopic's autonomous loop.

    A real table rather than more keys in ``ContentTopic.metadata_json``, for three reasons
    that only show up once a scheduler is actually running:

    * **``next_due_at`` has to be queried.** Finding due topics is ``WHERE next_due_at <= now``
      over an index. Reaching into a JSONB blob for that works on PostgreSQL and not on SQLite,
      and it cannot be indexed usefully either way.
    * **A JSONB column is read-modify-write.** Two writers touching different keys of the same
      blob silently lose one of the updates. Distinct columns do not have that failure.
    * **It is not the topic's business.** ``metadata_json`` holds editorial configuration a
      human edits; this holds machine bookkeeping a scheduler rewrites every tick. Mixing them
      means an operator editing the topic can clobber the schedule.

    This is scheduling state, not a workflow engine: there is no run history table, no task
    graph and no step records. Automation runs are reported through events and logs; only what
    the *next* tick needs to decide lives here.
    """

    __tablename__ = "automation_states"
    __table_args__ = (
        # The scheduler's only hot query.
        Index("ix_automation_states_next_due", "next_due_at"),
    )

    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_topics.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # When this topic may next run. NULL means "never scheduled", which is treated as due —
    # a newly configured topic should not have to wait a full interval for its first run.
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # completed | partial | failed | skipped | noop
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_automation_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Set while a run is in flight and cleared when it ends. This is what makes an overlapping
    # tick detectable: the advisory lock protects concurrent *processes*, but a run that
    # outlives its own interval would otherwise be re-entered by the next tick of the same
    # process the moment the lock is released.
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    running_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Backoff. Reset to 0 on any run that is not a failure, so a topic recovers immediately
    # rather than serving out a penalty it no longer deserves.
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Per-source cooldowns live here: {"<source_id>": "<iso timestamp>"}. A provider that
    # reported an exhausted quota must not be asked again every minute for the rest of the
    # day — the allowance resets on a clock, not on a retry.
    source_cooldowns_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
