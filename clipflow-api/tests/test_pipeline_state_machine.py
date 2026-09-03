"""Pure-logic tests for the pipeline state machine (no DB/Redis required)."""
import pytest

from app.models.enums import PipelineState
from app.services.pipeline_state_machine import (
    ALLOWED_TRANSITIONS,
    HAPPY_PATH,
    TERMINAL_STATES,
    WORKER_STAGE_TO_STATE,
    InvalidTransitionError,
    assert_can_transition,
    can_transition,
    is_terminal,
)


def test_every_state_is_in_transition_table():
    for state in PipelineState:
        assert state in ALLOWED_TRANSITIONS


def test_happy_path_is_fully_connected():
    for src, dst in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False):
        assert can_transition(src, dst), f"{src} should reach {dst}"


def test_any_non_terminal_can_fail_and_cancel():
    non_terminal = [s for s in HAPPY_PATH if s != PipelineState.PUBLISHED]
    for state in non_terminal:
        assert can_transition(state, PipelineState.FAILED)
        assert can_transition(state, PipelineState.CANCELED)


def test_terminal_states_have_no_outgoing_transitions():
    assert is_terminal(PipelineState.PUBLISHED)
    assert is_terminal(PipelineState.CANCELED)
    assert ALLOWED_TRANSITIONS[PipelineState.PUBLISHED] == frozenset()
    assert ALLOWED_TRANSITIONS[PipelineState.CANCELED] == frozenset()


def test_failed_retries_by_re_entering_the_queue():
    """PR-STATE-01 changed where a retry re-enters.

    It used to jump straight to DOWNLOADING. But a retry is the reliable queue re-delivering
    the same payload, which a worker then has to claim — so the run goes back to QUEUED and
    reaches DOWNLOADING again only when someone actually picks it up. Anything else would
    show a job as downloading while it sat in the queue.
    """
    assert can_transition(PipelineState.FAILED, PipelineState.QUEUED)
    assert not can_transition(PipelineState.FAILED, PipelineState.DOWNLOADING)
    assert can_transition(PipelineState.FAILED, PipelineState.CANCELED)
    # FAILED is recoverable, not terminal.
    assert not is_terminal(PipelineState.FAILED)


def test_illegal_skip_transition_is_rejected():
    assert not can_transition(PipelineState.DISCOVERED, PipelineState.RENDERING)
    assert not can_transition(PipelineState.PUBLISHED, PipelineState.DOWNLOADING)


def test_assert_can_transition_raises_on_illegal_edge():
    with pytest.raises(InvalidTransitionError):
        assert_can_transition(PipelineState.DISCOVERED, PipelineState.PUBLISHED)


def test_assert_can_transition_passes_on_legal_edge():
    # SELECTED now reaches work through QUEUED: a run that has been chosen still has to wait
    # for a worker, and that wait is a state rather than a gap.
    assert_can_transition(PipelineState.SELECTED, PipelineState.QUEUED)
    assert_can_transition(PipelineState.QUEUED, PipelineState.DOWNLOADING)


def test_worker_stage_map_targets_are_valid_states():
    for state in WORKER_STAGE_TO_STATE.values():
        assert isinstance(state, PipelineState)


def test_terminal_set_contents():
    assert frozenset({PipelineState.PUBLISHED, PipelineState.CANCELED}) == TERMINAL_STATES
