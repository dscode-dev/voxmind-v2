from app.prompts.manual_prompt_builder import ManualPromptBuilder
from app.integrations.ai_client import AIClient
from app.integrations.prompt_router import ApiPromptBuilder


class PromptRouter:

    def __init__(self):
        self.manual_builder = ManualPromptBuilder()
        self.api_builder = ApiPromptBuilder()
        self.ai_client = AIClient()

    def build(
        self,
        job_id,
        transcript,
        candidates,
        clip_mode,
        video_ratio,
        build_ia=False
    ):

        if build_ia:

            user_prompt = self.api_builder.build(
                job_id=job_id,
                transcript=transcript,
                candidates=candidates,
                clip_mode=clip_mode,
                video_ratio=video_ratio
            )

            return self.ai_client.generate(user_prompt)

        return self.manual_builder.build(
            job_id=job_id,
            transcript=transcript,
            candidates=candidates,
            clip_mode=clip_mode,
            video_ratio=video_ratio
        )