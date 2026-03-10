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
    "problema",
    "medo",
    "perigo"
]


class CandidateBuilder:

    def build(self, chunks: List[Dict]) -> List[Dict]:

        candidates = []

        for chunk in chunks:

            text = chunk["text"].lower()

            score = chunk.get("hook_score", 0)

            audio_peak = chunk.get("audio_peak_score", 0)
            
            if audio_peak > 0.7:
                score += 3
            elif audio_peak > 0.4:
                score += 1
            
            # narrativa
            setup = chunk.get("story_setup", 0)
            conflict = chunk.get("story_conflict", 0)
            reveal = chunk.get("story_reveal", 0)
            
            score += setup + (conflict * 2) + (reveal * 3)
            
            # energia emocional do áudio
            if audio_peak > 0.7:
                score += 3
            elif audio_peak > 0.4:
                score += 1

            # emotional triggers
            for word in EMOTIONAL_WORDS:
                if word in text:
                    score += 2

            # question
            if "?" in text:
                score += 3

            # contrast
            if re.search(r"\bmas\b|\bporém\b|\bso que\b", text):
                score += 2

            # numbers
            if re.search(r"\b\d+\b", text):
                score += 1

            # strong start
            first_words = " ".join(text.split()[:6])

            if any(word in first_words for word in EMOTIONAL_WORDS):
                score += 3

            # text length bonus
            if len(text.split()) > 20:
                score += 1

            if score >= 4:

                candidates.append(
                    {
                        "start": chunk["start"],
                        "end": chunk["end"],
                        "text": chunk["text"],
                        "heuristic_score": score,
                    }
                )

        return candidates