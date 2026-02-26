from __future__ import annotations

import json
from pathlib import Path

from voxmind.app.llm.router import LLMRouter


class HookAgent:
    def __init__(self, llm: LLMRouter, prompts_dir: Path):
        self.llm = llm
        self.prompt = (prompts_dir / "hook_v1.txt").read_text(encoding="utf-8")

    def run(self, *, cut: dict) -> dict:
        messages = [{"role": "system", "content": self.prompt}, {"role": "user", "content": json.dumps(cut, ensure_ascii=False)}]
        res = self.llm.call(task="hook", messages=messages, temperature=0.3, max_tokens=300)
        return json.loads(res.content)
