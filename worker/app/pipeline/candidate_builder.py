import re
from typing import List, Dict


EMOTIONAL_WORDS = [
    "nunca",
    "ninguém",
    "erro",
    "segredo",
    "verdade",
    "absurdo",
    "ridículo",
    "chocante",
    "incrível",
    "cuidado",
    "alerta",
]


class CandidateBuilder:

    def build(self, chunks: List[Dict]) -> List[Dict]:

        candidates = []

        for chunk in chunks:

            text = chunk["text"].lower()
            score = 0

            # emotional triggers
            for word in EMOTIONAL_WORDS:
                if word in text:
                    score += 2

            # question
            if "?" in text:
                score += 2

            # contrast
            if re.search(r"\bmas\b|\bporém\b|\bso que\b", text):
                score += 2

            # numbers
            if re.search(r"\b\d+\b", text):
                score += 1

            # strong start
            first_words = " ".join(text.split()[:6])
            if any(word in first_words for word in EMOTIONAL_WORDS):
                score += 2

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