from typing import Dict, List

from app.prompts.prompt_context import (
    build_candidate_context,
    build_candidate_neighborhood_context,
    build_timeline_context,
    build_transcript_context,
)


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
            max_chars=int(self.max_context_chars * 0.5),
        )
        timeline_context = build_timeline_context(
            transcript=transcript,
            max_chars=int(self.max_context_chars * 0.2),
        )
        neighborhood_context = build_candidate_neighborhood_context(
            transcript=transcript,
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.16),
        )
        candidate_context = build_candidate_context(
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.14),
        )

        return f"""
JOB CONFIGURATION

job_id: {job_id}
clip_mode: {clip_mode}
video_ratio: {video_ratio}

TASK

Select the best narrative cuts from the speaker-aware transcript.
Treat this as one final assembled video, not a set of isolated clips.

MANDATORY RULES

- Never start in the middle of a sentence.
- Never end before the idea concludes.
- Respect speaker continuity when dialogue is important.
- Use candidates as hints, not strict boundaries.
- You may adjust timestamps slightly to preserve complete meaning.
- Prefer editorially complete cuts over merely loud or sensational ones.
- Avoid redundant cuts that repeat the same narrative beat.
- If the best cut is outside the prioritized candidates, follow the story and pick it anyway.
- The selected cuts will be assembled into one final video, so preserve context between consecutive cuts.
- Prefer a chronologically cohesive sequence inside the same narrative arc.
- Avoid large time jumps between cuts unless they are essential for the final payoff.
- The final cut must end with a clear verbal closure or payoff.

MODE RULES

{self._build_mode_instructions(clip_mode)}

TRANSCRIPT WITH SPEAKERS

{transcript_context}

FULL VIDEO TIMELINE

{timeline_context}

CANDIDATE NEIGHBORHOODS

{neighborhood_context}

PRIORITIZED CANDIDATES

{candidate_context}

Return ONLY valid JSON in this format:

{{
  "job_id": "{job_id}",
  "clip_mode": "{clip_mode}",
  "video_ratio": "{video_ratio}",
  "story_map": {{
    "core_topic": "main subject of the full video",
    "central_conflict": "main tension or question driving the story",
    "hook_strategy": "why the chosen hook is the best opening",
    "sequence_logic": [
      "how cut 1 sets the stage",
      "how cut 2 develops the idea",
      "how the final cut closes the subject"
    ],
    "final_payoff": "the closing line or conclusion the final video should end on"
  }},
  "post": {{
    "title": "main final video title",
    "hook": "main hook used in the cold open",
    "description": "final posting description",
    "hashtags": ["#tag1", "#tag2", "#tag3"],
    "thumbnail": "thumbnail idea",
    "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null"
  }},
  "shorts_content": [
    {{
      "start": 10.5,
      "end": 45.3,
      "safe_start": 10.5,
      "safe_end": 45.3,
      "reason": "why this cut is good and respects speaker continuity and requested mode",
      "narrative_role": "hook | setup | development | payoff",
      "merge_group": "story_1",
      "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
      "transition_after": "hard_cut | punch_in | whoosh | fade | none"
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
