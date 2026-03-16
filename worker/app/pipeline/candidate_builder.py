from typing import List, Dict
import re


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


NUMERIC_PATTERN = r"\b\d+\b"
QUESTION_PATTERN = r"\?"
CONTRAST_PATTERN = r"\bmas\b|\bpor[eé]m\b|\bso que\b"


class CandidateBuilder:

    def build(self, chunks: List[Dict]) -> List[Dict]:

        candidates = []

        for chunk in chunks:

            text = chunk["text"].lower()

            hook_score = chunk.get("hook_score", 0)
            audio_peak = chunk.get("audio_peak_score", 0)

            setup = chunk.get("story_setup", 0)
            conflict = chunk.get("story_conflict", 0)
            reveal = chunk.get("story_reveal", 0)

            narrative_score = setup + (conflict * 2) + (reveal * 3)

            emotional_score = 0

            for word in EMOTIONAL_WORDS:
                if word in text:
                    emotional_score += 2

            question_score = 3 if re.search(QUESTION_PATTERN, text) else 0

            contrast_score = 2 if re.search(CONTRAST_PATTERN, text) else 0

            number_score = 1 if re.search(NUMERIC_PATTERN, text) else 0

            length_score = 1 if len(text.split()) > 20 else 0

            audio_score = 0

            if audio_peak > 0.7:
                audio_score = 4
            elif audio_peak > 0.4:
                audio_score = 2

            total = (
                hook_score
                + narrative_score
                + emotional_score
                + question_score
                + contrast_score
                + number_score
                + length_score
                + audio_score
            )

            if total < 5:
                continue

            candidates.append(
                {
                    "start": chunk["start"],
                    "end": chunk["end"],
                    "text": chunk["text"],
                    "hook_score": hook_score,
                    "narrative_score": narrative_score,
                    "emotional_score": emotional_score,
                    "audio_score": audio_score,
                    "total_score": total,
                }
            )

        return candidates