import re
from typing import List, Dict


SETUP_PATTERNS = [
    r"\bprimeiro\b",
    r"\bno começo\b",
    r"\bdeixa eu explicar\b",
    r"\bo contexto\b",
]

CONFLICT_PATTERNS = [
    r"\bproblema\b",
    r"\berro\b",
    r"\bmas\b",
    r"\bpor[eé]m\b",
    r"\bso que\b",
]

REVEAL_PATTERNS = [
    r"\bent[aã]o\b",
    r"\bdescobri\b",
    r"\bo segredo\b",
    r"\ba verdade\b",
    r"\bo que acontece\b",
]


class StoryShiftDetector:

    def analyze(self, chunks: List[Dict]) -> List[Dict]:

        enriched = []

        for chunk in chunks:

            text = chunk["text"].lower()

            setup_score = 0
            conflict_score = 0
            reveal_score = 0

            for p in SETUP_PATTERNS:
                if re.search(p, text):
                    setup_score += 1

            for p in CONFLICT_PATTERNS:
                if re.search(p, text):
                    conflict_score += 1

            for p in REVEAL_PATTERNS:
                if re.search(p, text):
                    reveal_score += 1

            enriched.append(
                {
                    **chunk,
                    "story_setup": setup_score,
                    "story_conflict": conflict_score,
                    "story_reveal": reveal_score
                }
            )

        return enriched