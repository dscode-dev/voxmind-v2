import re
from typing import List, Dict


HOOK_PATTERNS = [
    r"ninguém percebeu",
    r"o problema é",
    r"isso muda tudo",
    r"mas tem um detalhe",
    r"quase ninguém sabe",
    r"olha isso",
    r"preste atenção",
    r"isso explica",
    r"\bnunca\b",
    r"\bningu[eé]m\b",
    r"\bvoc[eê] sabia\b",
    r"\bo problema\b",
    r"\bo segredo\b",
    r"\bningu[eé]m fala\b",
]


QUESTION_PATTERN = r"\?"

CONTRAST_PATTERNS = [
    r"\bmas\b",
    r"\bpor[eé]m\b",
    r"\bso que\b",
]

class HookDetector:

    def analyze(self, chunks: List[Dict]) -> List[Dict]:

        enriched = []

        for chunk in chunks:

            text = chunk["text"].lower()

            hook_score = 0

            # hook phrases
            for p in HOOK_PATTERNS:
                if re.search(p, text):
                    hook_score += 3

            # questions
            if re.search(QUESTION_PATTERN, text):
                hook_score += 2

            # contrast
            for p in CONTRAST_PATTERNS:
                if re.search(p, text):
                    hook_score += 2

            # strong start
            first_words = " ".join(text.split()[:6])

            if any(re.search(p, first_words) for p in HOOK_PATTERNS):
                hook_score += 3

            enriched.append(
                {
                    **chunk,
                    "hook_score": hook_score
                }
            )

        return enriched