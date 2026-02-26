from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LLMTask = Literal["segmentation", "scoring", "copy", "hook"]


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class LLMResult:
    model: str
    content: str
    usage: LLMUsage | None = None
    cached: bool = False
