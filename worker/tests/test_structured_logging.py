"""Structured logging preserves correlation fields (PR-RUNTIME-01).

`logging.LoggerAdapter.process` replaces `kwargs["extra"]` with the adapter's own mapping,
so every `extra={"job_id": ...}` in this codebase was discarded and logged as None. These
tests pin the merge that fixes it.
"""

import logging

import pytest

from app.observability.logging import (
    LOG_CONTEXT_FIELDS,
    ContextLoggerAdapter,
    bind_context,
    get_logger,
    log_context,
    reset_context,
)


@pytest.fixture
def captured(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


@pytest.fixture(autouse=True)
def clean_context():
    token = bind_context()
    yield
    reset_context(token)


def only_record(caplog):
    assert len(caplog.records) == 1
    return caplog.records[0]


# ==========================================================================
# The regression
# ==========================================================================


def test_call_site_extra_is_not_discarded(captured):
    logger = get_logger("test.extra")

    logger.info(
        "transcription",
        extra={
            "job_id": "job-abc",
            "pipeline_stage": "prepare",
            "step": "transcription",
            "status": "started",
        },
    )

    record = only_record(captured)
    assert record.job_id == "job-abc"
    assert record.pipeline_stage == "prepare"
    assert record.step == "transcription"
    assert record.status == "started"


def test_a_plain_logging_adapter_would_have_dropped_them(captured):
    """Documents the behaviour this module exists to correct."""
    naive = logging.LoggerAdapter(
        logging.getLogger("test.naive"),
        {field: None for field in LOG_CONTEXT_FIELDS},
    )

    naive.info("x", extra={"job_id": "job-abc", "step": "transcription"})

    record = only_record(captured)
    assert record.job_id is None
    assert record.step is None


def test_every_correlation_field_survives(captured):
    logger = get_logger("test.all")

    logger.info(
        "render",
        extra={
            "job_id": "job-1",
            "pipeline_stage": "finalize",
            "step": "render_cuts",
            "status": "completed",
            "attempt": 2,
            "worker_id": "worker-7",
        },
    )

    record = only_record(captured)
    for field, expected in [
        ("job_id", "job-1"),
        ("pipeline_stage", "finalize"),
        ("step", "render_cuts"),
        ("status", "completed"),
        ("attempt", 2),
        ("worker_id", "worker-7"),
    ]:
        assert getattr(record, field) == expected, field


def test_all_fields_are_always_present_so_the_json_formatter_never_fails(captured):
    get_logger("test.defaults").info("no extra at all")

    record = only_record(captured)
    for field in LOG_CONTEXT_FIELDS:
        assert hasattr(record, field), field


# ==========================================================================
# Ambient context
# ==========================================================================


def test_bound_context_reaches_records_without_a_call_site_extra(captured):
    with log_context(worker_id="worker-9", job_id="job-ctx", attempt=3):
        get_logger("test.ctx").info("claimed")

    record = only_record(captured)
    assert record.worker_id == "worker-9"
    assert record.job_id == "job-ctx"
    assert record.attempt == 3


def test_call_site_extra_wins_over_the_bound_context(captured):
    with log_context(job_id="from-context", step="from-context"):
        get_logger("test.precedence").info("x", extra={"step": "from-call-site"})

    record = only_record(captured)
    assert record.step == "from-call-site"
    assert record.job_id == "from-context"


def test_none_at_the_call_site_does_not_erase_the_bound_context(captured):
    """The subprocess runner passes job_id=None when it has no local value."""
    with log_context(job_id="job-ctx"):
        get_logger("test.none").info("x", extra={"job_id": None, "step": "ffmpeg"})

    record = only_record(captured)
    assert record.job_id == "job-ctx"
    assert record.step == "ffmpeg"


def test_context_is_restored_after_the_block(captured):
    with log_context(job_id="inner"):
        pass
    get_logger("test.restore").info("after")

    assert only_record(captured).job_id is None


def test_adapter_does_not_mutate_its_defaults(captured):
    logger = get_logger("test.isolation")
    assert isinstance(logger, ContextLoggerAdapter)

    logger.info("first", extra={"job_id": "job-1"})
    logger.info("second")

    assert captured.records[0].job_id == "job-1"
    assert captured.records[1].job_id is None
