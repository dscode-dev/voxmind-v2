import json
import hashlib
from pathlib import Path
from typing import List, Dict

from openai import OpenAI
from worker.app.settings import settings


class Scorer:

    def __init__(self):
        self.llm_mode = settings.llm_mode
        self.cache_dir = Path("/tmp/llm_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        if self.llm_mode == "real":
            if not settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY not configured")
            self.client = OpenAI(api_key=settings.openai_api_key)

    # =========================
    # Public API
    # =========================
    def score(self, candidates: List[Dict]) -> List[Dict]:

        if not candidates:
            return []

        cache_key = self._hash_candidates(candidates)
        cached = self._read_cache(cache_key)

        if cached:
            return cached

        if self.llm_mode == "mock":
            result = self._mock_score(candidates)
        else:
            try:
                result = self._real_score(candidates)
            except Exception:
                # fallback automático se OpenAI falhar
                result = self._mock_score(candidates)

        self._write_cache(cache_key, result)
        return result

    # =========================
    # Real LLM scoring
    # =========================
    def _real_score(self, candidates: List[Dict]) -> List[Dict]:

        trimmed_candidates = self._limit_candidates(candidates)

        prompt = f"""
Você é um editor especialista em vídeos virais para YouTube Shorts e TikTok.

Seu objetivo é escolher cortes com ALTÍSSIMO potencial de retenção.

Critérios obrigatórios:

1. O trecho precisa funcionar sozinho (sem depender de contexto anterior)
2. Deve ter hook forte nos primeiros 3 segundos
3. Precisa gerar curiosidade, choque, revelação ou conflito
4. Evite trechos mornos ou explicativos demais
5. Final do trecho deve deixar impacto

Avalie cada trecho de 0 a 10 nos critérios:

- viral_score
- hook_strength
- curiosity_gap
- emotional_intensity
- standalone_quality

Retorne APENAS JSON válido no formato:

[
  {{
    "start": float,
    "end": float,
    "viral_score": int,
    "hook_strength": int,
    "curiosity_gap": int,
    "emotional_intensity": int,
    "standalone_quality": int,
    "reason": "breve justificativa objetiva"
  }}
]

Trechos:
{json.dumps(trimmed_candidates, ensure_ascii=False)}
"""

        response = self.client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=settings.openai_temperature,
            max_tokens=settings.openai_max_tokens
        )

        content = response.choices[0].message.content.strip()
        parsed = json.loads(content)

        ranked = sorted(
            parsed,
            key=lambda x: (
                x["viral_score"]
                + x["hook_strength"]
                + x["curiosity_gap"]
                + x["emotional_intensity"]
                + x["standalone_quality"]
            ),
            reverse=True,
        )

        return ranked[:3]

    # =========================
    # Mock scoring (zero custo)
    # =========================
    def _mock_score(self, candidates: List[Dict]) -> List[Dict]:

        ranked = sorted(
            candidates,
            key=lambda x: (
                x.get("heuristic_score", 0),
                len(x.get("text", "")),
            ),
            reverse=True,
        )

        results = []

        for item in ranked[:3]:
            base = item.get("heuristic_score", 5)

            results.append(
                {
                    "start": item["start"],
                    "end": item["end"],
                    "viral_score": min(10, base + 3),
                    "hook_strength": min(10, base + 2),
                    "curiosity_gap": min(10, base + 1),
                    "emotional_intensity": min(10, base + 2),
                    "standalone_quality": min(10, base + 2),
                    "reason": "Mock scoring based on heuristic score"
                }
            )

        return results

    # =========================
    # Cache helpers
    # =========================
    def _hash_candidates(self, candidates: List[Dict]) -> str:
        raw = json.dumps(candidates, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _read_cache(self, key: str):
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def _write_cache(self, key: str, data: List[Dict]):
        cache_file = self.cache_dir / f"{key}.json"
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # =========================
    # Payload limiter
    # =========================
    def _limit_candidates(self, candidates: List[Dict]) -> List[Dict]:

        limited = []
        total_chars = 0
        max_chars = settings.llm_max_chars

        for c in candidates:
            text_len = len(c.get("text", ""))

            if total_chars + text_len > max_chars:
                break

            limited.append(c)
            total_chars += text_len

        return limited