"""Composition layer for the automatic AI path.

Assembles the three reusable pieces — a static **system prompt**, a **clip-mode** addendum,
and a **response schema** — and reuses the existing :class:`ApiPromptBuilder` to render the
dynamic user prompt (transcript + span catalog + hook candidates). It intentionally does NOT
duplicate the large prompt logic; future CrewAI / LangGraph agents reuse these same parts.

The legacy :class:`ManualPromptBuilder` is left untouched and still powers manual mode.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.prompts.api_prompt_builder import ApiPromptBuilder

_PROMPTS_DIR = Path(__file__).resolve().parent
_CLIP_MODES_DIR = _PROMPTS_DIR / "clip_modes"
_SCHEMAS_DIR = _PROMPTS_DIR / "schemas"


@lru_cache(maxsize=8)
def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


class PromptBuilder:
    """Builds (system_prompt, user_prompt, schema) for an AIProvider call."""

    def __init__(self) -> None:
        self.api_builder = ApiPromptBuilder()

    def system_prompt(self, clip_mode: str = "short_serie") -> str:
        base = _read_text(_PROMPTS_DIR / "system_prompt.txt").strip()
        addendum = self.clip_mode_instructions(clip_mode).strip()
        if addendum:
            return f"{base}\n\nCLIP MODE\n\n{addendum}"
        return base

    def clip_mode_instructions(self, clip_mode: str) -> str:
        mode = (clip_mode or "short_serie").lower()
        if mode in {"long", "long_series"}:
            mode_file = "long"
        elif mode == "short":
            mode_file = "short"
        else:
            mode_file = "short_serie"
        return _read_text(_CLIP_MODES_DIR / f"{mode_file}.txt")

    def schema(self) -> dict[str, Any]:
        raw = _read_text(_SCHEMAS_DIR / "cuts_schema.json")
        return json.loads(raw) if raw else {}

    def build(
        self,
        *,
        transcript: list[dict],
        candidates: list[dict],
        span_catalog: list[dict],
        hook_candidates: list[dict],
        job_id: str,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
        job_preset: str | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        user_prompt = self.api_builder.build(
            transcript=transcript,
            candidates=candidates,
            span_catalog=span_catalog,
            hook_candidates=hook_candidates,
            job_id=job_id,
            clip_mode=clip_mode,
            video_ratio=video_ratio,
            job_preset=job_preset,
        )
        return self.system_prompt(clip_mode), user_prompt, self.schema()
