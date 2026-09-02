"""Stable per-process worker identity.

Resolution order:
  1. ``WORKER_ID`` when set explicitly (useful for pinning an operator-visible name);
  2. the container hostname, which Docker sets to the container id;
  3. a random suffix, so two processes on one host never collide.

The value is computed once at import and never changes for the life of the process.
"""
from __future__ import annotations

import os
import socket
import uuid

from app.settings import settings


def _resolve_worker_id() -> str:
    configured = str(settings.worker_id or "").strip()
    if configured:
        return configured

    hostname = (socket.gethostname() or "").strip()
    suffix = uuid.uuid4().hex[:8]
    if hostname:
        return f"worker-{hostname}-{suffix}"
    return f"worker-{suffix}"


WORKER_ID: str = _resolve_worker_id()


def worker_id() -> str:
    return WORKER_ID


def worker_pid() -> int:
    return os.getpid()
