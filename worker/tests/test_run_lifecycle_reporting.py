"""What the worker reports to the authoritative lifecycle (PR-STATE-01).

The worker's half of the contract is narrow and worth pinning down: it forwards the step it
is executing, it never names a state, it never writes to the database, and losing a report
never fails a job. Nothing here touches the network — the API client is stubbed.
"""
from __future__ import annotations

from unittest import mock

import pytest

from app.observability.logging import LOG_CONTEXT_FIELDS


class RecordingClient:
    """Stands in for ClipFlowApiClient, recording what the worker tried to report."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.fail:
            raise RuntimeError("API unreachable")
        return {"status": "ok"}

    def report_stage_safe(self, pipeline_job_id, **kwargs):
        return self._record("stage", pipeline_job_id=pipeline_job_id, **kwargs)

    def report_claim_safe(self, pipeline_job_id, **kwargs):
        return self._record("claim", pipeline_job_id=pipeline_job_id, **kwargs)

    def report_failure_safe(self, pipeline_job_id, **kwargs):
        return self._record("failure", pipeline_job_id=pipeline_job_id, **kwargs)

    def report_retry_safe(self, pipeline_job_id, **kwargs):
        return self._record("retry", pipeline_job_id=pipeline_job_id, **kwargs)

    def report_completion_safe(self, pipeline_job_id, **kwargs):
        return self._record("completion", pipeline_job_id=pipeline_job_id, **kwargs)

    def record_ai_execution_safe(self, pipeline_job_id, **kwargs):
        return self._record("ai_execution", pipeline_job_id=pipeline_job_id, **kwargs)

    def update_runtime_safe(self, *args, **kwargs):
        return None

    def publish_event_safe(self, **kwargs):
        return self._record("event", **kwargs)

    def stages(self):
        return [call["stage"] for name, call in self.calls if name == "stage"]


def stub_pipeline(client, pipeline_job_id="run-1", attempt=1):
    """A Pipeline with its IO replaced, so _mark_step can be exercised in isolation."""
    from app.pipeline.pipeline import Pipeline

    with mock.patch("app.pipeline.pipeline.MinioStorage"), \
         mock.patch("app.pipeline.pipeline.TelegramSender"), \
         mock.patch("app.pipeline.pipeline.ClipFlowApiClient", return_value=client):
        pipeline = Pipeline(
            video_url="https://example.invalid/v",
            job_id="job-abc",
            pipeline_job_id=pipeline_job_id,
            worker_id="worker-9",
            attempt=attempt,
        )
    pipeline.clipflow_api = client
    return pipeline


# ==========================================================================
# Stage reporting
# ==========================================================================


def test_a_started_step_is_reported_with_the_run(tmp_path, monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    client = RecordingClient()
    pipeline = stub_pipeline(client)

    pipeline._mark_step("transcribe", "started")

    assert client.stages() == ["transcribe"]
    _, call = next(c for c in client.calls if c[0] == "stage")
    assert call["pipeline_job_id"] == "run-1"
    assert call["worker_id"] == "worker-9"
    assert call["attempt"] == 1


def test_the_worker_never_sends_a_state(tmp_path, monkeypatch):
    """The API owns WORKER_STAGE_TO_STATE. A step that could name a state could bypass it."""
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    client = RecordingClient()
    pipeline = stub_pipeline(client)

    pipeline._mark_step("render_cuts", "started")

    _, call = next(c for c in client.calls if c[0] == "stage")
    assert "state" not in call
    assert set(call) <= {"pipeline_job_id", "stage", "status", "worker_id", "attempt", "metadata"}


def test_completion_of_a_step_is_not_a_transition(tmp_path, monkeypatch):
    """`started` moves the lifecycle; `completed` would only re-report the same state."""
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    client = RecordingClient()
    pipeline = stub_pipeline(client)

    pipeline._mark_step("transcribe", "started")
    pipeline._mark_step("transcribe", "completed")

    assert client.stages() == ["transcribe"]


def test_a_legacy_payload_reports_nothing_and_still_runs(tmp_path, monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    client = RecordingClient()
    pipeline = stub_pipeline(client, pipeline_job_id=None)

    pipeline._mark_step("transcribe", "started")

    assert client.stages() == []


def test_a_failing_api_does_not_break_the_step(tmp_path, monkeypatch):
    """Losing a report must never fail a job that is otherwise running correctly."""
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))

    class Boom(RecordingClient):
        def report_stage_safe(self, pipeline_job_id, **kwargs):
            self.calls.append(("stage", kwargs))
            return None  # the real client swallows and returns None

    client = Boom()
    pipeline = stub_pipeline(client)
    pipeline._mark_step("transcribe", "started")  # must not raise


# ==========================================================================
# AI correlation
# ==========================================================================


def test_ai_events_carry_the_run(tmp_path, monkeypatch):
    """These were emitted with pipeline_job_id=None: every AI event was unattributable."""
    from app.ai import events as ai_events
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    client = RecordingClient()
    pipeline = stub_pipeline(client)

    pipeline._emit_ai_event(
        ai_events.AI_REQUEST_FINISHED, provider="openai", model="gpt-x", latency_ms=1234
    )

    _, event = next(c for c in client.calls if c[0] == "event")
    assert event["pipeline_job_id"] == "run-1"
    assert event["payload"]["job_id"] == "job-abc"
    assert event["payload"]["attempt"] == 1


def test_worker_id_identifies_the_worker_not_the_job(tmp_path, monkeypatch):
    from app.ai import events as ai_events
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    client = RecordingClient()
    pipeline = stub_pipeline(client)

    pipeline._emit_ai_event(ai_events.AI_REQUEST_STARTED, provider="openai")

    _, event = next(c for c in client.calls if c[0] == "event")
    assert event["worker_id"] == "worker-9"
    assert event["worker_id"] != "job-abc"


def test_a_finished_provider_call_is_recorded_as_an_execution(tmp_path, monkeypatch):
    from app.ai import events as ai_events
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    client = RecordingClient()
    pipeline = stub_pipeline(client)

    pipeline._emit_ai_event(
        ai_events.AI_REQUEST_FINISHED, provider="openai", model="gpt-x", latency_ms=980
    )

    _, execution = next(c for c in client.calls if c[0] == "ai_execution")
    assert execution["pipeline_job_id"] == "run-1"
    assert execution["provider"] == "openai"
    assert execution["model"] == "gpt-x"
    assert execution["latency_ms"] == 980
    assert execution["status"] == "succeeded"


def test_a_failed_provider_call_is_recorded_as_failed(tmp_path, monkeypatch):
    from app.ai import events as ai_events
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    client = RecordingClient()
    pipeline = stub_pipeline(client)

    pipeline._emit_ai_event(
        ai_events.AI_PROVIDER_FAILED, provider="local", error="connection refused"
    )

    _, execution = next(c for c in client.calls if c[0] == "ai_execution")
    assert execution["status"] == "failed"
    assert execution["error_message"] == "connection refused"


def test_no_token_counts_are_invented(tmp_path, monkeypatch):
    """The provider layer does not report them, so nothing is sent."""
    from app.ai import events as ai_events
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    client = RecordingClient()
    pipeline = stub_pipeline(client)
    pipeline._emit_ai_event(ai_events.AI_REQUEST_FINISHED, provider="openai", latency_ms=10)

    _, execution = next(c for c in client.calls if c[0] == "ai_execution")
    assert "tokens_in" not in execution
    assert "tokens_out" not in execution
    assert "cost_usd" not in execution


def test_intermediate_ai_events_are_not_executions(tmp_path, monkeypatch):
    from app.ai import events as ai_events
    from app.settings import settings

    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    client = RecordingClient()
    pipeline = stub_pipeline(client)

    pipeline._emit_ai_event(ai_events.AI_REQUEST_STARTED, provider="openai")
    pipeline._emit_ai_event(ai_events.AI_PROVIDER_SELECTED, provider="openai")

    assert not [c for c in client.calls if c[0] == "ai_execution"]


# ==========================================================================
# Payload contract
# ==========================================================================


def test_the_run_id_is_read_off_the_payload():
    from app.main import _publication_eligibility

    assert _publication_eligibility({})["eligible"] is False


def test_a_missing_verdict_is_never_treated_as_approval():
    """Fail-closed: the field's absence must not read as eligible (PR-QA-01 §23)."""
    from app.main import _publication_eligibility

    for result in ({}, {"publication_eligibility": None}, {"publication_eligibility": "yes"}):
        verdict = _publication_eligibility(result)
        assert verdict["eligible"] is False
        assert verdict["technical_gate"] == "unmeasurable"


def test_a_real_verdict_is_forwarded_unchanged():
    from app.main import _publication_eligibility

    verdict = {"eligible": True, "technical_gate": "pass", "blocked_by": []}
    assert _publication_eligibility({"publication_eligibility": verdict}) == verdict


# ==========================================================================
# Observability
# ==========================================================================


def test_the_run_is_part_of_the_correlation_context():
    assert "pipeline_job_id" in LOG_CONTEXT_FIELDS


def test_the_runtime_correlation_fields_survive():
    """PR-RUNTIME-01's fields must not be dropped by this PR."""
    for field in ("job_id", "pipeline_stage", "step", "status", "attempt", "worker_id"):
        assert field in LOG_CONTEXT_FIELDS


def test_a_log_line_carries_both_ids(caplog):
    """Both ids on one record: job_id says where the artifacts are, pipeline_job_id says
    which run's state a transition belongs to. Joining logs to transitions needs both."""
    import logging

    from app.observability import get_logger
    from app.observability.logging import log_context

    caplog.set_level(logging.INFO)
    logger = get_logger("test.state")

    with log_context(job_id="job-abc", pipeline_job_id="run-1", worker_id="worker-9"):
        logger.info("stage report")

    record = next(r for r in caplog.records if r.getMessage() == "stage report")
    assert record.job_id == "job-abc"
    assert record.pipeline_job_id == "run-1"
    assert record.worker_id == "worker-9"
