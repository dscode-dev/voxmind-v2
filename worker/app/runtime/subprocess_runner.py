"""Subprocess execution with timeouts and diagnosable failures.

Every external tool the pipeline shells out to (yt-dlp, ffmpeg, ffprobe) previously ran with
no timeout and, in most call sites, ``stderr=DEVNULL``. A hung process blocked the single
worker thread forever, and a failed one raised ``CalledProcessError`` carrying no message.

This module keeps the commands exactly as they were and changes only how they are executed:

* a per-tool timeout, after which the process is killed and a diagnosable error is raised;
* stderr is always captured, but only surfaced **on failure**, truncated, so a successful
  run stays quiet and a failed one is explainable.
"""
from __future__ import annotations

import subprocess
from typing import Sequence

from app.observability import get_logger
from app.settings import settings

logger = get_logger(__name__)


class SubprocessError(RuntimeError):
    """Base class for external-command failures, carrying captured diagnostics."""

    def __init__(self, message: str, *, command: Sequence[str], stderr: str = "") -> None:
        super().__init__(message)
        self.command = list(command)
        self.stderr = stderr


class SubprocessTimeout(SubprocessError):
    """The command exceeded its timeout and was killed. Treated as retryable."""


class SubprocessFailed(SubprocessError):
    """The command exited non-zero."""


def truncate_stderr(text: str | bytes | None, limit: int | None = None) -> str:
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    limit = limit or settings.subprocess_stderr_capture_chars
    text = text.strip()
    if len(text) <= limit:
        return text
    # Keep the tail: ffmpeg and yt-dlp report the actual cause on their last lines.
    return f"... [truncated {len(text) - limit} chars] " + text[-limit:]


def run_command(
    command: Sequence[str],
    *,
    timeout: float,
    step: str,
    job_id: str | None = None,
    cwd: str | None = None,
    check: bool = True,
    capture_stdout: bool = False,
) -> subprocess.CompletedProcess:
    """Run an external command with a timeout, capturing stderr for diagnostics.

    Raises SubprocessTimeout or SubprocessFailed, both carrying the truncated stderr.
    """
    extra = {"job_id": job_id, "step": step}

    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            timeout=timeout,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = truncate_stderr(exc.stderr)
        logger.error(
            f"{command[0]} timed out after {timeout:.0f}s",
            extra={**extra, "status": "timeout"},
        )
        if stderr:
            logger.error(
                f"{command[0]} stderr: {stderr}",
                extra={**extra, "status": "timeout"},
            )
        raise SubprocessTimeout(
            f"{command[0]} timed out after {timeout:.0f}s during {step}",
            command=command,
            stderr=stderr,
        ) from exc

    if check and completed.returncode != 0:
        stderr = truncate_stderr(completed.stderr)
        logger.error(
            f"{command[0]} exited with code {completed.returncode}",
            extra={**extra, "status": "failed"},
        )
        if stderr:
            logger.error(
                f"{command[0]} stderr: {stderr}",
                extra={**extra, "status": "failed"},
            )
        raise SubprocessFailed(
            f"{command[0]} failed with exit code {completed.returncode} during {step}",
            command=command,
            stderr=stderr,
        )

    return completed


def run_ffmpeg(command: Sequence[str], *, step: str, job_id: str | None = None, cwd: str | None = None):
    return run_command(
        command,
        timeout=settings.ffmpeg_timeout_sec,
        step=step,
        job_id=job_id,
        cwd=cwd,
    )


def run_ffprobe(command: Sequence[str], *, step: str, job_id: str | None = None):
    return run_command(
        command,
        timeout=settings.ffprobe_timeout_sec,
        step=step,
        job_id=job_id,
        capture_stdout=True,
    )


def run_download(command: Sequence[str], *, step: str, job_id: str | None = None):
    return run_command(
        command,
        timeout=settings.download_timeout_sec,
        step=step,
        job_id=job_id,
    )
