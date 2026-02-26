from __future__ import annotations

import logging

from voxmind.app.agents.copy_agent import CopyAgent
from voxmind.app.agents.hook_agent import HookAgent
from voxmind.app.agents.scoring_agent import ScoringAgent
from voxmind.app.agents.segmentation_agent import SegmentationAgent
from voxmind.app.pipeline.steps.candidate_builder import build_candidate_prompt
from voxmind.app.pipeline.steps.chunking import make_chunks

log = logging.getLogger(__name__)


class OrchestratorV2:
    def __init__(self, *, segmentation: SegmentationAgent, scoring: ScoringAgent, hook: HookAgent, copy: CopyAgent):
        self.segmentation = segmentation
        self.scoring = scoring
        self.hook = hook
        self.copy = copy

    def run(self, *, url: str) -> dict:
        log.info("v2.start", extra={"url": url})

        transcript = self._fake_transcript(url)
        chunks = make_chunks(transcript, max_chars=2200)
        seg_input = build_candidate_prompt(chunks)

        candidates = self.segmentation.run(transcript=seg_input)
        scored = self.scoring.run(candidates=candidates)

        top = (scored.get("cuts") or [])[:1]
        hook_rewrites = [self.hook.run(cut=cut) for cut in top]

        copy = self.copy.run(top_cuts={"cuts": scored.get("cuts", []), "hook_rewrites": hook_rewrites})

        payload = {"url": url, "candidates": candidates, "scored": scored, "hook_rewrites": hook_rewrites, "copy": copy}
        log.info("v2.done", extra={"cuts": len(scored.get("cuts") or [])})
        return payload

    def _fake_transcript(self, url: str) -> str:
        return (
            f"VIDEO: {url}\n"
            "00:00 Eu vou te mostrar um ponto que quase ninguém entende...\n"
            "00:10 O problema não é o que você faz, é o que você repete.\n"
            "00:20 E quando você entende isso, tudo muda.\n"
            "00:35 Agora presta atenção nessa parte...\n"
            "00:45 Se você aplicar isso por 7 dias, você vai ver resultado real.\n"
        )
