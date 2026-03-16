import json

from app.prompts.manual_prompt_builder import ManualPromptBuilder
from app.integrations.api_prompt_builder import ApiPromptBuilder
from app.integrations.ai_client import AIClient


class PromptWorker:

    def __init__(self):

        self.manual_builder = ManualPromptBuilder()
        self.api_builder = ApiPromptBuilder()
        self.ai_client = AIClient()

    def process_message(self, message: dict):

        job_id = message["job_id"]
        transcript = message["transcript"]
        candidates = message["candidates"]

        clip_mode = message.get("clip_mode", "short_serie")
        video_ratio = message.get("video_ratio", "portrait")

        build_ia = message.get("build_ia", False)

        if build_ia:

            user_prompt = self.api_builder.build(
                job_id=job_id,
                transcript=transcript,
                candidates=candidates,
                clip_mode=clip_mode,
                video_ratio=video_ratio,
            )

            ai_response = self.ai_client.generate(user_prompt)

            try:
                return json.loads(ai_response)
            except Exception:
                raise RuntimeError("AI returned invalid JSON")

        manual_prompt = self.manual_builder.build(
            job_id=job_id,
            transcript=transcript,
            candidates=candidates,
            clip_mode=clip_mode,
            video_ratio=video_ratio,
        )

        return json.loads(manual_prompt)