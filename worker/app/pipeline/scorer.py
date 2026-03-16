from typing import List, Dict


class Scorer:

    def __init__(
        self,
        max_candidates: int = 10,
        min_gap: int = 18,
    ):
        self.max_candidates = max_candidates
        self.min_gap = min_gap

    def score(self, candidates: List[Dict]) -> List[Dict]:

        if not candidates:
            return []

        ranked = sorted(
            candidates,
            key=lambda x: x["total_score"],
            reverse=True,
        )

        results = []

        for candidate in ranked:

            if len(results) >= self.max_candidates:
                break

            if self._too_close(candidate, results):
                continue

            results.append(
                {
                    "start": candidate["start"],
                    "end": candidate["end"],
                    "text": candidate["text"],
                    "total_score": candidate["total_score"],
                }
            )

        return self._expand_windows(results, candidates)

    def _too_close(self, candidate: Dict, results: List[Dict]) -> bool:

        for r in results:

            if abs(candidate["start"] - r["start"]) < self.min_gap:
                return True

        return False

    def _expand_windows(self, selected: List[Dict], all_candidates: List[Dict]) -> List[Dict]:

        expanded = []

        for cut in selected:

            start = cut["start"]
            end = cut["end"]

            best_start = start
            best_end = end
            best_score = cut["total_score"]

            for candidate in all_candidates:

                if candidate["start"] < start and candidate["end"] <= end:

                    score = candidate["total_score"]

                    if score > best_score * 0.7:

                        best_start = candidate["start"]

                if candidate["end"] > end and candidate["start"] >= start:

                    score = candidate["total_score"]

                    if score > best_score * 0.7:

                        best_end = candidate["end"]

            expanded.append(
                {
                    "start": best_start,
                    "end": best_end,
                    "text": cut["text"],
                    "total_score": best_score,
                }
            )

        return expanded