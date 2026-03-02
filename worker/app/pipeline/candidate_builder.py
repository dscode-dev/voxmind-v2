import re
from typing import List, Dict


EMOTIONAL_WORDS = [
    "nunca", "jamais", "erro", "segredo", "ninguém",
    "verdade", "problema", "chocante", "incrível",
    "absurdo", "ridículo", "muda tudo", "cuidado",
    "impossível", "revelação", "alerta", "urgente"
]


class CandidateBuilder:

    def build(self, chunks: List[Dict]) -> List[Dict]:

        candidates = []

        for chunk in chunks:
            score = self._score_chunk(chunk["text"])

            if score >= 3:
                candidates.append({
                    "start": chunk["start"],
                    "end": chunk["end"],
                    "text": chunk["text"],
                    "heuristic_score": score
                })

        return candidates

    def _score_chunk(self, text: str) -> int:

        text_lower = text.lower()
        score = 0

        # Emotional keywords
        for word in EMOTIONAL_WORDS:
            if word in text_lower:
                score += 2

        # Question
        if "?" in text:
            score += 2

        # Contrast triggers
        if re.search(r"\bmas\b|\bporém\b|\bso que\b", text_lower):
            score += 2

        # Numbers (ex: "3 erros", "5 coisas")
        if re.search(r"\b\d+\b", text):
            score += 1

        # Strong beginning (first 8 words)
        first_words = " ".join(text_lower.split()[:8])
        if any(word in first_words for word in EMOTIONAL_WORDS):
            score += 2

        # Density boost
        length = len(text.split())
        if length > 0:
            density = score / length
            if density > 0.08:
                score += 2

        return score