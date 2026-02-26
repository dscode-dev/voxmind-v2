from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from voxmind.app.cache.base import Cache
from voxmind.app.llm.cost_tracker import approx_tokens, estimate_cost_usd
from voxmind.app.llm.models import LLMResult, LLMTask, LLMUsage
from voxmind.app.utils.hashing import sha256_text

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouterConfig:
    api_key: str | None
    base_url: str
    timeout_seconds: int
    model_segmentation: str
    model_scoring: str
    model_copy: str
    max_input_chars: int
    mock_llm: bool


class LLMRouter:
    def __init__(self, *, cfg: RouterConfig, cache: Cache, cache_ttl_seconds: int):
        self.cfg = cfg
        self.cache = cache
        self.cache_ttl_seconds = cache_ttl_seconds

    def _pick_model(self, task: LLMTask) -> str:
        if task == "scoring":
            return self.cfg.model_scoring
        if task == "segmentation":
            return self.cfg.model_segmentation
        if task == "copy":
            return self.cfg.model_copy
        if task == "hook":
            return self.cfg.model_scoring
        return self.cfg.model_copy

    def _cache_key(self, *, task: LLMTask, model: str, messages: list[dict]) -> str:
        payload = json.dumps({"task": task, "model": model, "messages": messages}, sort_keys=True)
        return "llm:" + sha256_text(payload)

    def _trim_messages(self, messages: list[dict]) -> list[dict]:
        total_chars = sum(len(m.get("content", "")) for m in messages)
        if total_chars <= self.cfg.max_input_chars:
            return messages
        trimmed = []
        for m in messages:
            if m.get("role") == "user":
                content = m.get("content", "")
                trimmed.append({**m, "content": content[: self.cfg.max_input_chars]})
            else:
                trimmed.append(m)
        return trimmed

    def call(self, *, task: LLMTask, messages: list[dict], temperature: float = 0.2, max_tokens: int = 900) -> LLMResult:
        model = self._pick_model(task)
        messages = self._trim_messages(messages)
        key = self._cache_key(task=task, model=model, messages=messages)

        cached = self.cache.get(key)
        if cached:
            return LLMResult(model=model, content=cached, usage=None, cached=True)

        if self.cfg.mock_llm or not self.cfg.api_key:
            content = self._mock(task)
            self.cache.set(key, content, self.cache_ttl_seconds)
            return LLMResult(model=model, content=content, usage=None, cached=False)

        result = self._call_openai(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
        self.cache.set(key, result.content, self.cache_ttl_seconds)
        return result

    def _mock(self, task: LLMTask) -> str:
        if task == "segmentation":
            return json.dumps({"candidates": [{"start": 12.0, "end": 34.0, "reason": "strong hook"}]})
        if task == "scoring":
            return json.dumps({"cuts": [{"start": 12.0, "end": 34.0, "score": 0.91, "hook": "Você não vai acreditar nisso..."}]})
        if task == "copy":
            return json.dumps({"title": "Corte insano em 30s", "description": "...", "hashtags": ["#shorts", "#viral"], "music": "trending"})
        if task == "hook":
            return json.dumps({"hook_rewrite": "Isso aqui vai explodir sua mente..."})
        return json.dumps({"ok": True})

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(3))
    def _call_openai(self, *, model: str, messages: list[dict], temperature: float, max_tokens: int) -> LLMResult:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}

        with httpx.Client(timeout=self.cfg.timeout_seconds) as client:
            r = client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()

        content = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage") or {}
        usage = None
        if usage_raw:
            usage = LLMUsage(
                input_tokens=int(usage_raw.get("prompt_tokens", 0)),
                output_tokens=int(usage_raw.get("completion_tokens", 0)),
            )

        in_t = usage.input_tokens if usage else approx_tokens(json.dumps(messages))
        out_t = usage.output_tokens if usage else approx_tokens(content)
        usd = estimate_cost_usd(model, in_t, out_t)
        log.info("llm.call", extra={"model": model, "in_toks": in_t, "out_toks": out_t, "usd_est": usd})

        return LLMResult(model=model, content=content, usage=usage, cached=False)
