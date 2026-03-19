from typing import Dict, List

from app.prompts.prompt_context import build_candidate_context, build_transcript_context


class ApiPromptBuilder:

    def __init__(self, max_context_chars: int | None = None):
        if max_context_chars is not None:
            self.max_context_chars = max_context_chars
            return

        from app.settings import settings

        self.max_context_chars = settings.llm_max_chars

    def build(
        self,
        transcript: List[Dict],
        candidates: List[Dict],
        job_id: str,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
    ) -> str:
        transcript_context = build_transcript_context(
            transcript=transcript,
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.72),
        )
        candidate_context = build_candidate_context(
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.28),
        )

        return f"""
JOB CONFIGURATION

job_id: {job_id}
clip_mode: {clip_mode}
video_ratio: {video_ratio}

TASK

Select the best narrative cuts from the speaker-aware transcript.

MANDATORY RULES

- Never start in the middle of a sentence.
- Never end before the idea concludes.
- Respect speaker continuity when dialogue is important.
- Use candidates as hints, not strict boundaries.
- You may adjust timestamps slightly to preserve complete meaning.
- Prefer editorially complete cuts over merely loud or sensational ones.
- Avoid redundant cuts that repeat the same narrative beat.

MODE RULES

{self._build_mode_instructions(clip_mode)}

TRANSCRIPT WITH SPEAKERS

{transcript_context}

PRIORITIZED CANDIDATES

{candidate_context}

Return ONLY valid JSON in this format:

{{
  "job_id": "{job_id}",
  "clip_mode": "{clip_mode}",
  "video_ratio": "{video_ratio}",
  "shorts_content": [
    {{
      "start": 10.5,
      "end": 45.3,
      "hook": "strong hook at the beginning",
      "reason": "why this cut is good and respects speaker continuity and requested mode",
      "narrative_role": "hook | setup | development | payoff",
      "title": "short impactful title",
      "description": "short description",
      "hashtags": ["#tag1", "#tag2", "#tag3"],
      "thumbnail": "thumbnail idea",
      "merge_group": "story_1"
    }}
  ]
}}
"""

    def _build_mode_instructions(self, clip_mode: str) -> str:
        if clip_mode == "short":
            return """
SHORT
- Generate independent cuts.
- Each cut must work as a standalone video.
- Prefer a complete beginning, development and ending.
"""

        if clip_mode == "long":
            return """
LONG
- Generate connected blocks for a larger coherent narrative.
- Preserve continuity between cuts.
"""

        return """
SHORT_SERIE
- Generate connected cuts that together form one cohesive short narrative.
- Related cuts should share the same merge_group.
"""
