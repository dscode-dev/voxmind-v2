"""Canonical AI event names.

These are emitted as generic ``PipelineEvent``s (service="ai") through the existing EventBus
— we do NOT introduce a new event model. The specific name lives in ``payload.ai_event`` while
the persisted ``event_type`` uses the coarse PipelineEventType (info/warning/error), so the
Ops Center can render AI provider status without any schema change to the foundation.
"""
from __future__ import annotations

# Provider lifecycle
AI_PROVIDER_SELECTED = "AI_PROVIDER_SELECTED"
AI_REQUEST_STARTED = "AI_REQUEST_STARTED"
AI_REQUEST_FINISHED = "AI_REQUEST_FINISHED"
AI_PROVIDER_FAILED = "AI_PROVIDER_FAILED"
AI_FALLBACK = "AI_FALLBACK"
LOCAL_PROVIDER_ONLINE = "LOCAL_PROVIDER_ONLINE"
LOCAL_PROVIDER_OFFLINE = "LOCAL_PROVIDER_OFFLINE"

# Coarse PipelineEventType value (see clipflow-api enums) each AI event maps to.
_LEVEL_BY_EVENT = {
    AI_PROVIDER_SELECTED: "info",
    AI_REQUEST_STARTED: "info",
    AI_REQUEST_FINISHED: "info",
    LOCAL_PROVIDER_ONLINE: "info",
    AI_FALLBACK: "warning",
    AI_PROVIDER_FAILED: "error",
    LOCAL_PROVIDER_OFFLINE: "error",
}


def level_for(event_name: str) -> str:
    return _LEVEL_BY_EVENT.get(event_name, "info")
