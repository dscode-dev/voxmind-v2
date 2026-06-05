"""Operations Center event endpoints.

- ``POST /internal/events`` — internal services (worker/scheduler/discovery/publisher) publish
  a generic PipelineEvent over HTTP (internal token).
- ``GET /ops/events`` — admin history/backfill from ``pipeline_events``.
- ``GET /ops/events/stream`` — admin SSE feed that forwards the Redis ``clipflow:events``
  channel. Extends the per-job SSE pattern in ``api/job_events.py`` into a global ops feed.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.enums import PipelineEventType
from app.models.pipeline_event import PipelineEvent
from app.models.user import User
from app.security.access_control import is_admin
from app.security.auth_middleware import get_current_user, require_internal_api_token
from app.services import event_bus

router = APIRouter()


class EventIngestInput(BaseModel):
    service: str
    type: PipelineEventType = PipelineEventType.INFO
    pipeline_job_id: str | None = None
    stage: str | None = None
    message: str | None = None
    payload: dict | None = None
    worker_id: str | None = None


@router.post("/internal/events")
def ingest_event(
    payload: EventIngestInput,
    _: None = Depends(require_internal_api_token),
    db: Session = Depends(get_db),
):
    job_id: uuid.UUID | None = None
    if payload.pipeline_job_id:
        try:
            job_id = uuid.UUID(payload.pipeline_job_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid pipeline_job_id") from exc

    event = event_bus.publish_event(
        db,
        service=payload.service,
        event_type=payload.type,
        pipeline_job_id=job_id,
        stage=payload.stage,
        message=payload.message,
        payload=payload.payload,
        worker_id=payload.worker_id,
        commit=True,
    )
    return {"status": "ok", "event_id": str(event.id)}


@router.get("/ops/events")
def list_ops_events(
    service: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="admin only")

    query = db.query(PipelineEvent)
    if service:
        query = query.filter(PipelineEvent.service == service)
    if since:
        query = query.filter(PipelineEvent.created_at >= since)

    events = (
        query.order_by(PipelineEvent.created_at.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    return [event_bus.serialize_event(event) for event in events]


@router.get("/ops/events/stream")
async def stream_ops_events(
    request: Request,
    user: User = Depends(get_current_user),
):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="admin only")

    async def event_generator():
        pubsub = event_bus.get_redis().pubsub()
        pubsub.subscribe(event_bus.EVENTS_CHANNEL)
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                message = await asyncio.to_thread(
                    pubsub.get_message,
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message and message.get("type") == "message":
                    yield f"event: pipeline_event\ndata: {message['data']}\n\n"
                else:
                    yield ": keepalive\n\n"
        finally:
            try:
                pubsub.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
