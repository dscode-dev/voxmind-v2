import re
from typing import List, Dict


EMOTIONAL_WORDS = [
    "nunca", "jamais", "erro", "segredo", "ninguém",
    "verdade", "problema", "chocante", "incrível",
    "absurdo", "ridículo", "muda tudo", "cuidado"
]


class CandidateBuilder:

    def build(self, chunks: List[Dict]) -> List[Dict]:

        candidates = []

        for chunk in chunks:
            score = self._score_chunk(chunk["text"])

            if score >= 2:
                candidates.append({
                    "start": chunk["start"],
                    "end": chunk["end"],
                    "text": chunk["text"],
                    "heuristic_score": score
                })

        return candidates

    def _score_chunk(self, text: str) -> int:
        score = 0

        for word in EMOTIONAL_WORDS:
            if word in text.lower():
                score += 1

        if "?" in text:
            score += 1

        if re.search(r"\bmas\b", text.lower()):
            score += 1

        return score