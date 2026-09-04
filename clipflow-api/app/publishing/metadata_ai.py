"""Writing a title, a description and tags for one clip, from what the run actually knows.

The technical alternative is what shipped: `final_clip_01.mp4` as a title and an empty
description. That is fine as a fallback and useless as a publication.

**Grounded, or conservative.** The model is given the facts this system holds — the topic, the
source video's own title and channel, the clip's transcript when there is one — and told to
describe only those. A model asked to write about football will happily invent a scoreline, a
competition and a quote, and every one of those would be published under someone's channel as
if the system had checked it. When the context is thin the instruction is to stay general
rather than to fill the gap.

**Structured, then validated anyway.** The response is requested as JSON and parsed into a
Pydantic model, and the result still goes through the publishing contract's own limits before
anything is sent. Model output is input, not truth.

**It writes editorial text and nothing else.** No privacy, no target, no scheduling. Those are
distribution decisions the policy makes, and a field for them does not exist on the way out of
here.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.publishing.metadata import (
    MAX_DESCRIPTION_CHARS,
    MAX_TAG_CHARS,
    MAX_TITLE_CHARS,
)

logger = logging.getLogger(__name__)

PROVIDER = "openai"
ENDPOINT = "https://api.openai.com/v1/chat/completions"

# Bumped when the prompt or the contract changes, so metadata generated under different
# rules is distinguishable after the fact.
SCHEMA_VERSION = "publication-metadata-v1"

OK = "ok"
UNAVAILABLE = "unavailable"
FAILED = "failed"
INVALID = "invalid"

# Asked for slightly under the hard limits, so the publishing contract validates rather than
# truncates. Truncation is a repair; getting it right the first time is not.
TARGET_TITLE_CHARS = 90
TARGET_DESCRIPTION_CHARS = 900
MAX_TAGS = 12

SYSTEM_PROMPT = f"""You write YouTube publication metadata for short video clips.

You are given the facts a content pipeline holds about ONE clip. Write a title, a description
and tags for THAT clip.

GROUNDING — this is the rule that matters most:
- Describe only what the provided context supports.
- Never invent a name, a scoreline, a result, a quote, a statistic, a date, a competition, a
  team, or a place that is not in the context.
- Never turn an inference into a stated fact. If the context suggests something without
  saying it, do not assert it.
- If the context is thin, write something accurate and general. A vague honest title is
  correct; a specific invented one is not.

LANGUAGE:
- Write in the language of the content. If the transcript or source title is Portuguese,
  write Brazilian Portuguese. Do not translate to English.

TITLE:
- Editorially useful and human. Never a filename, an id, or a slug.
- At most {TARGET_TITLE_CHARS} characters.
- No angle brackets. No surrounding quotes. No clickbait promising what the clip does not show.

DESCRIPTION:
- A short paragraph that gives context, then up to three relevant hashtags on their own line.
- At most {TARGET_DESCRIPTION_CHARS} characters.
- No invented links. No betting or gambling claims. No promises, offers or recommendations
  that are not in the content. No filler to reach a length.

TAGS:
- Between 3 and {MAX_TAGS} tags, specific to this clip's subject.
- Each at most {MAX_TAG_CHARS} characters. Lowercase. No duplicates. No hash symbol.
- Prefer specific terms over generic ones. Do not pad the list.

Reply with JSON only: {{"title": str, "description": str, "tags": [str]}}"""


class GeneratedMetadata(BaseModel):
    """The model's answer, before the publishing contract has looked at it."""

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS * 2)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS * 2)
    tags: list[str] = Field(default_factory=list, max_length=40)


@dataclass
class MetadataResult:
    status: str
    metadata: GeneratedMetadata | None = None
    provider: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    # A code or an exception class name. Never a provider body: an authenticated API can
    # reflect the request, and the request carries the key.
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK and self.metadata is not None

    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "schema_version": SCHEMA_VERSION,
            "latency_ms": self.latency_ms,
        }


@dataclass
class ClipContext:
    """What the model is told about one clip. Facts this system holds, nothing more.

    Assembled by the caller from the topic, the candidate and the clip itself. Every field is
    optional because a run can genuinely lack any of them, and a missing field must produce a
    conservative title rather than a confident invention.
    """

    video_index: int
    topic_name: str | None = None
    topic_keywords: list[str] | None = None
    source_title: str | None = None
    source_channel: str | None = None
    clip_mode: str | None = None
    clip_title: str | None = None
    clip_hook: str | None = None
    clip_description: str | None = None
    transcript_excerpt: str | None = None
    duration_sec: float | None = None
    total_clips: int | None = None

    def as_prompt_fields(self) -> dict[str, Any]:
        fields = {
            "clip_number": self.video_index,
            "clips_in_this_run": self.total_clips,
            "topic": self.topic_name,
            "topic_keywords": (self.topic_keywords or [])[:12] or None,
            "source_video_title": self.source_title,
            "source_channel": self.source_channel,
            "clip_format": self.clip_mode,
            "clip_working_title": self.clip_title,
            "clip_hook": self.clip_hook,
            "clip_notes": self.clip_description,
            "clip_duration_seconds": round(self.duration_sec) if self.duration_sec else None,
            "clip_transcript": self.transcript_excerpt,
        }
        # Absent fields are dropped rather than sent as null: a wall of nulls reads to a model
        # as a list of things it might helpfully fill in.
        return {key: value for key, value in fields.items() if value not in (None, "", [])}

    @property
    def is_thin(self) -> bool:
        """Whether there is enough here to describe anything specific."""
        return not any(
            (self.transcript_excerpt, self.clip_title, self.clip_hook, self.source_title)
        )


class OpenAIMetadataGenerator:
    """OpenAI adapter, built to the same shape as the selection evaluator.

    Deliberately a second small adapter rather than a shared abstraction over both: they
    answer different questions with different schemas, and the only thing they would share is
    an HTTP call.
    """

    provider = PROVIDER

    def __init__(
        self,
        api_key: str | None,
        *,
        model: str = "gpt-4o-mini",
        timeout_sec: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip() or None
        self.model = model
        self.timeout_sec = timeout_sec
        self._client = client

    def is_available(self) -> bool:
        return self.api_key is not None

    def generate(self, context: ClipContext) -> MetadataResult:
        if not self.is_available():
            return MetadataResult(status=UNAVAILABLE, error="no_api_key")

        started = time.monotonic()
        try:
            response = self._post(context.as_prompt_fields(), thin=context.is_thin)
        except httpx.HTTPError as exc:
            # A timeout or a dropped connection. The caller falls back; the video is fine.
            return MetadataResult(
                status=FAILED, provider=self.provider, model=self.model,
                error=type(exc).__name__,
            )

        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code != 200:
            return MetadataResult(
                status=FAILED, provider=self.provider, model=self.model,
                latency_ms=latency_ms, error=f"http_{response.status_code}",
            )

        try:
            content = response.json()["choices"][0]["message"]["content"]
            metadata = GeneratedMetadata.model_validate(json.loads(content))
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            # No repair loop. A model that cannot answer a three-field schema will not be
            # argued into it, and the fallback is already correct.
            return MetadataResult(
                status=INVALID, provider=self.provider, model=self.model,
                latency_ms=latency_ms, error=type(exc).__name__,
            )

        return MetadataResult(
            status=OK, metadata=metadata, provider=self.provider,
            model=self.model, latency_ms=latency_ms,
        )

    def _post(self, payload: dict[str, Any], *, thin: bool) -> httpx.Response:
        user_content = json.dumps(payload, ensure_ascii=False)
        if thin:
            user_content += (
                "\n\nNOTE: this context is sparse. Write something accurate and general. "
                "Do not invent specifics to make it more interesting."
            )
        body = {
            "model": self.model,
            # Low but not zero: identical clips should not produce identical titles across a
            # run, and editorial text has no single correct answer to converge on.
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self._client is not None:
            return self._client.post(
                ENDPOINT, json=body, headers=headers, timeout=self.timeout_sec
            )
        with httpx.Client(timeout=self.timeout_sec) as client:
            return client.post(ENDPOINT, json=body, headers=headers)


class NullMetadataGenerator:
    """What an unconfigured deployment gets: an honest refusal, never invented text."""

    provider = "none"

    def is_available(self) -> bool:
        return False

    def generate(self, context: ClipContext) -> MetadataResult:
        return MetadataResult(status=UNAVAILABLE, error="not_configured")


def build_metadata_generator(
    api_key: str | None, *, model: str, timeout_sec: float
) -> OpenAIMetadataGenerator | NullMetadataGenerator:
    """Whatever is actually configured — or nothing, stated plainly."""
    if api_key and api_key.strip():
        return OpenAIMetadataGenerator(api_key, model=model, timeout_sec=timeout_sec)
    return NullMetadataGenerator()
