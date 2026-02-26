from __future__ import annotations

import json
from pathlib import Path

from voxmind.app.llm.router import LLMRouter


class ScoringAgent:
    def __init__(self, llm: LLMRouter, prompts_dir: Path):
        self.llm = llm
        self.prompt = (prompts_dir / "scoring_v1.txt").read_text(encoding="utf-8")

    def run(self, *, candidates: dict) -> dict:
        messages = [{"role": "system", "content": self.prompt}, {"role": "user", "content": json.dumps(candidates, ensure_ascii=False)}]
        res = self.llm.call(task="scoring", messages=messages, temperature=0.2, max_tokens=900)
        return json.loads(res.content)
