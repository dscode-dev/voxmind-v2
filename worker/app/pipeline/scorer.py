from typing import Dict, List


class Scorer:

    def __init__(
        self,
        max_candidates: int = 10,
        max_candidates_per_window: int = 2,
        min_start_gap: int = 12,
        overlap_iou_threshold: float = 0.55,
    ):
        self.max_candidates = max_candidates
        self.max_candidates_per_window = max_candidates_per_window
        self.min_start_gap = min_start_gap
        self.overlap_iou_threshold = overlap_iou_threshold

    def score(self, candidates: List[Dict]) -> List[Dict]:
        if not candidates:
            return []

        ranked = sorted(
            candidates,
            key=lambda item: (
                item["total_score"],
                item.get("boundary_score", 0.0),
                -item.get("duration", 0.0),
            ),
            reverse=True,
        )

        selected: List[Dict] = []
        selected_per_window: dict[int, int] = {}

        for candidate in ranked:
            if len(selected) >= self.max_candidates:
                break

            window_index = candidate.get("window_index", 0)
            if selected_per_window.get(window_index, 0) >= self.max_candidates_per_window:
                continue

            if self._should_skip(candidate, selected):
                continue

            selected.append(self._to_output(candidate))
            selected_per_window[window_index] = selected_per_window.get(window_index, 0) + 1

        return sorted(selected, key=lambda item: item["start"])

    def _should_skip(self, candidate: Dict, selected: List[Dict]) -> bool:
        for existing in selected:
            if abs(float(candidate["start"]) - float(existing["start"])) < self.min_start_gap:
                return True

            if self._interval_iou(candidate, existing) >= self.overlap_iou_threshold:
                return True

        return False

    def _interval_iou(self, left: Dict, right: Dict) -> float:
        left_start = float(left["start"])
        left_end = float(left["end"])
        right_start = float(right["start"])
        right_end = float(right["end"])

        intersection = max(0.0, min(left_end, right_end) - max(left_start, right_start))
        if intersection <= 0:
            return 0.0

        union = max(left_end, right_end) - min(left_start, right_start)
        if union <= 0:
            return 0.0

        return intersection / union

    def _to_output(self, candidate: Dict) -> Dict:
        return {
            "candidate_id": candidate.get("candidate_id"),
            "start": float(candidate["start"]),
            "end": float(candidate["end"]),
            "duration": float(candidate.get("duration", float(candidate["end"]) - float(candidate["start"]))),
            "text": candidate["text"],
            "total_score": float(candidate["total_score"]),
            "window_index": candidate.get("window_index"),
            "speaker_count": candidate.get("speaker_count", 0),
            "speakers": candidate.get("speakers", []),
            "score_breakdown": {
                "hook_score": candidate.get("hook_score", 0.0),
                "narrative_score": candidate.get("narrative_score", 0.0),
                "emotional_score": candidate.get("emotional_score", 0.0),
                "audio_score": candidate.get("audio_score", 0.0),
                "boundary_score": candidate.get("boundary_score", 0.0),
                "speaker_score": candidate.get("speaker_score", 0.0),
                "density_bonus": candidate.get("density_bonus", 0.0),
                "duration_penalty": candidate.get("duration_penalty", 0.0),
            },
        }
