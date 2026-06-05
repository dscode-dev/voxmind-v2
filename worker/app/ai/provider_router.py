"""Provider router — the only AI entrypoint the worker pipeline knows about.

Decision logic:

    IF local enabled and reachable:  use local  (fall back to OpenAI on error)
    ELSE:                            use OpenAI

Every decision emits a generic ``PipelineEvent`` (service="ai") through the injected emitter,
so the Ops Center reflects provider activity in real time. The emitter is best-effort and must
never raise.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from app.ai import events as ai_events
from app.ai.providers.base_provider import AIProvider
from app.ai.providers.local_provider import LocalProvider
from app.ai.providers.openai_provider import OpenAIProvider
from app.observability import get_logger
from app.settings import settings

logger = get_logger(__name__)

# emitter(event_name, *, level=None, provider=None, model=None, latency_ms=None, error=None)
EventEmitter = Callable[..., None]


def _noop_emitter(event_name: str, **fields: Any) -> None:  # pragma: no cover - default
    return None


class ProviderRouter:
    def __init__(
        self,
        openai_provider: AIProvider | None = None,
        local_provider: AIProvider | None = None,
        local_enabled: bool | None = None,
        emitter: EventEmitter | None = None,
    ) -> None:
        self.openai = openai_provider or OpenAIProvider()
        self.local = local_provider or LocalProvider()
        self.local_enabled = (
            settings.local_llm_enabled if local_enabled is None else local_enabled
        )
        self._emit = emitter or _noop_emitter
        self.last_provider: str | None = None

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.local_enabled:
            if self._local_online():
                try:
                    return self._run(self.local, system_prompt, user_prompt, schema)
                except Exception as exc:
                    logger.exception("Local provider failed; falling back to OpenAI")
                    self._emit(
                        ai_events.AI_PROVIDER_FAILED,
                        provider=self.local.name,
                        model=getattr(self.local, "model", None),
                        error=str(exc),
                    )
                    self._emit(ai_events.AI_FALLBACK, provider=self.openai.name)
            else:
                self._emit(
                    ai_events.LOCAL_PROVIDER_OFFLINE,
                    provider=self.local.name,
                    model=getattr(self.local, "model", None),
                )
                self._emit(ai_events.AI_FALLBACK, provider=self.openai.name)

        return self._run(self.openai, system_prompt, user_prompt, schema)

    def _local_online(self) -> bool:
        online = False
        try:
            online = self.local.healthcheck()
        except Exception:
            online = False

        if online:
            self._emit(
                ai_events.LOCAL_PROVIDER_ONLINE,
                provider=self.local.name,
                model=getattr(self.local, "model", None),
            )
        return online

    def _run(
        self,
        provider: AIProvider,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        model = getattr(provider, "model", None)
        self.last_provider = provider.name
        self._emit(ai_events.AI_PROVIDER_SELECTED, provider=provider.name, model=model)
        self._emit(ai_events.AI_REQUEST_STARTED, provider=provider.name, model=model)

        started = time.perf_counter()
        try:
            result = provider.generate_json(system_prompt, user_prompt, schema)
        except Exception as exc:
            self._emit(
                ai_events.AI_PROVIDER_FAILED,
                provider=provider.name,
                model=model,
                error=str(exc),
            )
            raise

        latency_ms = int((time.perf_counter() - started) * 1000)
        self._emit(
            ai_events.AI_REQUEST_FINISHED,
            provider=provider.name,
            model=model,
            latency_ms=latency_ms,
        )
        return result
