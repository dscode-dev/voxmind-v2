from __future__ import annotations

import json
from pathlib import Path

from voxmind.app.llm.router import LLMRouter


class CopyAgent:
    def __init__(self, llm: LLMRouter, prompts_dir: Path):
        self.llm = llm
        self.prompt = (prompts_dir / "copy_v1.txt").read_text(encoding="utf-8")

    def run(self, *, top_cuts: dict) -> dict:
        messages = [{"role": "system", "content": self.prompt}, {"role": "user", "content": json.dumps(top_cuts, ensure_ascii=False)}]
        res = self.llm.call(task="copy", messages=messages, temperature=0.4, max_tokens=700)
        return json.loads(res.content)
