"""The authoritative run lifecycle (PR-STATE-01).

Covers what the state machine has to get right once it is load-bearing: a real run's happy
path, idempotent duplicate reports, refused regressions, retry semantics, terminal failure,
and the composition with PR-QA-01's publication gate.
"""
from __future__ import annotations

import pytest

from app.models.enums import PipelineEventType, PipelineState
from app.models.pipeline_event import PipelineEvent
from app.services.pipeline_job_service import PipelineJobService
from app.services.pipeline_state_machine import (
    APPLIED,
    COMPLETION_STATES,
    DUPLICATE,
    INVALID,
    NOT_MAPPED,
    REGRESSION_IGNORED,
    WORKER_STAGE_TO_STATE,
    PipelineStateMachine,
    is_backwards,
    is_terminal,
    state_for_stage,
)
from tests.conftest import make_run

machine = PipelineStateMachine()
service = PipelineJobService()

# The states a worker actually walks, in order.
REAL_RUN = [
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
]


def walk(db, job, states):
    for state in states:
        outcome = machine.report(db, job, state)
        assert outcome.applied, f"{job.state} -> {state}: {outcome.detail}"
    return job


def events_for(db, job):
    return (
        db.query(PipelineEvent)
        .filter(PipelineEvent.pipeline_job_id == job.id)
        .order_by(PipelineEvent.created_at.asc(), PipelineEvent.id.asc())
        .all()
    )


# ==========================================================================
# Happy path
# ==========================================================================


def test_a_run_walks_from_queued_to_ready(db, no_event_fanout):
    job = make_run(db)
    walk(db, job, REAL_RUN)

    outcome = machine.complete(
        db, job,
        publication_eligible=True,
        publication_eligibility={"eligible": True, "technical_gate": "pass", "blocked_by": []},
    )

    assert outcome.applied
    assert job.state == PipelineState.READY_TO_PUBLISH
    assert job.finished_at is not None


def test_ready_to_publish_is_not_published(db, no_event_fanout):
    """No publisher exists. Reaching the end of the pipeline is not publication."""
    job = make_run(db)
    walk(db, job, REAL_RUN)
    machine.complete(db, job, publication_eligible=True)

    assert job.state != PipelineState.PUBLISHED
    assert not is_terminal(job.state)


def test_started_at_is_set_when_work_begins(db, no_event_fanout):
    job = make_run(db)
    assert job.started_at is None
    machine.report(db, job, PipelineState.DOWNLOADING)
    assert job.started_at is not None


# ==========================================================================
# Idempotency — at-least-once delivery
# ==========================================================================


def test_a_repeated_report_is_a_no_op(db, no_event_fanout):
    job = make_run(db)
    walk(db, job, [PipelineState.DOWNLOADING, PipelineState.DOWNLOADED, PipelineState.TRANSCRIBING])
    before = len(events_for(db, job))

    outcome = machine.report(db, job, PipelineState.TRANSCRIBING)

    assert outcome.outcome == DUPLICATE
    assert job.state == PipelineState.TRANSCRIBING
    assert len(events_for(db, job)) == before, "a duplicate must not add an event"


def test_many_steps_map_to_one_state_and_report_once(db, no_event_fanout):
    """Seven analysis steps mean one ANALYZING transition, not seven."""
    job = make_run(db)
    walk(db, job, [PipelineState.DOWNLOADING, PipelineState.DOWNLOADED,
                   PipelineState.TRANSCRIBING, PipelineState.TRANSCRIBED])
    before = len(events_for(db, job))

    analysis_steps = [
        step for step, state in WORKER_STAGE_TO_STATE.items()
        if state == PipelineState.ANALYZING
    ]
    assert len(analysis_steps) > 3
    outcomes = [machine.report(db, job, state_for_stage(step)) for step in analysis_steps]

    assert sum(1 for o in outcomes if o.applied) == 1
    assert all(o.outcome == DUPLICATE for o in outcomes[1:])
    assert len(events_for(db, job)) == before + 1


def test_duplicate_is_distinguished_from_invalid(db, no_event_fanout):
    job = make_run(db)
    walk(db, job, REAL_RUN)

    assert machine.report(db, job, PipelineState.RENDERED).outcome == DUPLICATE
    assert machine.report(db, job, PipelineState.PUBLISHING).outcome == INVALID


# ==========================================================================
# Regressions
# ==========================================================================


def test_a_stale_report_cannot_roll_the_run_backwards(db, no_event_fanout):
    """The interleaving this exists to prevent: a late report overwriting a newer state."""
    job = make_run(db)
    walk(db, job, REAL_RUN[:9])
    assert job.state == PipelineState.RENDERING

    outcome = machine.report(db, job, PipelineState.TRANSCRIBING)

    assert outcome.outcome == REGRESSION_IGNORED
    assert job.state == PipelineState.RENDERING, "the run must not move backwards"


def test_a_regression_records_nothing(db, no_event_fanout):
    job = make_run(db)
    walk(db, job, REAL_RUN[:5])
    before = len(events_for(db, job))

    machine.report(db, job, PipelineState.DOWNLOADING)

    assert len(events_for(db, job)) == before


@pytest.mark.parametrize(
    "src,dst,backwards",
    [
        (PipelineState.RENDERING, PipelineState.DOWNLOADING, True),
        (PipelineState.RENDERING, PipelineState.RENDERED, False),
        (PipelineState.QUEUED, PipelineState.DOWNLOADING, False),
        (PipelineState.RENDERED, PipelineState.ANALYZING, True),
    ],
)
def test_backwards_detection(src, dst, backwards):
    assert is_backwards(src, dst) is backwards


def test_a_terminal_run_accepts_nothing(db, no_event_fanout):
    job = make_run(db)
    machine.report(db, job, PipelineState.CANCELED)
    assert is_terminal(job.state)

    for state in (PipelineState.DOWNLOADING, PipelineState.RENDERING, PipelineState.READY_TO_PUBLISH):
        assert machine.report(db, job, state).outcome == INVALID
        assert job.state == PipelineState.CANCELED


# ==========================================================================
# Failure and retry
# ==========================================================================


def test_failure_records_a_bounded_sanitised_message(db, no_event_fanout):
    job = make_run(db)
    machine.report(db, job, PipelineState.DOWNLOADING)

    outcome = machine.fail(
        db, job,
        error_type="SubprocessFailed",
        error_message="ffmpeg failed\n" + ("x" * 5000),
        attempt=1,
        worker_id="worker-1",
    )

    assert outcome.applied
    assert job.state == PipelineState.FAILED
    assert len(job.error_message) <= 500, "a stack trace must not become a status column"
    assert "\n" not in job.error_message
    assert job.finished_at is not None


def test_the_failure_event_carries_attribution(db, no_event_fanout):
    job = make_run(db)
    machine.report(db, job, PipelineState.DOWNLOADING)
    machine.fail(db, job, error_type="ValueError", error_message="bad input",
                 attempt=2, worker_id="worker-7")

    event = events_for(db, job)[-1]
    assert event.payload_json["error_type"] == "ValueError"
    assert event.payload_json["attempt"] == 2
    assert event.worker_id == "worker-7"
    assert "failed_at" in event.payload_json


def test_a_retry_reuses_the_same_run_and_counts_the_attempt(db, no_event_fanout):
    """One PipelineJob spans every attempt; fragmenting it would scatter the history."""
    job = make_run(db)
    run_id = job.id

    machine.report(db, job, PipelineState.DOWNLOADING)
    machine.fail(db, job, error_type="TimeoutError", error_message="timed out")
    assert job.retry_count == 0

    outcome = machine.report(db, job, PipelineState.QUEUED)

    assert outcome.applied
    assert job.id == run_id
    assert job.state == PipelineState.QUEUED
    assert job.retry_count == 1
    assert job.finished_at is None, "a re-queued run is not finished"
    assert job.error_message is None, "the previous attempt's error must not stick"


def test_the_second_attempt_can_walk_the_path_again(db, no_event_fanout):
    job = make_run(db)
    machine.report(db, job, PipelineState.DOWNLOADING)
    machine.fail(db, job, error_type="TimeoutError", error_message="timed out")
    machine.report(db, job, PipelineState.QUEUED)

    walk(db, job, REAL_RUN)
    assert job.state == PipelineState.RENDERED


def test_failing_twice_is_idempotent(db, no_event_fanout):
    job = make_run(db)
    machine.report(db, job, PipelineState.DOWNLOADING)
    machine.fail(db, job, error_type="ValueError", error_message="one")

    outcome = machine.fail(db, job, error_type="ValueError", error_message="two")

    assert outcome.outcome == DUPLICATE
    assert job.error_message == "one"


# ==========================================================================
# Completion and the publication gate
# ==========================================================================


def test_an_ineligible_run_rests_in_review_required(db, no_event_fanout):
    """A blocked render must not be filed under a state whose name says it is ready."""
    job = make_run(db)
    walk(db, job, REAL_RUN)

    machine.complete(
        db, job,
        publication_eligible=False,
        publication_eligibility={
            "eligible": False,
            "technical_gate": "fail",
            "blocked_by": ["final_media:audio_fully_silent"],
        },
    )

    assert job.state == PipelineState.REVIEW_REQUIRED
    assert job.state in COMPLETION_STATES


def test_the_verdict_is_persisted_on_the_run(db, no_event_fanout):
    job = make_run(db)
    walk(db, job, REAL_RUN)
    verdict = {"eligible": False, "technical_gate": "fail", "blocked_by": ["final_media:decode_error"]}

    machine.complete(db, job, publication_eligible=False, publication_eligibility=verdict)

    assert job.metadata_json["publication_eligibility"] == verdict


def test_a_reviewer_can_release_a_reviewed_run(db, no_event_fanout):
    job = make_run(db)
    walk(db, job, REAL_RUN)
    machine.complete(db, job, publication_eligible=False)

    outcome = machine.report(db, job, PipelineState.READY_TO_PUBLISH, service="api")

    assert outcome.applied
    assert job.state == PipelineState.READY_TO_PUBLISH


def test_a_reviewed_run_cannot_re_enter_production(db, no_event_fanout):
    """Re-running is a new run, not a rewind of this one.

    REVIEW_REQUIRED sits off the happy path, so this is refused as an illegal edge rather
    than as a stale report — the distinction only matters for how loudly it is logged.
    """
    job = make_run(db)
    walk(db, job, REAL_RUN)
    machine.complete(db, job, publication_eligible=False)

    outcome = machine.report(db, job, PipelineState.RENDERING)

    assert not outcome.applied
    assert outcome.outcome == INVALID
    assert job.state == PipelineState.REVIEW_REQUIRED


# ==========================================================================
# Stage mapping
# ==========================================================================


def test_every_mapped_stage_targets_a_real_state():
    for stage, state in WORKER_STAGE_TO_STATE.items():
        assert isinstance(state, PipelineState), stage


def test_the_quality_gates_do_not_get_states_of_their_own():
    """PR-QA-01's verdict is a result, not a workflow state (PR-STATE-01 §14)."""
    for stage in ("qa", "final_media_qa", "auto_review"):
        assert WORKER_STAGE_TO_STATE[stage] == PipelineState.RENDERED
    assert not any(state.value.startswith("qa_") for state in PipelineState)


def test_an_unmapped_step_moves_nothing():
    assert state_for_stage("transcript_cache") is None
    assert state_for_stage("") is None
    assert state_for_stage("not_a_real_step") is None


def test_the_mapping_covers_the_steps_the_worker_actually_emits():
    """Guards against the worker growing a step the API has never heard of.

    Read from the worker source rather than hardcoded, so adding a step there without
    deciding what it means for the lifecycle fails here.
    """
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[2] / "worker/app/pipeline/pipeline.py"
    if not source.exists():
        pytest.skip("worker source not available in this checkout")

    emitted = set(re.findall(r'_mark_step\(\s*"([a-z_]+)"', source.read_text(encoding="utf-8")))
    # Steps that are real work but deliberately do not move the lifecycle.
    not_lifecycle = {
        "prepare", "finalize", "pipeline",          # coarse stage bookends
        "transcript_cache", "transcript_cache_store",  # cache probes
    }
    unclassified = emitted - set(WORKER_STAGE_TO_STATE) - not_lifecycle
    assert not unclassified, f"worker steps with no lifecycle decision: {sorted(unclassified)}"


# ==========================================================================
# Producer wiring
# ==========================================================================


def test_creating_a_run_leaves_it_queued(db, no_event_fanout):
    run = service.create_for_enqueue(
        db, worker_job_id="job-123", source_url="https://example.invalid/v",
        origin="api", commit=False,
    )

    assert run.state == PipelineState.QUEUED
    assert run.worker_job_id == "job-123"
    assert run.queued_at is not None
    assert run.metadata_json["origin"] == "api"


def test_creation_emits_a_state_changed_event(db, no_event_fanout):
    run = service.create_for_enqueue(db, worker_job_id="job-1", source_url=None, commit=False)

    events = events_for(db, run)
    assert len(events) == 1
    assert events[0].event_type == PipelineEventType.STATE_CHANGED
    assert events[0].payload_json["from"] == "selected"
    assert events[0].payload_json["to"] == "queued"


def test_a_run_is_resolvable_by_the_queue_job_id(db, no_event_fanout):
    """The join key the worker already has. Telegram jobs have no ClipJob to join on."""
    run = service.create_for_enqueue(db, worker_job_id="tg-abc", source_url=None,
                                     origin="telegram", commit=False)
    db.flush()

    assert service.get_by_worker_job_id(db, "tg-abc").id == run.id
    assert service.resolve(db, worker_job_id="tg-abc").id == run.id
    assert service.resolve(db, pipeline_job_id=str(run.id)).id == run.id


def test_resolving_an_unknown_id_returns_nothing_rather_than_inventing_one(db):
    assert service.get(db, "not-a-uuid") is None
    assert service.get(db, "00000000-0000-0000-0000-000000000000") is None
    assert service.resolve(db, worker_job_id="never-seen") is None


def test_the_serialized_view_reports_the_current_attempt(db, no_event_fanout):
    job = make_run(db, retry_count=2)
    view = service.serialize(job)

    assert view["attempt"] == 3, "attempt is 1-based; retry_count is the number of retries"
    assert view["retry_count"] == 2
    assert view["state"] == "queued"


# ==========================================================================
# Forward skips and re-queueing — both found by the live smoke
# ==========================================================================


def test_a_run_may_skip_a_checkpoint_no_worker_step_produces(db, no_event_fanout):
    """TRANSCRIBING -> ANALYZING must be legal.

    Nothing the worker runs means "transcribed": after diarization it simply starts chunking.
    A strictly-sequential table refused this edge and the run stalled at TRANSCRIBING for
    good — a stall a table validated only against itself cannot show.
    """
    job = make_run(db)
    walk(db, job, [PipelineState.DOWNLOADING, PipelineState.TRANSCRIBING])

    outcome = machine.report(db, job, PipelineState.ANALYZING)

    assert outcome.applied
    assert job.state == PipelineState.ANALYZING


def test_forward_skips_stay_inside_production(db, no_event_fanout):
    """Publication is entered deliberately, one step at a time."""
    job = make_run(db)
    machine.report(db, job, PipelineState.DOWNLOADING)

    assert machine.report(db, job, PipelineState.READY_TO_PUBLISH).applied
    assert machine.report(db, job, PipelineState.PUBLISHED).outcome == INVALID
    assert machine.report(db, job, PipelineState.PUBLISHING).applied


def test_a_retry_can_be_scheduled_at_any_point_in_a_run(db, no_event_fanout):
    """The queue can retry mid-run, and the payload really does return to waiting.

    The stale-report guard was refusing this, so a retried run stayed frozen at whatever
    stage it had reached. Re-queueing is a command from the queue runner, not a progress
    report, so it takes a different door.
    """
    for state in (PipelineState.DOWNLOADING, PipelineState.ANALYZING, PipelineState.RENDERING):
        job = make_run(db)
        walk(db, job, [PipelineState.DOWNLOADING])
        if state != PipelineState.DOWNLOADING:
            machine.report(db, job, state)

        outcome = machine.requeue(db, job, reason="retry", attempt=2)

        assert outcome.applied, f"a run at {state} must be re-queueable"
        assert job.state == PipelineState.QUEUED


def test_re_queueing_a_started_run_counts_another_attempt(db, no_event_fanout):
    job = make_run(db)
    machine.report(db, job, PipelineState.DOWNLOADING)
    machine.report(db, job, PipelineState.ANALYZING)
    assert job.retry_count == 0

    machine.requeue(db, job, reason="retry")

    assert job.retry_count == 1
    assert service.serialize(job)["attempt"] == 2


def test_creating_a_run_is_not_an_attempt(db, no_event_fanout):
    """The initial SELECTED -> QUEUED happens before anything has run."""
    run = service.create_for_enqueue(db, worker_job_id="job-fresh", source_url=None, commit=False)

    assert run.retry_count == 0
    assert service.serialize(run)["attempt"] == 1


def test_re_queueing_is_idempotent(db, no_event_fanout):
    job = make_run(db)
    machine.report(db, job, PipelineState.DOWNLOADING)
    machine.requeue(db, job)

    outcome = machine.requeue(db, job)

    assert outcome.outcome == DUPLICATE
    assert job.retry_count == 1, "a duplicate re-queue must not inflate the attempt count"


def test_a_terminal_run_cannot_be_re_queued(db, no_event_fanout):
    job = make_run(db)
    machine.report(db, job, PipelineState.CANCELED)

    assert machine.requeue(db, job).outcome == INVALID
    assert job.state == PipelineState.CANCELED


def test_no_worker_step_can_reach_the_queue(db, no_event_fanout):
    """Re-queueing is reachable only as a command, so a stage report cannot rewind a run."""
    assert PipelineState.QUEUED not in WORKER_STAGE_TO_STATE.values()
