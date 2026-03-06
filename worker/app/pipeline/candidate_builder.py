import re
from typing import List, Dict


EMOTIONAL_WORDS = [
    "nunca",
    "jamais",
    "erro",
    "segredo",
    "ninguém",
    "verdade",
    "problema",
    "chocante",
    "incrível",
    "absurdo",
    "ridículo",
    "muda tudo",
    "cuidado",
    "impossível",
    "revelação",
    "alerta",
    "urgente",
]


class CandidateBuilder:

    def build(self, chunks: List[Dict]) -> List[Dict]:

        candidates = []

        for chunk in chunks:
            score = self._score_chunk(chunk["text"], chunk.get("hook_score", 0))

            if score >= 3:
                candidates.append(
                    {
                        "start": chunk["start"],
                        "end": chunk["end"],
                        "text": chunk["text"],
                        "heuristic_score": score,
                    }
                )

        return candidates

    def _score_chunk(self, text: str, hook_score: int = 0) -> int:

        text_lower = text.lower()
        score = 0

        for word in EMOTIONAL_WORDS:
            if word in text_lower:
                score += 2

        if "?" in text:
            score += 2

        if re.search(r"\bmas\b|\bporém\b|\bso que\b", text_lower):
            score += 2

        if re.search(r"\b\d+\b", text):
            score += 1

        first_words = " ".join(text_lower.split()[:8])
        if any(word in first_words for word in EMOTIONAL_WORDS):
            score += 2

        length = len(text.split())
        if length > 0:
            density = score / length
            if density > 0.08:
                score += 2

        score += hook_score

        return score
