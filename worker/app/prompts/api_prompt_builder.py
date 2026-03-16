import json
from typing import List, Dict


class ApiPromptBuilder:

    def build(
        self,
        transcript: List[Dict],
        candidates: List[Dict],
        job_id: str,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
    ) -> str:

        return f"""
JOB CONFIGURATION

job_id: {job_id}
clip_mode: {clip_mode}
video_ratio: {video_ratio}

TASK

You must identify the best narrative cuts from the transcript.

The cut strategy depends on clip_mode:

SHORT
Generate independent cuts.
Each cut must work as a standalone video.

LONG
Generate multiple cuts that together can compose a larger coherent video.

SHORT_SERIE
Generate multiple connected cuts that together can form a single short narrative.

RULES

- Never start a cut in the middle of a sentence
- Never end a cut before the idea concludes
- Preserve narrative integrity
- You may slightly adjust timestamps to preserve complete meaning
- Use candidates as hints, not strict limits

TRANSCRIPT

{json.dumps(transcript, ensure_ascii=False, indent=2)}

CANDIDATES

{json.dumps(candidates, ensure_ascii=False, indent=2)}

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
      "reason": "why this cut is good and respects the requested mode",
      "title": "short impactful title",
      "description": "short description",
      "hashtags": ["#tag1", "#tag2", "#tag3"],
      "thumbnail": "thumbnail idea",
      "merge_group": "story_1"
    }}
  ]
}}
"""