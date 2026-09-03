"""Reads come from the database, not from object storage (PR-STATE-01 §18, §20).

The SSE stream used to call ``JobArtifactSyncService.sync_job`` once every two seconds for
the lifetime of every connection, and that call issues eleven ``stat_object`` probes plus up
to four ``get_object`` downloads. Object presence was also what decided the job's status.

These tests assert the new arrangement directly: the read paths touch MinIO zero times, and
a job with an authoritative run has its state read from the run rather than inferred.
"""
from __future__ import annotations

import uuid
from unittest import mock

import pytest

from app.models.enums import PipelineState
from app.services.pipeline_job_service import PipelineJobService
from app.services.pipeline_state_machine import PipelineStateMachine
from tests.conftest import make_run

machine = PipelineStateMachine()
service = PipelineJobService()


class CountingMinio:
    """A MinIO client that refuses to do anything and counts the attempts."""

    def __init__(self):
        self.calls: list[str] = []

    def stat_object(self, *args, **kwargs):
        self.calls.append("stat_object")
        raise AssertionError("state reads must not probe object storage")

    def get_object(self, *args, **kwargs):
        self.calls.append("get_object")
        raise AssertionError("state reads must not download artifacts")


@pytest.fixture()
def counting_minio(monkeypatch):
    client = CountingMinio()
    monkeypatch.setattr(
        "app.services.job_artifact_sync.Minio", lambda *a, **k: client
    )
    return client


# ==========================================================================
# The hot path
# ==========================================================================


def test_the_stream_frame_for_a_run_touches_no_storage(db, no_event_fanout, counting_minio):
    from app.api.job_events import _run_frame

    run = make_run(db)
    machine.report(db, run, PipelineState.DOWNLOADING)
    machine.report(db, run, PipelineState.DOWNLOADED)
    db.flush()

    job = mock.Mock()
    job.id = "job-1"
    job.status.value = "preparing"

    payload, signature, finished = _run_frame(db, job, run)

    assert counting_minio.calls == [], "the SSE hot path must not call MinIO"
    assert payload["state"] == "downloaded"
    assert payload["state_source"] == "pipeline"
    assert payload["pipeline_job_id"] == str(run.id)
    assert finished is False


def test_the_frame_signature_changes_only_when_something_changed(db, no_event_fanout, counting_minio):
    from app.api.job_events import _run_frame

    run = make_run(db)
    job = mock.Mock()
    job.id = "job-1"
    job.status.value = "preparing"

    _, first, _ = _run_frame(db, job, run)
    _, unchanged, _ = _run_frame(db, job, run)
    assert first == unchanged, "an unchanged run must not re-emit a frame"

    machine.report(db, run, PipelineState.DOWNLOADING)
    db.flush()
    _, moved, _ = _run_frame(db, job, run)
    assert moved != first


def test_the_stream_closes_when_the_run_comes_to_rest(db, no_event_fanout, counting_minio):
    from app.api.job_events import _run_frame

    run = make_run(db, state=PipelineState.RENDERED)
    job = mock.Mock()
    job.id = "job-1"
    job.status.value = "finalizing"

    machine.complete(db, run, publication_eligible=False)
    db.flush()

    _, _, finished = _run_frame(db, job, run)
    assert finished is True, "REVIEW_REQUIRED needs a human; nothing more will arrive"


def test_a_legacy_frame_also_avoids_storage(db, counting_minio):
    """Jobs without a run fall back to JobEvent rows — still no object probes."""
    from app.api.job_events import _legacy_frame
    from app.models.clip_job import ClipJob

    job = mock.Mock(spec=ClipJob)
    # A real UUID: this path queries job_events by id, and the column is typed.
    job.id = uuid.uuid4()
    job.status.value = "preparing"
    job.pipeline_stage = "prepare"
    job.metadata_json = {"runtime": {"updated_at": "2026-01-01T00:00:00Z"}}

    payload, signature, finished = _legacy_frame(db, job)

    assert counting_minio.calls == []
    assert payload["state"] is None
    assert payload["state_source"] == "legacy_artifact_inference"
    assert finished is False


# ==========================================================================
# Artifact sync is no longer the state authority
# ==========================================================================


def test_a_job_with_a_run_is_not_reconciled_from_artifacts(db, no_event_fanout, counting_minio):
    """Eleven object probes must not be allowed to overwrite a validated transition."""
    from app.api.jobs import _refresh_job_from_artifacts
    from app.models.enums import JobStatus

    run = service.create_for_enqueue(db, worker_job_id="job-77", source_url=None, commit=False)
    machine.report(db, run, PipelineState.DOWNLOADING)
    db.flush()

    job = mock.Mock()
    job.id = "job-77"
    job.status = JobStatus.PREPARING

    _refresh_job_from_artifacts(db, job)

    assert counting_minio.calls == []


def test_a_finished_job_is_never_reconciled(db, counting_minio):
    from app.api.jobs import _refresh_job_from_artifacts
    from app.models.enums import JobStatus

    job = mock.Mock()
    job.id = "job-done"
    job.status = JobStatus.COMPLETED

    _refresh_job_from_artifacts(db, job)

    assert counting_minio.calls == []


# ==========================================================================
# The read model
# ==========================================================================


def test_the_read_model_reports_the_authoritative_state(db, no_event_fanout, counting_minio):
    from app.api.jobs import _run_view

    run = service.create_for_enqueue(db, worker_job_id="job-88", source_url=None, commit=False)
    machine.report(db, run, PipelineState.DOWNLOADING)
    machine.report(db, run, PipelineState.DOWNLOADED)
    db.flush()

    job = mock.Mock()
    job.id = "job-88"

    view = _run_view(db, job)

    assert view["state"] == "downloaded"
    assert view["state_source"] == "pipeline"
    assert view["pipeline_job_id"] == str(run.id)
    assert view["attempt"] == 1


def test_a_legacy_job_is_labelled_as_such(db, counting_minio):
    """Authoritative and inferred states must not be mixed silently."""
    from app.api.jobs import _run_view

    job = mock.Mock()
    job.id = "job-from-last-year"

    view = _run_view(db, job)

    assert view["state"] is None
    assert view["state_source"] == "legacy_artifact_inference"
    assert view["pipeline_job_id"] is None


def test_the_read_model_carries_the_publication_verdict(db, no_event_fanout, counting_minio):
    from app.api.jobs import _run_view

    run = service.create_for_enqueue(db, worker_job_id="job-99", source_url=None, commit=False)
    for state in (
        PipelineState.DOWNLOADING, PipelineState.DOWNLOADED, PipelineState.TRANSCRIBING,
        PipelineState.TRANSCRIBED, PipelineState.ANALYZING, PipelineState.PROMPT_BUILDING,
        PipelineState.WAITING_AI, PipelineState.AI_COMPLETED, PipelineState.RENDERING,
        PipelineState.RENDERED,
    ):
        machine.report(db, run, state)
    machine.complete(
        db, run,
        publication_eligible=False,
        publication_eligibility={"eligible": False, "technical_gate": "fail", "blocked_by": ["x"]},
    )
    db.flush()

    job = mock.Mock()
    job.id = "job-99"

    view = _run_view(db, job)

    assert view["state"] == "review_required"
    assert view["publication_eligibility"]["eligible"] is False


def test_the_timeline_comes_from_recorded_transitions(db, no_event_fanout, counting_minio):
    """History is read, not reconstructed from timestamps and object listings."""
    from app.models.pipeline_event import PipelineEvent

    run = service.create_for_enqueue(db, worker_job_id="job-hist", source_url=None, commit=False)
    machine.report(db, run, PipelineState.DOWNLOADING)
    machine.report(db, run, PipelineState.DOWNLOADED)
    db.flush()

    events = (
        db.query(PipelineEvent)
        .filter(PipelineEvent.pipeline_job_id == run.id)
        .order_by(PipelineEvent.created_at.asc(), PipelineEvent.id.asc())
        .all()
    )
    transitions = [(e.payload_json["from"], e.payload_json["to"]) for e in events]

    assert transitions == [
        ("selected", "queued"),
        ("queued", "downloading"),
        ("downloading", "downloaded"),
    ]
    assert counting_minio.calls == []
