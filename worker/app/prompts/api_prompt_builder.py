from typing import Dict, List

from app.prompts.prompt_context import (
    build_hook_candidate_context,
    build_span_catalog_context,
    build_transcript_context,
)


class ApiPromptBuilder:

    def __init__(self, max_context_chars: int | None = None):
        from app.settings import settings

        self.prompt_long_max_segments_per_candidate = settings.prompt_long_max_segments_per_candidate
        self.render_min_long_video_duration_sec = settings.render_min_long_video_duration_sec
        if max_context_chars is not None:
            self.max_context_chars = max_context_chars
            return

        self.max_context_chars = settings.llm_max_chars

    def build(
        self,
        transcript: List[Dict],
        candidates: List[Dict],
        span_catalog: List[Dict],
        hook_candidates: List[Dict],
        job_id: str,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
    ) -> str:
        transcript_context = build_transcript_context(
            transcript=transcript,
            candidates=[],
            max_chars=int(self.max_context_chars * 0.80),
            max_segments_per_candidate=(
                self.prompt_long_max_segments_per_candidate if clip_mode == "long" else 18
            ),
            context_padding_sec=48 if clip_mode == "long" else 32,
            min_total_segments=42 if clip_mode == "long" else 28,
        )
        span_catalog_context = build_span_catalog_context(
            spans=span_catalog,
            max_chars=int(self.max_context_chars * 0.08),
        )
        hook_candidate_context = build_hook_candidate_context(
            hook_candidates=hook_candidates,
            max_chars=int(self.max_context_chars * 0.08),
        )

        if clip_mode == "long":
            return self._build_long_prompt(
                transcript_context=transcript_context,
                span_catalog_context=span_catalog_context,
                hook_candidate_context=hook_candidate_context,
                job_id=job_id,
                video_ratio=video_ratio,
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
- Use the transcript as the primary context.
- You may adjust timestamps slightly to preserve complete meaning.
- Prefer editorially complete cuts over merely loud or sensational ones.
- Avoid redundant cuts that repeat the same narrative beat.
- Produce up to 3 final videos that can each stand alone.
- Each final video must have its own hook, title, description and closure.
- Do not split one incomplete thought across different final videos.
- Prefer fewer strong final videos over 3 weak or incomplete ones.
- Prefer 2 connected cuts inside the same final video when that gives a stronger hook, better context and more competitive duration.
- In `short_serie`, prefer 2 connected cuts per final video whenever there is a strong continuation in the material.
- Use a single cut only when one continuous block already delivers hook, development and closure within the target duration.
- `post.hook` must be fully contained inside the first selected cut.
- Prefer selecting the hook with `hook_id` from `HOOK CANDIDATES`.
- Prefer selecting the cuts with `span_ids` from `SPAN CATALOG`.
- Also provide `hook_start` and `hook_end` in seconds for the exact location of the hook.
- Do not describe a hook without giving its exact timing.
- `hook_start` must mark the real beginning of the hook sentence and `hook_end` the real end of that sentence, without a loose window.
- Prefer hooks with roughly 3 to 8 seconds of continuous speech.
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

SPAN CATALOG

{span_catalog_context}

HOOK CANDIDATES

{hook_candidate_context}

Return ONLY valid JSON in this format:

The structure below is only an example of shape.
Do not mechanically copy the number of items from the example.
You must decide how many final videos to return and how many cuts each `shorts_content` needs.
Use 1 or 2 cuts per final video according to the real narrative need.
Whenever possible, prefer returning `hook_id` and `span_ids`.
Use `shorts_content` directly only as a fallback when you need to detail cuts manually.
If `hook_id` and `span_ids` already define the selection clearly, `shorts_content` may be omitted or returned as an empty list.

{{
  "job_id": "{job_id}",
  "final_videos": [
    {{
      "video_index": 1,
      "hook_id": "hook_0001",
      "span_ids": ["span_0003", "span_0004"],
      "title": "main final video title",
      "hook": "main hook used in the opening of this final video",
      "hook_source_cut_index": 0,
      "hook_start": 10.5,
      "hook_end": 16.8,
      "description": "final posting description",
      "hashtags": ["#tag1", "#tag2", "#tag3"],
      "thumbnail": "thumbnail idea",
      "soundtrack_suggestion": "political_tension | mystery_tension | finance_tension | generic",
      "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
      "shorts_content": [
        {{
          "start": 10.5,
          "end": 45.3,
          "safe_start": 10.5,
          "safe_end": 45.3,
          "reason": "why this cut respects narrative, context and continuity",
          "narrative_role": "hook | setup | development | payoff",
          "merge_group": "story_1",
          "continuity_note": "how this cut connects to the rest of the final video",
          "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
          "transition_after": "hard_cut | punch_in | whoosh | fade | none"
        }}
      ]
    }}
  ]
}}

Use the transcript as the main context and choose the sequence that best closes the narrative.
Return `final_videos` with up to 3 separate final videos.
Each `final_videos[i]` must directly include `title`, `hook_id`, `span_ids`, `hook`, `hook_start`, `hook_end`, `description`, `hashtags`, `thumbnail`, `soundtrack_suggestion`, `speaker_focus` and `shorts_content`.
Prefer `hook_id` and `span_ids` as the main structured selection fields.
Use `shorts_content` as a complement or fallback when you need to detail cuts manually.
If `hook_id` and `span_ids` already define the selection clearly, `shorts_content` may be omitted or returned as an empty list.
Each `final_videos[i]` should preferably contain 2 connected cuts in `shorts_content` when strong continuation exists.
Use a single cut only when one block alone already delivers hook, development and closure within the target duration.
Do not mechanically replicate the number of items shown in the JSON example.
Choose the real number of cuts based on context and narrative strength.
Prefer final videos around {self._preferred_duration_band(clip_mode)} when the material supports it.
Only go below {self._response_min_total_duration(clip_mode)} seconds when there is truly no strong continuation available in the material.
You may go beyond 75 seconds only when that extension is necessary to conclude the subject clearly.
Prefer concluding the idea correctly even if that pushes the final video beyond 1 minute.
Validate the total duration of each `final_video` before responding: it must stay between {self._response_min_total_duration(clip_mode)} and 120 seconds.
If any `final_video` exceeds 120 seconds, shorten the last cut of that video before responding.
`final_videos[i].hook_source_cut_index` must point to the cut index inside `final_videos[i].shorts_content` that fully contains the main hook.
`final_videos[i].shorts_content[0]` must fully contain the main hook for that final video.
If there is only enough strong material for 1 or 2 good final videos, return only 1 or 2.
"""

    def _build_long_prompt(
        self,
        *,
        transcript_context: str,
        span_catalog_context: str,
        hook_candidate_context: str,
        job_id: str,
        video_ratio: str,
    ) -> str:
        return f"""
JOB CONFIGURATION

job_id: {job_id}
clip_mode: long
video_ratio: {video_ratio}

TASK

Select the best long-form narrative excerpts from the transcript.
Treat this as normal-video excerpt editing, not short-form editing.
Return ONLY valid JSON.
Do not place unescaped double quotes inside string values.

MANDATORY RULES

- Produce 1 final video by default.
- Only produce 2 final videos if there are two clearly distinct, complete and strong chapters.
- Never split one connected argument into multiple final videos just to increase the count.
- Prefer 1 very strong final video over 2 mediocre ones.
- Each final video must feel like a robust excerpt from a normal video.
- Preserve more setup, development and closure than a short-form output.
- Prefer 2 or 3 long connected cuts when that improves context and ending.
- Use a single cut only when one block alone already delivers enough context and a proper conclusion.
- Never start in the middle of a sentence.
- Never end before the subject is properly closed.
- Preserve chronology and continuity between cuts.
- Avoid abrupt topic jumps.
- Avoid over-compressing the material into a shorts-like structure.
- Avoid cuts that feel like shorts, reels or tiktok highlights.
- In `long`, each cut should feel like part of a larger continuous excerpt, not a compressed highlight.
- If there is useful continuation right after the first cut, keep that continuation inside the same final video.
- `post.hook` must be fully contained inside the first selected cut.
- Prefer selecting the hook with `hook_id` from `HOOK CANDIDATES`.
- Prefer selecting the cuts with `span_ids` from `SPAN CATALOG`.
- Also provide `hook_start` and `hook_end` in seconds for the exact location of the hook.
- `hook_start` must mark the first real word of the hook and `hook_end` the last real word of the hook.
- The final cut must close the narrative clearly.

TRANSCRIPT WITH SPEAKERS

{transcript_context}

SPAN CATALOG

{span_catalog_context}

HOOK CANDIDATES

{hook_candidate_context}

Return ONLY valid JSON in this format:

The structure below is only an example.
Do not mechanically copy the number of items from the example.
Whenever possible, prefer returning `hook_id` and `span_ids`.
Use `shorts_content` directly only as a fallback when you need to detail cuts manually.
If `hook_id` and `span_ids` already define the selection clearly, `shorts_content` may be omitted or returned as an empty list.

{{
  "job_id": "{job_id}",
  "final_videos": [
    {{
      "video_index": 1,
      "hook_id": "hook_0001",
      "span_ids": ["span_0005", "span_0006", "span_0007"],
      "title": "main final video title",
      "hook": "main hook used in the opening of this final video",
      "hook_source_cut_index": 0,
      "hook_start": 10.5,
      "hook_end": 18.0,
      "description": "final posting description",
      "hashtags": ["#tag1", "#tag2"],
      "thumbnail": "thumbnail idea",
      "soundtrack_suggestion": "political_tension | mystery_tension | finance_tension | generic",
      "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
      "shorts_content": [
        {{
          "start": 10.5,
          "end": 58.0,
          "safe_start": 10.5,
          "safe_end": 58.0,
          "reason": "why this excerpt opens the subject with enough context",
          "narrative_role": "hook | setup | development | payoff",
          "merge_group": "story_1",
          "continuity_note": "how this cut prepares the next one",
          "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
          "transition_after": "hard_cut | punch_in | whoosh | fade | none"
        }}
      ]
    }}
  ]
}}

Return `final_videos` with at most 2 separate final videos.
Each `final_videos[i]` must directly include `title`, `hook_id`, `span_ids`, `hook`, `hook_start`, `hook_end`, `description`, `hashtags`, `thumbnail`, `soundtrack_suggestion`, `speaker_focus` and `shorts_content`.
Prefer `hook_id` and `span_ids` as the main structured selection fields.
Use `shorts_content` as a complement or fallback when you need to detail cuts manually.
If `hook_id` and `span_ids` already define the selection clearly, `shorts_content` may be omitted or returned as an empty list.
Prefer final videos around 100 to 120 seconds when the material supports it.
Validate the total duration of each `final_video` before responding: it must stay between {self.render_min_long_video_duration_sec} and 120 seconds.
If any `final_video` exceeds 120 seconds, shorten the last cut of that video before responding.
By default, return only 1 `final_video`.
Return 2 `final_videos` only if the material clearly contains two distinct, complete and non-redundant chapters.
In `long`, prefer 2 or 3 larger connected cuts inside the same `final_video` instead of several short-like videos.
If there is only enough strong material for 1 good final video, return only 1.
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
- Keep more context than short-form outputs.
- Prefer sequences that feel like strong excerpts from a normal video, not shorts disguised as long clips.
"""

        return """
SHORT_SERIE
- Generate connected cuts that together form one cohesive short narrative.
- Related cuts should share the same merge_group.
"""

    def _response_min_total_duration(self, clip_mode: str) -> int:
        if clip_mode == "long":
            return int(self.render_min_long_video_duration_sec)
        return 60

    def _preferred_duration_band(self, clip_mode: str) -> str:
        if clip_mode == "long":
            return "100 to 120 seconds"
        return "60 to 90 seconds"
