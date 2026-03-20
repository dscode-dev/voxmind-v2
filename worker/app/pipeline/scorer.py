from typing import Dict, List


class Scorer:

    def __init__(
        self,
        max_candidates: int = 10,
        max_candidates_per_window: int = 2,
        min_start_gap: int = 12,
        overlap_iou_threshold: float = 0.55,
        prefer_thematic_continuity: bool = False,
        thematic_similarity_threshold: float = 0.14,
    ):
        self.max_candidates = max_candidates
        self.max_candidates_per_window = max_candidates_per_window
        self.min_start_gap = min_start_gap
        self.overlap_iou_threshold = overlap_iou_threshold
        self.prefer_thematic_continuity = prefer_thematic_continuity
        self.thematic_similarity_threshold = thematic_similarity_threshold

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
        anchor_candidate: Dict | None = None

        for candidate in ranked:
            if len(selected) >= self.max_candidates:
                break

            window_index = candidate.get("window_index", 0)
            if selected_per_window.get(window_index, 0) >= self.max_candidates_per_window:
                continue

            if self._should_skip(candidate, selected, anchor_candidate):
                continue

            output_candidate = self._to_output(candidate)
            selected.append(output_candidate)
            selected_per_window[window_index] = selected_per_window.get(window_index, 0) + 1
            if anchor_candidate is None:
                anchor_candidate = output_candidate

        return sorted(selected, key=lambda item: item["start"])

    def _should_skip(
        self,
        candidate: Dict,
        selected: List[Dict],
        anchor_candidate: Dict | None,
    ) -> bool:
        for existing in selected:
            if abs(float(candidate["start"]) - float(existing["start"])) < self.min_start_gap:
                return True

            if self._interval_iou(candidate, existing) >= self.overlap_iou_threshold:
                return True

            if self._text_similarity(candidate.get("text", ""), existing.get("text", "")) >= 0.72:
                return True

        if (
            self.prefer_thematic_continuity
            and anchor_candidate is not None
            and self._topic_similarity(candidate.get("text", ""), anchor_candidate.get("text", ""))
            < self.thematic_similarity_threshold
        ):
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
            "narrative_role": candidate.get("narrative_role"),
            "editorial_signals": candidate.get("editorial_signals", {}),
            "score_breakdown": {
                "hook_score": candidate.get("hook_score", 0.0),
                "narrative_score": candidate.get("narrative_score", 0.0),
                "emotional_score": candidate.get("emotional_score", 0.0),
                "audio_score": candidate.get("audio_score", 0.0),
                "boundary_score": candidate.get("boundary_score", 0.0),
                "speaker_score": candidate.get("speaker_score", 0.0),
                "narrative_completeness_score": candidate.get("narrative_completeness_score", 0.0),
                "dialogue_penalty": candidate.get("dialogue_penalty", 0.0),
                "title_signal_score": candidate.get("title_signal_score", 0.0),
                "retention_score": candidate.get("retention_score", 0.0),
                "density_bonus": candidate.get("density_bonus", 0.0),
                "duration_penalty": candidate.get("duration_penalty", 0.0),
            },
        }

    def _text_similarity(self, left_text: str, right_text: str) -> float:
        left_tokens = self._tokenize(left_text)
        right_tokens = self._tokenize(right_text)
        if not left_tokens or not right_tokens:
            return 0.0

        intersection = len(left_tokens & right_tokens)
        denominator = min(len(left_tokens), len(right_tokens))
        if denominator == 0:
            return 0.0
        return intersection / denominator

    def _tokenize(self, text: str) -> set[str]:
        return {
            token
            for token in "".join(char.lower() if char.isalnum() else " " for char in text).split()
            if len(token) > 2
        }

    def _topic_similarity(self, left_text: str, right_text: str) -> float:
        left_tokens = self._tokenize(left_text)
        right_tokens = self._tokenize(right_text)
        if not left_tokens or not right_tokens:
            return 0.0

        intersection = len(left_tokens & right_tokens)
        union = len(left_tokens | right_tokens)
        if union == 0:
            return 0.0

        return intersection / union
