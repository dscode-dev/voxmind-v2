"""Base agent contract (placeholder).

TODO(Phase 5+): define the shared agent interface. Agents will compose prompts via
``app.prompts.builder.PromptBuilder`` and execute through ``app.ai.provider_router.ProviderRouter``.
CrewAI / LangGraph integration happens in a future phase — do not implement logic yet.
"""
from __future__ import annotations

from typing import Any


class BaseAgent:
    name: str = "base"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        # TODO(Phase 5+): implement agent execution loop.
        raise NotImplementedError("Agents are not implemented yet (scaffolding only).")
