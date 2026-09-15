from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AutomationRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One execution of a pipeline's cycle: find, choose, admit, publish.

    The report already existed and was already rich — every stage with its counts and its
    reasons — and it was thrown away. It went out in the HTTP response of whichever call
    triggered the tick and nowhere else, so ``AutomationState`` kept the id of the last run
    and nothing about what it did. An operator asking "did the 3am cycle find anything?" had
    no way to answer except reading logs.

    This is that report, kept. One row per execution, whether it worked, was skipped or
    crashed — a cycle that decided to do nothing is a fact worth being able to see, and it is
    the answer to the most common question of all: "why is nothing happening?"

    **It stays open past the tick.** Admission is where the cycle stops being synchronous: the
    productions it started are then handed to a worker that takes minutes. A run marked
    finished at that point would report success for cuts that had not been rendered yet, so
    the row records what it admitted and is settled later, when those productions reach an end.
    """

    __tablename__ = "automation_runs"
    __table_args__ = (
        # The listing every screen does: this pipeline's runs, newest first.
        Index("ix_automation_runs_pipeline_started", "pipeline_id", "started_at"),
        # Finding the runs still waiting on their productions, without scanning the table.
        Index("ix_automation_runs_open", "production_status"),
    )

    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # The run survives the pipeline being deleted. It describes work that really happened,
        # and a configuration change must not erase the record of it.
        ForeignKey("pipelines.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # What started this: "schedule", "manual" or "tick". Recorded rather than inferred,
    # because "did this run because I pressed the button, or on its own?" is the first
    # question asked about a surprising run.
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="tick")
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The cycle itself: completed, partial, failed, skipped.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    # Why a cycle did nothing. A skipped run with no reason is indistinguishable from a bug.
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The productions this run started, separately from the cycle itself. A cycle can complete
    # perfectly and still be waiting on four renders.
    production_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="none"
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the productions this run started all reached an end. Null while any is in flight.
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Denormalised so a list of runs does not need four joins to say anything useful.
    discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    admitted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # What the cycle put on the publish queue. Queued is not published: the publisher may not
    # even be running, and a column that conflated them would report videos on a channel that
    # nothing had uploaded.
    publications_queued: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Filled when the run settles, from the productions themselves. Zero until then, because
    # until then it is genuinely zero.
    published: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The stage-by-stage report: status, counts and reasons for discovery, selection,
    # admission and publication. JSON because it is a record, not something queried by field.
    stages_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    pipeline = relationship("Pipeline", back_populates="runs")
    jobs = relationship("PipelineJob", back_populates="automation_run")
