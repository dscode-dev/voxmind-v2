"""Structural contract for the AI cuts response.

**Pydantic is the single authority.** ``prompts/schemas/cuts_schema.json`` is generated from
these models (see ``tests/test_ai_schema_contract.py``, which fails if the checked-in file
drifts), so there is one definition rather than three that silently disagree.

The split this module enforces:

STRUCTURAL validity — here
    Is it a JSON object? Does it carry a selection? Are timestamps finite numbers with
    ``start < end``? Are identifiers strings? Are enumerated values in range?

EDITORIAL validity — not here
    Is the hook strong? Does the argument conclude? Is the cut long enough for the preset?
    Those are judgements, and they stay in the normalization/QA layers where they already
    live. Pydantic rejecting a cut for being 11.5s would silently overrule the preset.
"""
from __future__ import annotations

import math
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


NARRATIVE_ROLES = ("hook", "setup", "development", "payoff")
TRANSITIONS = ("hard_cut", "punch_in", "whoosh", "fade", "none")


def _finite(value: float, field_name: str) -> float:
    if value is None or math.isnan(value) or math.isinf(value):
        raise ValueError(f"{field_name} must be a finite number")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")
    return float(value)


class CutModel(BaseModel):
    """One cut inside a final video."""

    model_config = ConfigDict(extra="allow")

    start: float
    end: float
    safe_start: Optional[float] = None
    safe_end: Optional[float] = None

    span_id: Optional[str] = None
    reason: Optional[str] = None
    narrative_role: Optional[str] = None
    merge_group: Optional[str] = None
    continuity_note: Optional[str] = None
    speaker_focus: Optional[str] = None
    transition_after: Optional[str] = None

    @field_validator("start", "end", "safe_start", "safe_end")
    @classmethod
    def _timestamps_are_finite(cls, value, info):
        if value is None:
            return value
        return _finite(float(value), info.field_name)

    @field_validator("narrative_role")
    @classmethod
    def _known_narrative_role(cls, value):
        if value is None:
            return value
        normalized = str(value).strip().lower()
        # The prompt shows the options pipe-separated; a model echoing the whole list is a
        # formatting slip, not a structural failure.
        if "|" in normalized:
            return None
        if normalized not in NARRATIVE_ROLES:
            raise ValueError(
                f"narrative_role must be one of {NARRATIVE_ROLES}, got {value!r}"
            )
        return normalized

    @field_validator("transition_after")
    @classmethod
    def _known_transition(cls, value):
        if value is None:
            return value
        normalized = str(value).strip().lower()
        if "|" in normalized:
            return None
        if normalized not in TRANSITIONS:
            raise ValueError(f"transition_after must be one of {TRANSITIONS}, got {value!r}")
        return normalized

    @model_validator(mode="after")
    def _ordered(self) -> "CutModel":
        if self.end <= self.start:
            raise ValueError(f"cut end ({self.end}) must be greater than start ({self.start})")
        if self.safe_start is not None and self.safe_end is not None:
            if self.safe_end <= self.safe_start:
                raise ValueError("safe_end must be greater than safe_start")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start


class FinalVideoModel(BaseModel):
    """One independently postable final video."""

    model_config = ConfigDict(extra="allow")

    video_index: Optional[int] = None
    hook_id: Optional[str] = None
    span_ids: List[str] = Field(default_factory=list)

    title: Optional[str] = None
    hook: Optional[str] = None
    hook_source_cut_index: Optional[int] = None
    hook_start: Optional[float] = None
    hook_end: Optional[float] = None
    description: Optional[str] = None
    hashtags: List[str] = Field(default_factory=list)
    thumbnail: Optional[str] = None
    soundtrack_suggestion: Optional[str] = None
    speaker_focus: Optional[str] = None

    shorts_content: List[CutModel] = Field(default_factory=list)

    @field_validator("hook_start", "hook_end")
    @classmethod
    def _hook_timestamps_are_finite(cls, value, info):
        if value is None:
            return value
        return _finite(float(value), info.field_name)

    @field_validator("span_ids", mode="before")
    @classmethod
    def _span_ids_are_strings(cls, value):
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("span_ids must be a list")
        return [str(item) for item in value if str(item).strip()]

    @field_validator("hashtags", mode="before")
    @classmethod
    def _hashtags_are_strings(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            raise ValueError("hashtags must be a list")
        return [str(item) for item in value if str(item).strip()]

    @model_validator(mode="after")
    def _carries_a_selection(self) -> "FinalVideoModel":
        if not self.shorts_content and not self.span_ids:
            raise ValueError(
                "a final video must carry a selection: either span_ids or shorts_content"
            )
        if self.hook_start is not None and self.hook_end is not None:
            if self.hook_end <= self.hook_start:
                raise ValueError("hook_end must be greater than hook_start")
        return self


class CutsResponseModel(BaseModel):
    """The root object the provider must return."""

    model_config = ConfigDict(extra="allow")

    job_id: Optional[str] = None
    final_videos: List[FinalVideoModel] = Field(default_factory=list)
    shorts_content: List[CutModel] = Field(default_factory=list)

    @model_validator(mode="after")
    def _carries_a_selection(self) -> "CutsResponseModel":
        if not self.final_videos and not self.shorts_content:
            raise ValueError(
                "response must contain a non-empty 'final_videos' or 'shorts_content'"
            )
        return self

    def all_cuts(self) -> List[CutModel]:
        cuts = list(self.shorts_content)
        for video in self.final_videos:
            cuts.extend(video.shorts_content)
        return cuts

    def referenced_span_ids(self) -> set[str]:
        referenced: set[str] = set()
        for video in self.final_videos:
            referenced.update(video.span_ids)
        for cut in self.all_cuts():
            if cut.span_id:
                referenced.add(cut.span_id)
        return referenced


class RawEditResponseModel(BaseModel):
    """Raw authorial edit uses a different shape; only the root is constrained here."""

    model_config = ConfigDict(extra="allow")

    job_id: Optional[str] = None


def json_schema() -> dict[str, Any]:
    """The generated JSON Schema. `cuts_schema.json` is a build artifact of this."""
    return CutsResponseModel.model_json_schema()
