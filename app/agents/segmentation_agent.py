from __future__ import annotations

import json
from pathlib import Path

from voxmind.app.llm.router import LLMRouter


class SegmentationAgent:
    def __init__(self, llm: LLMRouter, prompts_dir: Path):
        self.llm = llm
        self.prompt = (prompts_dir / "segmentation_v1.txt").read_text(encoding="utf-8")

    def run(self, *, transcript: str) -> dict:
        messages = [{"role": "system", "content": self.prompt}, {"role": "user", "content": transcript}]
        res = self.llm.call(task="segmentation", messages=messages, temperature=0.2, max_tokens=800)
        return json.loads(res.content)
