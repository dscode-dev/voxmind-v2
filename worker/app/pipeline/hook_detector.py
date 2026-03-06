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
]


class HookDetector:

    def detect(self, chunks: List[Dict]) -> List[Dict]:

        processed = []

        for chunk in chunks:

            text = chunk["text"].lower()

            hook_score = 0

            for pattern in HOOK_PATTERNS:
                if re.search(pattern, text):
                    hook_score += 1

            chunk["hook_score"] = hook_score

            processed.append(chunk)

        return processed