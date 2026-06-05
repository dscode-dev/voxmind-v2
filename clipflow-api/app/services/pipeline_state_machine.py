"""Pipeline state machine for autonomous PipelineJobs.

The transition table is the contract. Validation is pure (no DB/Redis), so it can be tested in
isolation; ``PipelineStateMachine.transition`` is the thin integration wrapper that updates the
row and emits a STATE_CHANGED ``PipelineEvent`` via the event bus.

Not yet wired into the live worker — that is Phase 5. This module + its tests prove the
contract first.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.enums import PipelineEventType, PipelineState
from app.models.pipeline_event import PipelineEvent
from app.models.pipeline_job import PipelineJob
from app.services import event_bus

# Linear happy path (brief §PIPELINE STATE MACHINE).
HAPPY_PATH: list[PipelineState] = [
    PipelineState.DISCOVERED,
    PipelineState.SELECTED,
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

TERMINAL_STATES: frozenset[PipelineState] = frozenset(
    {PipelineState.PUBLISHED, PipelineState.CANCELED}
)


def _build_allowed_transitions() -> dict[PipelineState, frozenset[PipelineState]]:
    transitions: dict[PipelineState, set[PipelineState]] = {}
    for index, state in enumerate(HAPPY_PATH):
        nxt: set[PipelineState] = set()
        if index + 1 < len(HAPPY_PATH):
            nxt.add(HAPPY_PATH[index + 1])
        # Any non-terminal state can fail or be canceled.
        nxt.add(PipelineState.FAILED)
        nxt.add(PipelineState.CANCELED)
        transitions[state] = nxt

    # PUBLISHED is terminal.
    transitions[PipelineState.PUBLISHED] = set()
    # FAILED can be retried (re-enters the download stage) or canceled.
    transitions[PipelineState.FAILED] = {PipelineState.DOWNLOADING, PipelineState.CANCELED}
    # CANCELED is terminal.
    transitions[PipelineState.CANCELED] = set()

    return {state: frozenset(targets) for state, targets in transitions.items()}


ALLOWED_TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = _build_allowed_transitions()


# Maps the existing worker step names (worker `_mark_step` in pipeline.py) onto V2 states, so
# the Phase 5 worker refactor can translate its progress into the state machine.
WORKER_STAGE_TO_STATE: dict[str, PipelineState] = {
    "download_video": PipelineState.DOWNLOADING,
    "upload_video": PipelineState.DOWNLOADED,
    "transcribe": PipelineState.TRANSCRIBING,
    "diarization": PipelineState.TRANSCRIBING,
    "chunk": PipelineState.ANALYZING,
    "hook_detection": PipelineState.ANALYZING,
    "audio_peak_detection": PipelineState.ANALYZING,
    "story_shift_detection": PipelineState.ANALYZING,
    "candidate_build": PipelineState.ANALYZING,
    "candidate_score": PipelineState.ANALYZING,
    "span_catalog": PipelineState.ANALYZING,
    "prompt_build": PipelineState.PROMPT_BUILDING,
    "raw_edit_prompt_build": PipelineState.PROMPT_BUILDING,
    "send_prompt": PipelineState.WAITING_AI,
    "validate_ai_response": PipelineState.AI_COMPLETED,
    "render_cuts": PipelineState.RENDERING,
    "send_cuts": PipelineState.RENDERED,
}


class InvalidTransitionError(Exception):
    """Raised when a state transition is not permitted by ALLOWED_TRANSITIONS."""

    def __init__(self, src: PipelineState, dst: PipelineState) -> None:
        self.src = src
        self.dst = dst
        super().__init__(f"Invalid pipeline transition: {src.value} -> {dst.value}")


def is_terminal(state: PipelineState) -> bool:
    return state in TERMINAL_STATES


def can_transition(src: PipelineState, dst: PipelineState) -> bool:
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def assert_can_transition(src: PipelineState, dst: PipelineState) -> None:
    if not can_transition(src, dst):
        raise InvalidTransitionError(src, dst)


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
        from_state = job.state
        assert_can_transition(from_state, to_state)

        now = datetime.now(timezone.utc)
        job.state = to_state

        if to_state == PipelineState.DOWNLOADING and job.started_at is None:
            job.started_at = now
        if to_state in TERMINAL_STATES or to_state == PipelineState.FAILED:
            job.finished_at = now
        if to_state == PipelineState.DOWNLOADING and from_state == PipelineState.FAILED:
            job.retry_count = (job.retry_count or 0) + 1
            job.finished_at = None

        event_payload = {"from": from_state.value, "to": to_state.value}
        if payload:
            event_payload.update(payload)

        event = event_bus.publish_event(
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
        return event
