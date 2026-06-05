"""AIProvider contract. The worker only ever depends on this interface."""
from __future__ import annotations

from typing import Any


class AIProvider:
    #: short, stable identifier used in events/logs (e.g. "openai", "local").
    name: str = "base"

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the model in JSON mode and return parsed JSON (no markdown, no wrappers)."""
        raise NotImplementedError()

    def healthcheck(self) -> bool:
        """Return True if the provider is reachable/usable right now."""
        raise NotImplementedError()
