"""Local LLM provider for an optional external node (e.g. iPad M4 running an
Ollama-compatible API). It is OPTIONAL: if unavailable the router falls back to OpenAI, and
the platform continues normally. We assume nothing about the model or that the node is up.

  healthcheck:  GET  {base_url}/api/tags
  generation:   POST {base_url}/api/generate   ({model, prompt, format: "json", stream: false})
"""
from __future__ import annotations

import json
from typing import Any

import requests

from app.ai.providers.base_provider import AIProvider
from app.observability import get_logger
from app.settings import settings

logger = get_logger(__name__)


class LocalProvider(AIProvider):
    name = "local"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout_sec: int | None = None,
        healthcheck_timeout_sec: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.local_llm_base_url or "").rstrip("/")
        self.model = model or settings.local_llm_model
        self.timeout_sec = timeout_sec or settings.local_llm_timeout_sec
        self.healthcheck_timeout_sec = (
            healthcheck_timeout_sec or settings.local_llm_healthcheck_timeout_sec
        )

    def healthcheck(self) -> bool:
        if not self.base_url:
            return False
        try:
            response = requests.get(
                f"{self.base_url}/api/tags",
                timeout=self.healthcheck_timeout_sec,
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("LOCAL_LLM_BASE_URL not configured")

        prompt = f"{system_prompt}\n\n{user_prompt}"
        response = requests.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
            },
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        body = response.json()

        # Ollama-compatible APIs return the generated text under "response".
        content = body.get("response") if isinstance(body, dict) else None
        if not content:
            raise RuntimeError("Empty response from local LLM")

        return json.loads(content)
