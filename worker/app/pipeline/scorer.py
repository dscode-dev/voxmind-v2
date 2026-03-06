from typing import List, Dict


class Scorer:

    def __init__(self, max_candidates: int = 12, min_gap: int = 15):

        self.max_candidates = max_candidates
        self.min_gap = min_gap

    def score(self, candidates: List[Dict]) -> List[Dict]:

        if not candidates:
            return []

        ranked = sorted(
            candidates,
            key=lambda x: (
                x.get("heuristic_score", 0),
                len(x.get("text", "")),
            ),
            reverse=True,
        )

        results = []

        for c in ranked:

            if len(results) >= self.max_candidates:
                break

            # evitar cortes muito próximos
            if any(abs(c["start"] - r["start"]) < self.min_gap for r in results):
                continue

            results.append(
                {
                    "start": c["start"],
                    "end": c["end"],
                    "text": c["text"],
                    "heuristic_score": c.get("heuristic_score", 0),
                }
            )

        return results