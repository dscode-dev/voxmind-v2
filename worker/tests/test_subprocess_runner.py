"""Subprocess timeouts, exit codes and stderr diagnostics (PR-RUNTIME-01).

No real download or encode happens here: the subprocess boundary is mocked, except for a few
cases that shell out to `python -c`, which is available wherever the tests run.
"""

import subprocess
import sys
from unittest import mock

import pytest

from app.runtime import failures
from app.runtime.subprocess_runner import (
    SubprocessFailed,
    SubprocessTimeout,
    run_command,
    run_ffmpeg,
    run_ffprobe,
    truncate_stderr,
)


# ==========================================================================
# Success
# ==========================================================================


def test_successful_command_returns_the_completed_process():
    result = run_command(
        [sys.executable, "-c", "print('ok')"],
        timeout=30,
        step="unit",
        capture_stdout=True,
    )

    assert result.returncode == 0
    assert b"ok" in result.stdout


def test_success_does_not_log_stderr(caplog):
    """Successful runs stay quiet: no megabytes of ffmpeg banner in the logs."""
    caplog.set_level("DEBUG")

    run_command(
        [sys.executable, "-c", "import sys; sys.stderr.write('noisy banner\\n')"],
        timeout=30,
        step="unit",
    )

    assert "noisy banner" not in caplog.text


# ==========================================================================
# Non-zero exit
# ==========================================================================


def test_non_zero_exit_raises_with_stderr_attached():
    with pytest.raises(SubprocessFailed) as exc:
        run_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('codec not found'); sys.exit(3)",
            ],
            timeout=30,
            step="render",
        )

    assert "exit code 3" in str(exc.value)
    assert "codec not found" in exc.value.stderr


def test_failure_logs_the_captured_stderr(caplog):
    caplog.set_level("ERROR")

    with pytest.raises(SubprocessFailed):
        run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"],
            timeout=30,
            step="render",
            job_id="job-9",
        )

    assert "boom" in caplog.text
    assert any(getattr(r, "job_id", None) == "job-9" for r in caplog.records)


def test_check_false_returns_instead_of_raising():
    result = run_command(
        [sys.executable, "-c", "import sys; sys.exit(4)"],
        timeout=30,
        step="unit",
        check=False,
    )
    assert result.returncode == 4


# ==========================================================================
# Timeout
# ==========================================================================


def test_timeout_raises_subprocess_timeout():
    with pytest.raises(SubprocessTimeout) as exc:
        run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=1,
            step="download",
        )

    assert "timed out after 1s" in str(exc.value)
    assert exc.value.command[0] == sys.executable


def test_timeout_kills_the_process():
    """subprocess.run kills the child on TimeoutExpired; assert we surface that, not hang."""
    with pytest.raises(SubprocessTimeout):
        run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=1,
            step="download",
        )


def test_timeout_is_classified_retryable():
    error = SubprocessTimeout("ffmpeg timed out after 60s", command=["ffmpeg"])
    assert failures.is_retryable(error) is True


# ==========================================================================
# stderr truncation
# ==========================================================================


def test_stderr_is_truncated_and_keeps_the_tail():
    noise = "x" * 50_000 + "REAL CAUSE"
    truncated = truncate_stderr(noise, limit=100)

    assert len(truncated) < 200
    assert "REAL CAUSE" in truncated  # ffmpeg reports the cause on its last lines
    assert "truncated" in truncated


def test_short_stderr_is_untouched():
    assert truncate_stderr("small problem") == "small problem"


def test_empty_stderr_is_safe():
    assert truncate_stderr(None) == ""
    assert truncate_stderr(b"") == ""


def test_bytes_stderr_is_decoded():
    assert "café" in truncate_stderr("café".encode("utf-8"))


# ==========================================================================
# Per-tool timeouts come from settings
# ==========================================================================


@pytest.mark.parametrize(
    "runner, setting_name",
    [
        (run_ffmpeg, "ffmpeg_timeout_sec"),
        (run_ffprobe, "ffprobe_timeout_sec"),
    ],
)
def test_each_tool_uses_its_own_configured_timeout(runner, setting_name):
    from app.settings import settings

    with mock.patch("app.runtime.subprocess_runner.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
        runner(["ffmpeg", "-version"], step="unit")

    assert run.call_args.kwargs["timeout"] == getattr(settings, setting_name)


def test_ffmpeg_and_ffprobe_do_not_share_one_timeout():
    from app.settings import settings

    assert settings.ffprobe_timeout_sec != settings.ffmpeg_timeout_sec


# ==========================================================================
# Failure classification
# ==========================================================================


@pytest.mark.parametrize(
    "error",
    [
        SubprocessTimeout("timed out", command=["ffmpeg"]),
        ConnectionError("connection reset by peer"),
        RuntimeError("Service Unavailable"),
        RuntimeError("rate limit exceeded"),
        RuntimeError("something nobody anticipated"),
    ],
)
def test_transient_and_unknown_failures_are_retryable(error):
    assert failures.is_retryable(error) is True


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Finalize job received without manual_response"),
        RuntimeError("Invalid JSON received from AI"),
        RuntimeError("shorts_content is empty"),
        RuntimeError("No valid cuts after filtering (preset=short_series_portrait)"),
    ],
)
def test_deterministic_failures_are_not_retried(error):
    assert failures.is_retryable(error) is False


def test_unknown_failures_default_to_retryable():
    """Losing work is worse than spending two extra attempts."""

    class WeirdError(Exception):
        pass

    assert failures.classify(WeirdError("???")) == failures.RETRYABLE
