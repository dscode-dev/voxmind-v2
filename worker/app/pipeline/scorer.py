from typing import List, Dict
import re


STRONG_PATTERNS = [
    r"\bnunca\b",
    r"\bningu[eé]m\b",
    r"\bverdade\b",
    r"\bsegredo\b",
    r"\bproblema\b",
    r"\berro\b",
    r"\babsurdo\b",
    r"\bchocante\b",
]

CONTRAST_PATTERNS = [
    r"\bmas\b",
    r"\bpor[eé]m\b",
    r"\bso que\b",
]

QUESTION_PATTERNS = [
    r"\?",
]


class Scorer:

    def __init__(self, max_candidates: int = 12, min_gap: int = 15):

        self.max_candidates = max_candidates
        self.min_gap = min_gap

    def _semantic_score(self, text: str) -> int:

        text = text.lower()

        score = 0

        for p in STRONG_PATTERNS:
            if re.search(p, text):
                score += 3

        for p in CONTRAST_PATTERNS:
            if re.search(p, text):
                score += 2

        for p in QUESTION_PATTERNS:
            if re.search(p, text):
                score += 2

        if len(text.split()) > 25:
            score += 1

        return score

    def score(self, candidates: List[Dict]) -> List[Dict]:

        if not candidates:
            return []

        for c in candidates:

            semantic = self._semantic_score(c["text"])

            c["semantic_score"] = semantic

            c["total_score"] = (
                c.get("heuristic_score", 0)
                + semantic
                + (c.get("audio_peak_score", 0) * 3)
            ) 

        ranked = sorted(
            candidates,
            key=lambda x: x["total_score"],
            reverse=True,
        )

        results = []

        for c in ranked:

            if len(results) >= self.max_candidates:
                break

            if any(abs(c["start"] - r["start"]) < self.min_gap for r in results):
                continue

            results.append(
                {
                    "start": c["start"],
                    "end": c["end"],
                    "text": c["text"],
                    "heuristic_score": c.get("heuristic_score", 0),
                    "semantic_score": c.get("semantic_score", 0),
                    "total_score": c.get("total_score", 0),
                }
            )

        return results
