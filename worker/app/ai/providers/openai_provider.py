"""OpenAI provider (gpt-4o-mini by default). Uses JSON output mode and returns parsed JSON."""
from __future__ import annotations

import json
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from app.ai.providers.base_provider import AIProvider
from app.observability import get_logger
from app.settings import settings

logger = get_logger(__name__)


class OpenAIProvider(AIProvider):
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        timeout_sec: int | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = model or settings.openai_model
        self.temperature = settings.openai_temperature if temperature is None else temperature
        self.timeout_sec = timeout_sec or settings.openai_timeout_sec

    def healthcheck(self) -> bool:
        # A configured key is the cheap, network-free readiness signal; the request itself is
        # retried/guarded. The router only hard-depends on the *local* healthcheck.
        return bool(self.api_key)

    @retry(
        stop=stop_after_attempt(settings.integration_retry_attempts),
        wait=wait_exponential(
            multiplier=1,
            min=settings.integration_retry_min_sec,
            max=settings.integration_retry_max_sec,
        ),
        reraise=True,
    )
    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not configured")

        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, timeout=self.timeout_sec)
        response = client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("Empty response from OpenAI")

        return json.loads(content)
