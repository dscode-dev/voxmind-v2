import re
from typing import Dict, List


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
    "perigo",
]

NUMERIC_PATTERN = r"\b\d+\b"
QUESTION_PATTERN = r"\?"
CONTRAST_PATTERN = r"\bmas\b|\bpor[eé]m\b|\bso que\b"
STRONG_ENDING_PATTERN = r"[\.\?!]\s*$|\bpor isso\b|\bno final\b|\ba verdade\b"


class CandidateBuilder:

    def __init__(
        self,
        min_total_score: float = 6.0,
        max_candidates_per_window: int = 4,
        window_size_sec: int = 180,
    ):
        self.min_total_score = min_total_score
        self.max_candidates_per_window = max_candidates_per_window
        self.window_size_sec = window_size_sec

    def build(self, chunks: List[Dict]) -> List[Dict]:
        if not chunks:
            return []

        candidates = []

        for index, chunk in enumerate(chunks):
            candidate = self._build_candidate(index, chunk)
            if candidate is None:
                continue
            candidates.append(candidate)

        return self._apply_window_budget(candidates)

    def _build_candidate(self, index: int, chunk: Dict) -> Dict | None:
        text = (chunk.get("text") or "").strip()
        if not text:
            return None

        text_lower = text.lower()
        duration = max(float(chunk["end"]) - float(chunk["start"]), 0.01)
        word_count = len(text.split())

        hook_score = float(chunk.get("hook_score", 0))
        audio_peak = float(chunk.get("audio_peak_score", 0))

        setup = float(chunk.get("story_setup", 0))
        conflict = float(chunk.get("story_conflict", 0))
        reveal = float(chunk.get("story_reveal", 0))

        narrative_score = setup + (conflict * 2.0) + (reveal * 3.0)
        emotional_score = self._compute_emotional_score(text_lower)
        question_score = 2.5 if re.search(QUESTION_PATTERN, text_lower) else 0.0
        contrast_score = 1.5 if re.search(CONTRAST_PATTERN, text_lower) else 0.0
        number_score = 1.0 if re.search(NUMERIC_PATTERN, text_lower) else 0.0
        length_score = 1.0 if word_count >= 20 else 0.0
        audio_score = 4.0 if audio_peak > 0.7 else 2.0 if audio_peak > 0.4 else 0.0

        boundary_score = 0.0
        if not self._bad_start(text):
            boundary_score += 1.5
        if self._has_strong_ending(text_lower):
            boundary_score += 1.5

        speaker_score = 1.5 if int(chunk.get("speaker_count", 0)) <= 1 else 0.5

        raw_score = (
            hook_score
            + narrative_score
            + emotional_score
            + question_score
            + contrast_score
            + number_score
            + length_score
            + audio_score
            + boundary_score
            + speaker_score
        )

        duration_penalty = self._duration_penalty(duration)
        density_bonus = min(word_count / max(duration, 1.0), 4.0) * 0.4
        total_score = raw_score + density_bonus - duration_penalty

        if total_score < self.min_total_score:
            return None

        return {
            "candidate_id": f"cand_{index:04d}",
            "start": float(chunk["start"]),
            "end": float(chunk["end"]),
            "duration": duration,
            "text": text,
            "hook_score": hook_score,
            "narrative_score": narrative_score,
            "emotional_score": emotional_score,
            "audio_score": audio_score,
            "boundary_score": boundary_score,
            "speaker_score": speaker_score,
            "duration_penalty": duration_penalty,
            "density_bonus": density_bonus,
            "raw_score": raw_score,
            "total_score": round(total_score, 3),
            "window_index": int(float(chunk["start"]) // self.window_size_sec),
            "speaker_count": int(chunk.get("speaker_count", 0)),
            "speakers": chunk.get("speakers", []),
            "segment_count": int(chunk.get("segment_count", 0)),
            "start_segment_index": chunk.get("start_segment_index"),
            "end_segment_index": chunk.get("end_segment_index"),
        }

    def _apply_window_budget(self, candidates: List[Dict]) -> List[Dict]:
        windows: dict[int, List[Dict]] = {}
        for candidate in candidates:
            windows.setdefault(candidate["window_index"], []).append(candidate)

        selected: List[Dict] = []

        for window_index in sorted(windows):
            window_candidates = sorted(
                windows[window_index],
                key=lambda item: (item["total_score"], -item["duration"]),
                reverse=True,
            )
            selected.extend(window_candidates[: self.max_candidates_per_window])

        return sorted(selected, key=lambda item: (item["start"], item["end"]))

    def _compute_emotional_score(self, text: str) -> float:
        score = 0.0
        for word in EMOTIONAL_WORDS:
            if word in text:
                score += 1.5
        return score

    def _duration_penalty(self, duration: float) -> float:
        if duration < 28.0:
            return 2.0
        if duration <= 75.0:
            return 0.0
        return min((duration - 75.0) / 10.0, 4.0)

    def _bad_start(self, text: str) -> bool:
        first_words = " ".join(text.split()[:3]).lower()
        return bool(re.search(r"^\be\b|^\bmas\b|^\bent[aã]o\b|^\bporque\b", first_words))

    def _has_strong_ending(self, text: str) -> bool:
        return bool(re.search(STRONG_ENDING_PATTERN, text))
