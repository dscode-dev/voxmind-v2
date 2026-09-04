"""Deciding whether a run has finished publishing, in one place.

Three services used to answer this, two of them with the same wrong test — ``all existing
attempts succeeded``. That test is true of a four-clip run with two attempts, and it marked
such runs PUBLISHED with two videos never uploaded.

The question is now asked once, against the run's required set:

    required     what the manifest says this run owes, decided before anything was published
    attempts     what has actually been tried, for one target
    outstanding  required items with no attempt at all - the only ones anything may create

**Target-scoped.** A publication to channel B cannot satisfy channel A's manifest. Today one
topic has one automatic target, so the distinction rarely bites, but it is the kind that is
silently wrong rather than loudly wrong when it does.

**Initiator-blind.** A clip published by an operator and one published by automation are both
external successes. Who decided is a budget question, not a completion question.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.enums import PublishAttemptStatus
from app.models.pipeline_job import PipelineJob
from app.models.publish_attempt import PublishAttempt
from app.publishing.manifest import PublicationManifest, RequiredItem

logger = logging.getLogger(__name__)

# What the run's publications add up to. A domain result, deliberately not a PipelineJob
# state: the run has three states and this has six, because "why is it not finished" is a
# different question from "where is it in the pipeline".
NOT_STARTED = "not_started"
IN_PROGRESS = "in_progress"
PARTIAL = "partial"
COMPLETE = "complete"
BLOCKED = "blocked"
UNRESOLVED = "unresolved"

# Attempt statuses that mean the item is being worked on right now. Nothing may create a
# replacement for these, and the run counts as actively publishing.
ACTIVE_STATUSES = (
    PublishAttemptStatus.PENDING,
    PublishAttemptStatus.IN_PROGRESS,
)


@dataclass
class ItemState:
    """One required item and what has happened to it."""

    item: RequiredItem
    attempt: PublishAttempt | None = None

    @property
    def status(self) -> str:
        if self.attempt is None:
            return "outstanding"
        if self.attempt.status == PublishAttemptStatus.SUCCEEDED:
            return "succeeded"
        if self.attempt.needs_human:
            return "unresolved"
        if self.attempt.status == PublishAttemptStatus.FAILED_FINAL:
            return "failed_final"
        if self.attempt.status == PublishAttemptStatus.CANCELED:
            return "canceled"
        if self.attempt.status == PublishAttemptStatus.FAILED_RETRYABLE:
            # The publish queue owns the retry, and nothing here may create a replacement -
            # that would be a second logical publication for the same media.
            #
            # But only while a retry is actually coming. Once the attempt budget is spent the
            # command has been dead-lettered and nothing will pick it up again, so counting it
            # as active work would leave the run in PUBLISHING for ever with nothing running.
            if (self.attempt.attempt_no or 0) < (self.attempt.max_attempts or 0):
                return "retry_pending"
            return "retry_exhausted"
        return "in_flight"

    def as_dict(self) -> dict[str, Any]:
        return {
            "media_identity": self.item.media_identity,
            "video_index": self.item.video_index,
            "status": self.status,
            "attempt_id": str(self.attempt.id) if self.attempt else None,
            "external_id": self.attempt.external_id if self.attempt else None,
        }


@dataclass
class CompletionResult:
    status: str
    required_count: int = 0
    succeeded_count: int = 0
    in_flight_count: int = 0
    retryable_count: int = 0
    unresolved_count: int = 0
    final_failed_count: int = 0
    exhausted_count: int = 0
    canceled_count: int = 0
    missing_count: int = 0
    manifest_version: int = 0
    items: list[ItemState] = field(default_factory=list)

    @property
    def outstanding_items(self) -> list[RequiredItem]:
        """Required items nothing has attempted. The only ones anything may allocate."""
        return [state.item for state in self.items if state.attempt is None]

    @property
    def is_complete(self) -> bool:
        """The one condition that may promote a run to PUBLISHED.

        ``required_count > 0`` matters: a manifest that somehow ended up empty must not read
        as "everything required is done".
        """
        return self.required_count > 0 and self.succeeded_count == self.required_count

    @property
    def has_active_work(self) -> bool:
        """Something is queued, uploading, or waiting on a scheduled retry."""
        return bool(self.in_flight_count or self.retryable_count)

    def external_ids(self) -> list[str]:
        return [
            state.attempt.external_id
            for state in self.items
            if state.attempt is not None and state.attempt.external_id
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "required": self.required_count,
            "succeeded": self.succeeded_count,
            "outstanding": self.missing_count,
            "in_flight": self.in_flight_count,
            "retry_pending": self.retryable_count,
            "unresolved": self.unresolved_count,
            "blocked": self.final_failed_count + self.canceled_count,
            "retry_exhausted": self.exhausted_count,
            "manifest_version": self.manifest_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.summary(), "items": [state.as_dict() for state in self.items]}


class PublicationCompletionEvaluator:
    """The single answer to 'is this run finished publishing, and if not, why not?'"""

    def evaluate(
        self,
        db: Session,
        job: PipelineJob,
        *,
        manifest: PublicationManifest,
        target_id: Any,
        attempts: list[PublishAttempt] | None = None,
    ) -> CompletionResult:
        if attempts is None:
            attempts = (
                db.query(PublishAttempt)
                .filter(
                    PublishAttempt.pipeline_job_id == job.id,
                    PublishAttempt.target_id == target_id,
                )
                .all()
            )
        else:
            # Pre-fetched by a caller walking many runs; still scoped here so a broader
            # fetch cannot leak another target's publication into this answer.
            attempts = [a for a in attempts if str(a.target_id) == str(target_id)]

        by_identity = {a.media_identity: a for a in attempts if a.media_identity}
        states = [
            ItemState(item=item, attempt=by_identity.get(item.media_identity))
            for item in manifest.ordered()
        ]

        counts = {key: 0 for key in (
            "succeeded", "outstanding", "in_flight", "retry_pending",
            "retry_exhausted", "unresolved", "failed_final", "canceled",
        )}
        for state in states:
            counts[state.status] += 1

        result = CompletionResult(
            status=NOT_STARTED,
            required_count=len(states),
            succeeded_count=counts["succeeded"],
            in_flight_count=counts["in_flight"],
            retryable_count=counts["retry_pending"],
            unresolved_count=counts["unresolved"],
            # An exhausted retry will never succeed on its own, so it blocks completion
            # exactly as a final failure does. Kept in its own count because the operator
            # action differs: one needs the metadata fixing, the other a deliberate re-run.
            final_failed_count=counts["failed_final"] + counts["retry_exhausted"],
            exhausted_count=counts["retry_exhausted"],
            canceled_count=counts["canceled"],
            missing_count=counts["outstanding"],
            manifest_version=manifest.version,
            items=states,
        )
        result.status = self._status(result)
        return result

    @staticmethod
    def _status(result: CompletionResult) -> str:
        """Ordered by what needs a decision, not by what is most common.

        ``unresolved`` outranks everything short of complete because it is the state that
        needs a person, and a publication that may or may not exist must never be reported
        inside a word as ordinary as "partial".
        """
        if result.required_count == 0:
            return NOT_STARTED
        if result.is_complete:
            return COMPLETE
        if result.unresolved_count:
            return UNRESOLVED
        if result.final_failed_count or result.canceled_count:
            # Required items that will never succeed without a human changing something.
            return BLOCKED
        if result.has_active_work:
            return IN_PROGRESS
        if result.succeeded_count:
            # Some of it is done and the rest is simply waiting to be allocated. Normal, and
            # emphatically not a failure - a budget spreading a large run over days looks
            # exactly like this.
            return PARTIAL
        return NOT_STARTED

    @staticmethod
    def log(job: PipelineJob, target_id: Any, result: CompletionResult) -> None:
        logger.info(
            "publication_completion",
            extra={
                "pipeline_job_id": str(job.id),
                "publish_target_id": str(target_id),
                "required_count": result.required_count,
                "succeeded_count": result.succeeded_count,
                "outstanding_count": result.missing_count,
                "in_flight_count": result.in_flight_count,
                "retryable_count": result.retryable_count,
                "blocked_count": result.final_failed_count + result.canceled_count,
                "unresolved_count": result.unresolved_count,
                "completion_status": result.status,
                "manifest_version": result.manifest_version,
            },
        )
