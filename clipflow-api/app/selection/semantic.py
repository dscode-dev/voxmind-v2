"""Semantic judgement of a candidate against a topic.

This is a model call, not an agent: input goes in, a structured result comes out, and nothing
loops, plans or converses. It is named for what it does.

It works from **metadata only** — title, description excerpt, channel, the query that
surfaced it. There is no transcript at this stage and there must not be: downloading and
transcribing every candidate to decide whether to select it would cost more than producing
the video, for candidates that will mostly be discarded.

Three properties matter more than the score itself:

* **It can be absent.** With no provider configured the result is ``unavailable`` and the
  engine continues on deterministic signals. There is no local stand-in generating plausible
  numbers — a fabricated relevance score is worse than none, because nothing downstream can
  tell it apart from a real one.
* **It cannot dominate.** Eligibility and policy are decided before and after this, so a
  confident model cannot select an unavailable video or blow through a channel cap.
* **Its output is validated.** A Pydantic contract with bounded ranges; anything outside it
  is rejected as a failure rather than clamped into looking reasonable.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

# Status of the semantic leg for one candidate.
OK = "ok"
UNAVAILABLE = "unavailable"
INVALID = "invalid_response"
FAILED = "failed"

# Descriptions are often thousands of characters of boilerplate. This is enough to judge the
# subject without paying to send sponsor links and channel bios to a model.
DESCRIPTION_BUDGET = 600


class SemanticVerdict(BaseModel):
    """The contract the model must satisfy.

    Ranges are validated, not clamped. A model that answers 4.7 for a 0-1 field has not
    understood the request, and silently turning that into 1.0 would hide the fact.
    """

    relevance: float = Field(ge=0.0, le=1.0)
    editorial_interest: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=400)

    def as_dict(self) -> dict[str, Any]:
        return {
            "relevance": round(self.relevance, 4),
            "editorial_interest": round(self.editorial_interest, 4),
            "confidence": round(self.confidence, 4),
            "reason": self.reason.strip()[:400],
        }


@dataclass(frozen=True)
class SemanticResult:
    """What the semantic leg produced, including why it produced nothing."""

    status: str
    verdict: SemanticVerdict | None = None
    provider: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK and self.verdict is not None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
        }
        if self.verdict is not None:
            payload.update(self.verdict.as_dict())
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class CandidateBrief:
    """Exactly what the model is shown. Nothing else leaves the process."""

    title: str | None
    description: str | None
    channel: str | None
    published_at: str | None
    discovery_query: str | None

    def as_prompt_fields(self) -> dict[str, Any]:
        description = (self.description or "").strip()
        if len(description) > DESCRIPTION_BUDGET:
            description = description[:DESCRIPTION_BUDGET] + "..."
        return {
            "title": (self.title or "").strip()[:300],
            "description": description,
            "channel": (self.channel or "").strip()[:120],
            "published_at": self.published_at,
            "discovery_query": self.discovery_query,
        }


class SemanticRelevanceEvaluator(Protocol):
    """The port. One judgement per call."""

    name: str

    def is_available(self) -> bool: ...

    def evaluate(self, *, topic_name: str, topic_description: str | None,
                 topic_keywords: list[str], brief: CandidateBrief) -> SemanticResult: ...


class NullSemanticEvaluator:
    """No provider configured.

    Returns ``unavailable``, never a number. This is the difference between "we did not
    judge this" and "we judged it a 0.5", and the engine has to be able to tell.
    """

    name = "none"

    def is_available(self) -> bool:
        return False

    def evaluate(self, **_: Any) -> SemanticResult:
        return SemanticResult(status=UNAVAILABLE, error="no semantic provider configured")


SYSTEM_PROMPT = (
    "You judge whether a video belongs to an editorial topic and whether it is worth "
    "covering. You see only metadata: no transcript, no video.\n\n"
    "relevance: does this video actually belong to the topic? A video that merely mentions "
    "the subject in passing is not relevant to it.\n\n"
    "editorial_interest: is there something here worth making a clip about — a dispute, a "
    "strong or unexpected statement, a conflict, a development people will react to? "
    "Routine coverage, recaps and highlight compilations are low interest even when they are "
    "perfectly on topic. Interest is NOT profanity, negativity or politics; it is whether "
    "the material gives an audience something to react to.\n\n"
    "confidence: how sure are you, given that you only saw metadata? Vague or truncated "
    "titles deserve low confidence.\n\n"
    "reason: one short sentence, in the topic's language, saying why.\n\n"
    "Answer with JSON only: "
    '{"relevance": 0.0-1.0, "editorial_interest": 0.0-1.0, "confidence": 0.0-1.0, '
    '"reason": "..."}'
)


class OpenAISemanticEvaluator:
    """OpenAI adapter.

    A small port and adapter in the API rather than importing the worker's provider layer:
    the two processes do not share a package, and moving half the worker across the boundary
    to make one call would be a far larger change than this PR should carry. The interface
    above is what a local model plugs into later.
    """

    name = "openai"

    def __init__(
        self,
        api_key: str | None,
        *,
        model: str = "gpt-4o-mini",
        timeout_sec: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip() or None
        self.model = model
        self.timeout_sec = timeout_sec
        self._client = client

    def is_available(self) -> bool:
        return self.api_key is not None

    def evaluate(
        self,
        *,
        topic_name: str,
        topic_description: str | None,
        topic_keywords: list[str],
        brief: CandidateBrief,
    ) -> SemanticResult:
        if not self.is_available():
            return SemanticResult(status=UNAVAILABLE, error="no api key configured")

        payload = {
            "topic": {
                "name": topic_name,
                "description": topic_description,
                "keywords": topic_keywords[:20],
            },
            "candidate": brief.as_prompt_fields(),
        }

        import time

        started = time.monotonic()
        try:
            response = self._post(payload)
        except httpx.HTTPError as exc:
            return SemanticResult(
                status=FAILED, provider=self.name, model=self.model,
                error=type(exc).__name__,
            )

        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code != 200:
            # The body is not echoed: providers reflect request parameters into errors, and
            # for an authenticated API that can include the key.
            return SemanticResult(
                status=FAILED, provider=self.name, model=self.model,
                latency_ms=latency_ms, error=f"http_{response.status_code}",
            )

        try:
            content = response.json()["choices"][0]["message"]["content"]
            verdict = SemanticVerdict.model_validate(json.loads(content))
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            # No repair loop. A model that cannot answer a four-field schema is not going to
            # be argued into it, and a retry costs another call to find that out.
            return SemanticResult(
                status=INVALID, provider=self.name, model=self.model,
                latency_ms=latency_ms, error=type(exc).__name__,
            )

        return SemanticResult(
            status=OK, verdict=verdict, provider=self.name,
            model=self.model, latency_ms=latency_ms,
        )

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        body = {
            "model": self.model,
            # Deterministic-ish: the same candidate should not swing between runs. It is not
            # a guarantee — sampling at temperature 0 is still not reproducible across model
            # versions — which is why reproducibility is only claimed for the deterministic
            # path.
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = "https://api.openai.com/v1/chat/completions"
        if self._client is not None:
            return self._client.post(url, json=body, headers=headers, timeout=self.timeout_sec)
        with httpx.Client(timeout=self.timeout_sec) as client:
            return client.post(url, json=body, headers=headers)


def build_evaluator(
    api_key: str | None,
    *,
    model: str,
    timeout_sec: float,
) -> SemanticRelevanceEvaluator:
    """Whatever is actually configured — or nothing, stated plainly."""
    if api_key and api_key.strip():
        return OpenAISemanticEvaluator(api_key, model=model, timeout_sec=timeout_sec)
    return NullSemanticEvaluator()
