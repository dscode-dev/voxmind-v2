import os
import json
from openai import OpenAI


class Scorer:

    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not configured")

        self.client = OpenAI(api_key=api_key)

    def score(self, candidates):

        if not candidates:
            return []

        prompt = f"""
Você receberá trechos candidatos de vídeo.

Avalie cada trecho com notas de 0 a 10 nos critérios:

- viral_score
- hook_strength
- curiosity_gap
- emotional_intensity

Retorne APENAS JSON válido no formato:

[
  {{
    "start": float,
    "end": float,
    "viral_score": int,
    "hook_strength": int,
    "curiosity_gap": int,
    "emotional_intensity": int,
    "reason": "breve justificativa"
  }}
]

Candidatos:
{json.dumps(candidates, ensure_ascii=False)}
"""

        response = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )

        content = response.choices[0].message.content.strip()

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            raise RuntimeError("LLM returned invalid JSON")

        # Ordenar por viral_score + hook_strength
        ranked = sorted(
            parsed,
            key=lambda x: (
                x["viral_score"] +
                x["hook_strength"] +
                x["curiosity_gap"] +
                x["emotional_intensity"]
            ),
            reverse=True
        )

        return ranked[:3]