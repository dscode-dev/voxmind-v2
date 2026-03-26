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
            max_chars=int(self.max_context_chars * 0.42),
        )
        timeline_context = build_timeline_context(
            transcript=transcript,
            max_chars=int(self.max_context_chars * 0.18),
        )
        neighborhood_context = build_candidate_neighborhood_context(
            transcript=transcript,
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.22),
            max_candidates=6,
        )
        candidate_context = build_candidate_context(
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.18),
        )

        return f"""
JOB CONFIGURATION

job_id: {job_id}
clip_mode: {clip_mode}
video_ratio: {video_ratio}

TASK

Select the best narrative cuts from the speaker-aware transcript.
Treat this as a task to produce up to 3 separate final videos, each one independently postable.
Return ONLY valid JSON.
Do not place unescaped double quotes inside string values.
If you want to emphasize a term inside a string, prefer plain text instead of nested quotes.

MANDATORY RULES

- Never start in the middle of a sentence.
- Never end before the idea concludes.
- Respect speaker continuity when dialogue is important.
- Use candidates as strong and prioritized hints, not strict boundaries.
- Give special attention to candidates with `source = clipsai`, because they represent narrative blocks detected directly from the transcript.
- You may adjust timestamps slightly to preserve complete meaning.
- Prefer editorially complete cuts over merely loud or sensational ones.
- Avoid redundant cuts that repeat the same narrative beat.
- If the best cut is outside the prioritized candidates, follow the story and pick it anyway.
- Produce up to 3 final videos that can each stand alone.
- Each final video must have its own hook, title, description and closure.
- Do not split one incomplete thought across different final videos.
- Prefer fewer strong final videos over 3 weak or incomplete ones.
- Prefer 2 connected cuts inside the same final video when that gives a stronger hook, better context and more competitive duration.
- `post.hook` must be fully contained inside the first selected cut.
- The first cut must start before or exactly where the hook phrase begins.
- If a strong hook sits outside the first cut, either move the first cut to include it or choose a different hook.
- The hook must be a complete spoken line that feels strong and recognizable in the first seconds of the final video.
- Do not use a hook phrase that becomes weak or confusing when isolated.
- Every cut after the first must feel like a natural continuation of the previous one, not a new disconnected subject.
- If the choice is between 3 disconnected cuts or 2 coherent cuts, prefer 2 coherent cuts.
- Do not pick a later cut only because it is strong in isolation; it must strengthen the assembled video.
- If there is a meaningful time jump between consecutive cuts, explain why the jump still preserves narrative logic.
- Prefer ending slightly earlier with a clear closure over ending in the middle of the subject.

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
  "final_videos": [
    {{
      "video_index": 1,
      "post": {{
        "title": "main final video title",
        "hook": "main hook used in the opening of this final video",
        "hook_source_cut_index": 0,
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
          "continuity_note": "how this final video stands on its own",
          "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
          "transition_after": "hard_cut | punch_in | whoosh | fade | none"
        }}
      ]
    }}
  ]
}}

Use transcript, timeline, heuristic candidates and ClipsAI candidates as strong context, but keep editorial autonomy if a better sequence is clearly supported by the material.
Return `final_videos` with up to 3 separate final videos.
Each `final_videos[i]` should usually contain 1 or 2 connected cuts in `shorts_content`.
Prefer final videos around 55 to 75 seconds when the material supports it.
Only go below 55 seconds when the subject clearly closes earlier and there is no strong continuation.
`final_videos[i].post.hook_source_cut_index` must point to the cut index inside `final_videos[i].shorts_content` that fully contains the main hook.
`final_videos[i].shorts_content[0]` must fully contain the main hook for that final video.
If there is only enough strong material for 1 or 2 good final videos, return only 1 or 2.
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
