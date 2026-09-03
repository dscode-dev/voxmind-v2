"""The authoritative lifecycle of a pipeline run.

A job's state is the result of a validated transition, not an inference from which objects
happen to exist in MinIO. Before PR-STATE-01 it was the other way round: ``JobArtifactSync``
probed eleven object keys and derived a status from what it found, and this module — the
machine that was supposed to own the lifecycle — was called by nothing but its own tests.

    command / worker report
            ↓
    validated transition        ← this module
            ↓
    persisted PipelineJob.state
            ↓
    PipelineEvent  →  Redis fan-out  →  SSE

Three authorities, kept apart:

* **Redis** owns message possession. A claim, a retry and a dead-letter are queue facts, and
  PR-RUNTIME-01 owns them. Nothing here changes them.
* **The worker** owns *facts*: which stage it is executing, whether it succeeded, how it
  failed. It reports those and nothing more.
* **This module** owns *state*. It decides what a reported stage means for the lifecycle,
  whether the transition is legal, and what gets persisted. The worker never writes state.

Delivery is at-least-once: a worker may report the same stage twice (HTTP retry, restart,
duplicate delivery). Transitions are therefore idempotent — a repeat of the current state is
a no-op, not an error — and a report that would move the run *backwards* is refused rather
than applied. See :class:`TransitionOutcome`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.enums import PipelineEventType, PipelineState
from app.models.pipeline_event import PipelineEvent
from app.models.pipeline_job import PipelineJob
from app.services import event_bus

# The linear production path a real run walks.
#
# DISCOVERED and SELECTED sit before it: they belong to the discovery lineage, which does not
# exist yet, so a job created from a submitted URL enters at QUEUED. They are kept in the
# machine (and can still reach QUEUED) so discovery can be wired in later without reopening
# this table.
HAPPY_PATH: list[PipelineState] = [
    PipelineState.QUEUED,
    PipelineState.DOWNLOADING,
    PipelineState.DOWNLOADED,
    PipelineState.TRANSCRIBING,
    PipelineState.TRANSCRIBED,
    PipelineState.ANALYZING,
    PipelineState.PROMPT_BUILDING,
    PipelineState.WAITING_AI,
    PipelineState.AI_COMPLETED,
    PipelineState.RENDERING,
    PipelineState.RENDERED,
    PipelineState.READY_TO_PUBLISH,
    PipelineState.PUBLISHING,
    PipelineState.PUBLISHED,
]

# Where a run can come to rest. PUBLISHED and CANCELED are final. REVIEW_REQUIRED is a
# resting state, not a final one: a person can still approve or cancel it.
TERMINAL_STATES: frozenset[PipelineState] = frozenset(
    {PipelineState.PUBLISHED, PipelineState.CANCELED}
)

# States a completed run can end in. Both mean "the pipeline finished"; they differ on
# whether a human has to look before anything leaves the building.
COMPLETION_STATES: frozenset[PipelineState] = frozenset(
    {PipelineState.READY_TO_PUBLISH, PipelineState.REVIEW_REQUIRED}
)

# Position on the happy path, used to tell a duplicate report from a backwards one.
_ORDER: dict[PipelineState, int] = {state: index for index, state in enumerate(HAPPY_PATH)}


# Where production ends and publication begins. Everything up to and including this state is
# work the worker does; past it is a publisher that does not exist yet.
_PRODUCTION_END = HAPPY_PATH.index(PipelineState.READY_TO_PUBLISH)


def _build_allowed_transitions() -> dict[PipelineState, frozenset[PipelineState]]:
    transitions: dict[PipelineState, set[PipelineState]] = {}

    for index, state in enumerate(HAPPY_PATH):
        nxt: set[PipelineState] = set()
        if index < _PRODUCTION_END:
            # Any state further along the production path, not just the immediate next one.
            #
            # The checkpoint states (DOWNLOADED, TRANSCRIBED, RENDERED) exist to mark that a
            # phase completed, but the worker does not emit a step for every one of them —
            # nothing it runs means "transcribed", it simply moves on to chunking. A
            # strictly-sequential table therefore refuses TRANSCRIBING -> ANALYZING and the
            # run stalls at TRANSCRIBING for the rest of its life. Found by the live smoke,
            # which is exactly the kind of thing a table validated only against itself hides.
            #
            # Reaching a later state implies the ones in between; what stays forbidden is
            # moving BACKWARDS, which is the failure that actually corrupts a timeline.
            nxt.update(HAPPY_PATH[index + 1 : _PRODUCTION_END + 1])
        elif index + 1 < len(HAPPY_PATH):
            # Publication is deliberate and sequential: no skipping into PUBLISHED.
            nxt.add(HAPPY_PATH[index + 1])
        # Any state still in flight can fail or be canceled.
        nxt.add(PipelineState.FAILED)
        nxt.add(PipelineState.CANCELED)
        # ...and can be sent back to the queue. This is the one legitimate backwards move in
        # the system: the reliable queue can schedule a retry at any point in a run, and the
        # payload genuinely returns to waiting. It is reachable only through `requeue()` — a
        # command from the queue runner — never through a stage report, so it cannot be used
        # to walk a run backwards. No worker step maps to QUEUED.
        if index <= _PRODUCTION_END:
            nxt.add(PipelineState.QUEUED)
        transitions[state] = nxt

    # Discovery lineage: unused today, kept connected for when discovery lands.
    transitions[PipelineState.DISCOVERED] = {
        PipelineState.SELECTED,
        PipelineState.CANCELED,
        PipelineState.FAILED,
    }
    transitions[PipelineState.SELECTED] = {
        PipelineState.QUEUED,
        PipelineState.CANCELED,
        PipelineState.FAILED,
    }

    # A finished render whose output did not clear the technical gate waits for a person.
    transitions[PipelineState.RENDERED].add(PipelineState.REVIEW_REQUIRED)
    transitions[PipelineState.REVIEW_REQUIRED] = {
        # A reviewer can release it or drop it. It cannot re-enter production from here:
        # that would be a new run.
        PipelineState.READY_TO_PUBLISH,
        PipelineState.CANCELED,
    }

    # An upload that did not confirm returns the run to where it was before anyone tried.
    # PR-PUBLISH-01: the table had no edge out of PUBLISHING except forward or FAILED, and
    # neither is right for a failed publish. FAILED means the *production* failed, and its
    # only recovery is FAILED -> QUEUED, which would re-run the entire render for what was a
    # network error at the last step. A run whose media is fine and whose upload did not
    # land is still ready to publish, so that is where it goes.
    #
    # Backwards on the happy path, so it is reachable only through the `publish_failed()`
    # command - never through `report()`, whose stale-report guard exists precisely to stop
    # a late message walking a run backwards.
    transitions[PipelineState.PUBLISHING].add(PipelineState.READY_TO_PUBLISH)

    transitions[PipelineState.PUBLISHED] = set()
    transitions[PipelineState.CANCELED] = set()
    # A failed run is retried by re-queueing it, which is what the reliable queue does with
    # the same payload. It re-enters at QUEUED, not mid-pipeline.
    transitions[PipelineState.FAILED] = {PipelineState.QUEUED, PipelineState.CANCELED}

    return {state: frozenset(targets) for state, targets in transitions.items()}


ALLOWED_TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = _build_allowed_transitions()


# Worker steps (``Pipeline._mark_step``) mapped onto lifecycle states.
#
# This map lives here and only here. The worker reports the step name it is actually
# executing and has no opinion about what state that implies — otherwise the mapping would
# have to be kept in sync in two repositories, and the worker could smuggle a state past the
# transition rules by naming it directly.
#
# Steps deliberately absent map to nothing and are recorded as events without moving the
# state (cache lookups, artifact writes, notifications).
WORKER_STAGE_TO_STATE: dict[str, PipelineState] = {
    # Acquisition
    "download_video": PipelineState.DOWNLOADING,
    "upload_video": PipelineState.DOWNLOADED,
    # Transcription
    "transcribe": PipelineState.TRANSCRIBING,
    "diarization": PipelineState.TRANSCRIBING,
    # Analysis
    "chunk": PipelineState.ANALYZING,
    "hook_detection": PipelineState.ANALYZING,
    "audio_peak_detection": PipelineState.ANALYZING,
    "story_shift_detection": PipelineState.ANALYZING,
    "candidate_build": PipelineState.ANALYZING,
    "candidate_score": PipelineState.ANALYZING,
    "span_catalog": PipelineState.ANALYZING,
    # Prompting
    "prompt_build": PipelineState.PROMPT_BUILDING,
    "raw_edit_prompt_build": PipelineState.PROMPT_BUILDING,
    # AI
    "send_prompt": PipelineState.WAITING_AI,
    "ai_request": PipelineState.WAITING_AI,
    "validate_ai_response": PipelineState.AI_COMPLETED,
    "raw_edit_decision": PipelineState.AI_COMPLETED,
    # Rendering
    "render_plan": PipelineState.RENDERING,
    "render_cuts": PipelineState.RENDERING,
    "final_clips": PipelineState.RENDERING,
    "final_reel": PipelineState.RENDERING,
    "final_reel_subtitles": PipelineState.RENDERING,
    "raw_edit_render": PipelineState.RENDERING,
    # Quality gates (PR-QA-01). Both layers run over rendered material, so the run is
    # RENDERED while they execute; their verdict decides the completion state, not a state
    # of its own.
    "qa": PipelineState.RENDERED,
    "final_media_qa": PipelineState.RENDERED,
    "auto_review": PipelineState.RENDERED,
    "delivery_package": PipelineState.RENDERED,
    "publish_package": PipelineState.RENDERED,
    "send_cuts": PipelineState.RENDERED,
}

# Outcomes of a reported transition.
APPLIED = "applied"
DUPLICATE = "duplicate"
REGRESSION_IGNORED = "regression_ignored"
NOT_MAPPED = "not_mapped"
INVALID = "invalid"


@dataclass(frozen=True)
class TransitionOutcome:
    """What a report did, and why.

    The caller needs to distinguish three things a naive implementation conflates: a report
    that moved the run, a repeat of one already applied, and one that arrived out of order.
    Only the last is a problem, and even then it is the *report* that is wrong, not the run.
    """

    outcome: str
    from_state: PipelineState
    to_state: PipelineState | None
    event: PipelineEvent | None = None
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.outcome == APPLIED

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value if self.to_state else None,
            "detail": self.detail,
            "event_id": str(self.event.id) if self.event is not None else None,
        }


class InvalidTransitionError(Exception):
    """Raised when a state transition is not permitted by ALLOWED_TRANSITIONS."""

    def __init__(self, src: PipelineState, dst: PipelineState) -> None:
        self.src = src
        self.dst = dst
        super().__init__(f"Invalid pipeline transition: {src.value} -> {dst.value}")


def is_terminal(state: PipelineState) -> bool:
    return state in TERMINAL_STATES


def is_completion(state: PipelineState) -> bool:
    return state in COMPLETION_STATES


def can_transition(src: PipelineState, dst: PipelineState) -> bool:
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def assert_can_transition(src: PipelineState, dst: PipelineState) -> None:
    if not can_transition(src, dst):
        raise InvalidTransitionError(src, dst)


def state_for_stage(stage: str) -> PipelineState | None:
    """The lifecycle state a worker step implies, or None if the step does not move state."""
    return WORKER_STAGE_TO_STATE.get(str(stage or "").strip())


def is_backwards(src: PipelineState, dst: PipelineState) -> bool:
    """True when ``dst`` sits earlier on the happy path than ``src``.

    Used to tell a stale report from an illegal one. Both are refused, but a stale report is
    an expected consequence of at-least-once delivery and is not an error worth alarming on.
    """
    if src not in _ORDER or dst not in _ORDER:
        return False
    return _ORDER[dst] < _ORDER[src]


class PipelineStateMachine:
    """Applies validated transitions to a PipelineJob and emits a STATE_CHANGED event."""

    def transition(
        self,
        db: Session,
        job: PipelineJob,
        to_state: PipelineState,
        *,
        service: str = "api",
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        worker_id: str | None = None,
        commit: bool = False,
    ) -> PipelineEvent:
        """Force a transition, raising on an illegal edge.

        Direct callers are the ones that *command* a state (a producer queueing work, an
        operator cancelling). Worker reports go through :meth:`report` instead, which is
        tolerant of duplicates and stale deliveries.
        """
        from_state = job.state
        assert_can_transition(from_state, to_state)
        self._apply(job, from_state, to_state)

        event_payload = {"from": from_state.value, "to": to_state.value}
        if payload:
            event_payload.update(payload)

        return event_bus.publish_event(
            db,
            service=service,
            event_type=PipelineEventType.STATE_CHANGED,
            pipeline_job_id=job.id,
            stage=to_state.value,
            message=message or f"{from_state.value} -> {to_state.value}",
            payload=event_payload,
            worker_id=worker_id,
            commit=commit,
        )

    def report(
        self,
        db: Session,
        job: PipelineJob,
        to_state: PipelineState,
        *,
        service: str = "worker",
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        worker_id: str | None = None,
        commit: bool = False,
    ) -> TransitionOutcome:
        """Apply a reported state, tolerating the realities of at-least-once delivery.

        * the state the run is already in — idempotent no-op;
        * a state behind the current one — refused as stale, the run is left alone;
        * a legal forward edge — applied and recorded;
        * anything else — refused as invalid.

        Never raises: a bad report must not take down a worker that is otherwise doing its
        job correctly. The outcome says what happened and the caller decides how loudly to
        complain.
        """
        from_state = job.state

        if from_state == to_state:
            return TransitionOutcome(
                DUPLICATE, from_state, to_state,
                detail=f"already in {to_state.value}",
            )

        if is_terminal(from_state):
            return TransitionOutcome(
                INVALID, from_state, to_state,
                detail=f"{from_state.value} is terminal",
            )

        if is_backwards(from_state, to_state):
            return TransitionOutcome(
                REGRESSION_IGNORED, from_state, to_state,
                detail=(
                    f"{to_state.value} is behind {from_state.value} on the pipeline; "
                    "report ignored as stale"
                ),
            )

        if not can_transition(from_state, to_state):
            return TransitionOutcome(
                INVALID, from_state, to_state,
                detail=f"{from_state.value} -> {to_state.value} is not an allowed edge",
            )

        event = self.transition(
            db, job, to_state,
            service=service, message=message, payload=payload,
            worker_id=worker_id, commit=commit,
        )
        return TransitionOutcome(APPLIED, from_state, to_state, event=event)

    def requeue(
        self,
        db: Session,
        job: PipelineJob,
        *,
        reason: str = "retry",
        attempt: int | None = None,
        worker_id: str | None = None,
        service: str = "worker",
        commit: bool = False,
    ) -> TransitionOutcome:
        """Return a run to the queue for another attempt.

        A *command*, not a progress report, which is why it does not go through `report()`:
        the stale-report guard exists to stop a late stage report from rewinding a run, and a
        retry is the one case where moving back to waiting is the truth. The queue has
        already decided; this records it.
        """
        from_state = job.state
        if from_state == PipelineState.QUEUED:
            return TransitionOutcome(DUPLICATE, from_state, PipelineState.QUEUED, detail="already queued")
        if is_terminal(from_state):
            return TransitionOutcome(
                INVALID, from_state, PipelineState.QUEUED,
                detail=f"{from_state.value} is terminal",
            )
        if not can_transition(from_state, PipelineState.QUEUED):
            return TransitionOutcome(
                INVALID, from_state, PipelineState.QUEUED,
                detail=f"{from_state.value} cannot be re-queued",
            )

        event = self.transition(
            db, job, PipelineState.QUEUED,
            service=service,
            message=f"re-queued: {reason}",
            payload={"reason": reason, "attempt": attempt},
            worker_id=worker_id,
            commit=commit,
        )
        return TransitionOutcome(APPLIED, from_state, PipelineState.QUEUED, event=event)

    def fail(
        self,
        db: Session,
        job: PipelineJob,
        *,
        error_type: str,
        error_message: str,
        attempt: int | None = None,
        worker_id: str | None = None,
        service: str = "worker",
        commit: bool = False,
    ) -> TransitionOutcome:
        """Record a terminal failure of this attempt.

        Only a bounded, sanitised message is persisted: a stack trace in a status column is
        neither queryable nor readable, and the worker's structured logs already carry the
        full diagnostic under the same job_id.
        """
        from_state = job.state
        if from_state == PipelineState.FAILED:
            return TransitionOutcome(DUPLICATE, from_state, PipelineState.FAILED, detail="already failed")
        if is_terminal(from_state):
            return TransitionOutcome(
                INVALID, from_state, PipelineState.FAILED,
                detail=f"{from_state.value} is terminal",
            )

        job.error_message = _sanitize_error(error_message)
        event = self.transition(
            db, job, PipelineState.FAILED,
            service=service,
            message=f"failed: {error_type}",
            payload={
                "error_type": error_type,
                "error_message": job.error_message,
                "attempt": attempt if attempt is not None else job.retry_count,
                "failed_at": datetime.now(timezone.utc).isoformat(),
            },
            worker_id=worker_id,
            commit=commit,
        )
        return TransitionOutcome(APPLIED, from_state, PipelineState.FAILED, event=event)

    # ------------------------------------------------------------------ publishing
    #
    # Three commands rather than `report()` calls, for the same reason `requeue` is a
    # command: publishing is driven by an operator and a provider, not by a worker reporting
    # progress, and one of the three legitimately moves the run backwards.

    def start_publishing(
        self,
        db: Session,
        job: PipelineJob,
        *,
        service: str = "publisher",
        actor: str | None = None,
        commit: bool = False,
    ) -> TransitionOutcome:
        """Claim a run for publication.

        Only from READY_TO_PUBLISH: the transition table refuses REVIEW_REQUIRED, which is
        what stops a run that failed the technical gate from being published by a caller who
        checked eligibility carelessly. Claiming is also how two concurrent publish requests
        are separated - the second finds the run already in PUBLISHING.
        """
        from_state = job.state
        if from_state == PipelineState.PUBLISHING:
            return TransitionOutcome(
                DUPLICATE, from_state, PipelineState.PUBLISHING, detail="already publishing"
            )
        if not can_transition(from_state, PipelineState.PUBLISHING):
            return TransitionOutcome(
                INVALID, from_state, PipelineState.PUBLISHING,
                detail=f"{from_state.value} cannot begin publishing",
            )

        event = self.transition(
            db, job, PipelineState.PUBLISHING,
            service=service,
            message="publishing started",
            payload={"actor": actor},
            commit=commit,
        )
        return TransitionOutcome(APPLIED, from_state, PipelineState.PUBLISHING, event=event)

    def publish_failed(
        self,
        db: Session,
        job: PipelineJob,
        *,
        reason: str,
        service: str = "publisher",
        commit: bool = False,
    ) -> TransitionOutcome:
        """Release a run whose publication did not confirm.

        Back to READY_TO_PUBLISH, not FAILED: the render is intact and the technical gate
        still passed. FAILED would describe the production as broken and its only recovery
        would re-run the whole pipeline.

        An UNKNOWN outcome deliberately does NOT come here - see `publishing_service`. A run
        whose upload may have succeeded must not be returned to a state whose name invites
        someone to publish it again.
        """
        from_state = job.state
        if from_state != PipelineState.PUBLISHING:
            return TransitionOutcome(
                INVALID, from_state, PipelineState.READY_TO_PUBLISH,
                detail=f"{from_state.value} is not publishing",
            )

        event = self.transition(
            db, job, PipelineState.READY_TO_PUBLISH,
            service=service,
            message=f"publishing released: {reason}",
            payload={"reason": reason},
            commit=commit,
        )
        return TransitionOutcome(
            APPLIED, from_state, PipelineState.READY_TO_PUBLISH, event=event
        )

    def mark_published(
        self,
        db: Session,
        job: PipelineJob,
        *,
        external_ids: list[str],
        service: str = "publisher",
        commit: bool = False,
    ) -> TransitionOutcome:
        """Record that every required publication of this run is confirmed.

        The caller establishes "every required" - this refuses to move a run that is not
        currently publishing, and records the external ids that justify the transition, so a
        PUBLISHED run always names the videos that make it so.
        """
        from_state = job.state
        if from_state == PipelineState.PUBLISHED:
            return TransitionOutcome(
                DUPLICATE, from_state, PipelineState.PUBLISHED, detail="already published"
            )
        if not can_transition(from_state, PipelineState.PUBLISHED):
            return TransitionOutcome(
                INVALID, from_state, PipelineState.PUBLISHED,
                detail=f"{from_state.value} -> published is not an allowed edge",
            )

        event = self.transition(
            db, job, PipelineState.PUBLISHED,
            service=service,
            message="published",
            payload={"external_ids": list(external_ids)},
            commit=commit,
        )
        return TransitionOutcome(APPLIED, from_state, PipelineState.PUBLISHED, event=event)

    def complete(
        self,
        db: Session,
        job: PipelineJob,
        *,
        publication_eligible: bool,
        publication_eligibility: dict[str, Any] | None = None,
        worker_id: str | None = None,
        service: str = "worker",
        commit: bool = False,
    ) -> TransitionOutcome:
        """Settle a finished run into its completion state.

        READY_TO_PUBLISH is *not* PUBLISHED, and it is not granted on the strength of the
        pipeline having run: PR-QA-01 computes publication eligibility fail-closed, and a run
        whose output did not clear that gate rests in REVIEW_REQUIRED instead.
        """
        target = (
            PipelineState.READY_TO_PUBLISH if publication_eligible else PipelineState.REVIEW_REQUIRED
        )
        eligibility = publication_eligibility or {}
        metadata = dict(job.metadata_json or {})
        metadata["publication_eligibility"] = eligibility
        job.metadata_json = metadata

        return self.report(
            db, job, target,
            service=service,
            message=f"run complete -> {target.value}",
            payload={"publication_eligible": publication_eligible, **_summary(eligibility)},
            worker_id=worker_id,
            commit=commit,
        )

    # ------------------------------------------------------------------ internals

    def _apply(self, job: PipelineJob, from_state: PipelineState, to_state: PipelineState) -> None:
        now = datetime.now(timezone.utc)
        job.state = to_state

        if to_state == PipelineState.QUEUED:
            job.queued_at = now
            # Re-queueing a run that has already started is another attempt of it — whether
            # it got there by failing outright or by the queue scheduling a retry mid-run.
            # The initial SELECTED -> QUEUED at creation is not, because nothing ran yet.
            if job.started_at is not None:
                job.retry_count = (job.retry_count or 0) + 1
                job.finished_at = None
                job.error_message = None
        if to_state == PipelineState.DOWNLOADING and job.started_at is None:
            job.started_at = now
        if to_state in TERMINAL_STATES or to_state in COMPLETION_STATES or to_state == PipelineState.FAILED:
            job.finished_at = now


def _sanitize_error(message: str) -> str:
    """Keep the failure legible without turning the column into a log sink."""
    text = " ".join(str(message or "").split())
    return text[:500]


def _summary(eligibility: dict[str, Any]) -> dict[str, Any]:
    """A small, safe projection of the QA verdict for the event payload."""
    return {
        "technical_gate": eligibility.get("technical_gate"),
        "blocked_by": list(eligibility.get("blocked_by") or [])[:10],
    }
