import os
from pathlib import Path

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.settings import settings

class AIClient:

    def __init__(self):

        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.system_prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.txt"

    @retry(
        stop=stop_after_attempt(settings.integration_retry_attempts),
        wait=wait_exponential(
            multiplier=1,
            min=settings.integration_retry_min_sec,
            max=settings.integration_retry_max_sec,
        ),
        reraise=True,
    )
    def generate(self, user_prompt: str) -> str:

        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not configured")

        if not self.system_prompt_path.exists():
            raise RuntimeError("system_prompt.txt not found")

        system_prompt = self.system_prompt_path.read_text(encoding="utf-8")

        client = OpenAI(
            api_key=self.api_key,
            timeout=settings.openai_timeout_sec,
        )

        response = client.chat.completions.create(
            model=self.model,
            temperature=0.3,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
        )

        content = response.choices[0].message.content

        if not content:
            raise RuntimeError("Empty response from AI API")

        return content
