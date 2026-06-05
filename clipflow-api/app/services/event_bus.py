"""Generic V2 event bus.

`publish_event` persists a `PipelineEvent` row **and** fans it out to the Redis pub/sub
channel `clipflow:events`, which the Ops Center SSE endpoint forwards to the frontend.

Redis fan-out is best-effort: if Redis is unavailable the event is still persisted (and the
Ops Center can backfill from `GET /ops/events`), so emitting an event never breaks a caller.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import redis
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.enums import PipelineEventType
from app.models.pipeline_event import PipelineEvent

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "clipflow:events"

_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Lazy, process-wide Redis client used for event fan-out."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis(
            host=settings.voxmind_redis_host,
            port=settings.voxmind_redis_port,
            decode_responses=True,
        )
    return _redis_client


def serialize_event(event: PipelineEvent) -> dict[str, Any]:
    created_at = event.created_at or datetime.now(timezone.utc)
    return {
        "id": str(event.id),
        "pipeline_job_id": str(event.pipeline_job_id) if event.pipeline_job_id else None,
        "service": event.service,
        "stage": event.stage,
        "type": event.event_type.name if event.event_type else None,
        "message": event.message,
        "payload": event.payload_json,
        "created_at": created_at.isoformat(),
    }


def publish_event(
    db: Session,
    *,
    service: str,
    event_type: PipelineEventType = PipelineEventType.INFO,
    pipeline_job_id: Any = None,
    stage: str | None = None,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
    worker_id: str | None = None,
    commit: bool = False,
) -> PipelineEvent:
    """Persist a PipelineEvent and fan it out to Redis. The caller owns the transaction unless
    ``commit=True``; we ``flush`` so the row gets an id/created_at before fan-out."""
    event = PipelineEvent(
        pipeline_job_id=pipeline_job_id,
        service=service,
        stage=stage,
        event_type=event_type,
        message=message,
        payload_json=payload,
        worker_id=worker_id,
    )
    db.add(event)
    db.flush()

    if commit:
        db.commit()
        db.refresh(event)

    _fan_out(serialize_event(event))
    return event


def _fan_out(payload: dict[str, Any]) -> None:
    try:
        get_redis().publish(EVENTS_CHANNEL, json.dumps(payload))
    except Exception:
        logger.warning("event_bus: Redis fan-out failed (event persisted)", exc_info=True)
