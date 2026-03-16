import json


class ApiPromptBuilder:

    def build(
        self,
        job_id: str,
        transcript: list,
        candidates: list,
        clip_mode: str,
        video_ratio: str
    ) -> str:

        return f"""
JOB CONFIGURATION

job_id: {job_id}
clip_mode: {clip_mode}
video_ratio: {video_ratio}

Your task:

1. Identify the best cuts from the transcript.
2. Respect the clip_mode strategy.
3. Use candidates as hints but not strict boundaries.
4. Preserve narrative integrity.

TRANSCRIPT

{json.dumps(transcript, ensure_ascii=False)}

CANDIDATES

{json.dumps(candidates, ensure_ascii=False)}

Return JSON in the following structure:

{{
  "job_id": "{job_id}",
  "clip_mode": "{clip_mode}",
  "cuts": [
    {{
      "start": float,
      "end": float,
      "hook": "string",
      "reason": "string",
      "merge_group": "string or null"
    }}
  ]
}}
"""