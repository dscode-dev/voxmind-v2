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
        max_candidate_duration_sec: int = 90,
    ):
        self.min_total_score = min_total_score
        self.max_candidates_per_window = max_candidates_per_window
        self.window_size_sec = window_size_sec
        self.max_candidate_duration_sec = max_candidate_duration_sec

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
        if duration > self.max_candidate_duration_sec:
            return None

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
        has_clean_start = not self._bad_start(text)
        if has_clean_start:
            boundary_score += 1.5
        has_strong_ending = self._has_strong_ending(text_lower)
        if has_strong_ending:
            boundary_score += 1.5

        speaker_score = 1.5 if int(chunk.get("speaker_count", 0)) <= 1 else 0.5
        narrative_completeness = self._narrative_completeness_score(
            text_lower=text_lower,
            has_clean_start=has_clean_start,
            has_strong_ending=has_strong_ending,
            word_count=word_count,
            speaker_count=int(chunk.get("speaker_count", 0)),
        )
        dialogue_penalty = self._dialogue_fragment_penalty(
            text=text,
            speaker_count=int(chunk.get("speaker_count", 0)),
        )
        title_signal_score = self._title_signal_score(text_lower)
        retention_score = self._retention_score(
            text_lower=text_lower,
            hook_score=hook_score,
            has_clean_start=has_clean_start,
        )

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
            + narrative_completeness
            + title_signal_score
            + retention_score
        )

        duration_penalty = self._duration_penalty(duration)
        density_bonus = min(word_count / max(duration, 1.0), 4.0) * 0.4
        total_score = raw_score + density_bonus - duration_penalty - dialogue_penalty

        if total_score < self.min_total_score:
            return None

        narrative_role = self._narrative_role(
            hook_score=hook_score,
            reveal=reveal,
            conflict=conflict,
            has_question=question_score > 0,
            has_strong_ending=has_strong_ending,
        )

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
            "narrative_completeness_score": narrative_completeness,
            "dialogue_penalty": dialogue_penalty,
            "title_signal_score": title_signal_score,
            "retention_score": retention_score,
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
            "narrative_role": narrative_role,
            "editorial_signals": {
                "clean_start": has_clean_start,
                "strong_ending": has_strong_ending,
                "retention_score": retention_score,
                "title_signal_score": title_signal_score,
                "narrative_completeness_score": narrative_completeness,
                "dialogue_penalty": dialogue_penalty,
            },
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

    def _narrative_completeness_score(
        self,
        text_lower: str,
        has_clean_start: bool,
        has_strong_ending: bool,
        word_count: int,
        speaker_count: int,
    ) -> float:
        score = 0.0
        if has_clean_start:
            score += 1.2
        if has_strong_ending:
            score += 1.8
        if word_count >= 24:
            score += 0.8
        if re.search(r"\bporque\b|\bpor isso\b|\bmas\b|\bent[aã]o\b", text_lower):
            score += 0.8
        if speaker_count > 1 and has_strong_ending:
            score += 0.5
        return score

    def _dialogue_fragment_penalty(self, text: str, speaker_count: int) -> float:
        if speaker_count <= 1:
            return 0.0

        lowered = text.lower().strip()
        if lowered.startswith(("e ", "mas ", "então ", "porque ")):
            return 1.5
        if not re.search(r"[.!?]\s*$", text.strip()):
            return 1.0
        return 0.0

    def _title_signal_score(self, text_lower: str) -> float:
        score = 0.0
        if re.search(r"\bcomo\b|\bpor que\b|\bo que\b|\bqual\b", text_lower):
            score += 0.8
        if re.search(r"\bningu[eé]m\b|\bnunca\b|\bsempre\b|\btodo mundo\b", text_lower):
            score += 0.8
        if re.search(r"\bsegredo\b|\berro\b|\bverdade\b|\balerta\b", text_lower):
            score += 0.8
        return score

    def _retention_score(self, text_lower: str, hook_score: float, has_clean_start: bool) -> float:
        score = 0.0
        if hook_score >= 3:
            score += 1.2
        if has_clean_start and re.search(r"\bhoje\b|\bagora\b|\bpresta aten[cç][aã]o\b|\bolha\b", text_lower):
            score += 0.8
        if re.search(QUESTION_PATTERN, text_lower):
            score += 0.5
        return score

    def _narrative_role(
        self,
        hook_score: float,
        reveal: float,
        conflict: float,
        has_question: bool,
        has_strong_ending: bool,
    ) -> str:
        if reveal >= 2 or has_strong_ending:
            return "payoff"
        if hook_score >= 3 or has_question:
            return "hook"
        if conflict >= 1:
            return "development"
        return "setup"
