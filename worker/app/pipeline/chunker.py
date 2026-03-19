from bisect import bisect_left
import re
from typing import Dict, List


SENTENCE_ENDINGS = [
    r"\.",
    r"\?",
    r"\!",
]

CONCLUSION_PATTERNS = [
    r"\bent[aã]o\b",
    r"\bpor isso\b",
    r"\bno final\b",
    r"\ba verdade\b",
    r"\bo resultado\b",
]

START_AVOID_PATTERNS = [
    r"^\be\b",
    r"^\bmas\b",
    r"^\bent[aã]o\b",
    r"^\bporque\b",
]


class Chunker:

    def __init__(
        self,
        min_duration: int = 30,
        target_duration: int = 55,
        max_duration: int = 95,
        overlap: int = 6,
        boundary_lookback: int = 2,
    ):
        self.min_duration = min_duration
        self.target_duration = target_duration
        self.max_duration = max_duration
        self.overlap = overlap
        self.boundary_lookback = boundary_lookback

    def chunk(self, segments: List[Dict]) -> List[Dict]:
        if not segments:
            return []

        starts = [float(segment["start"]) for segment in segments]
        chunks: List[Dict] = []
        start_index = 0
        iteration_count = 0
        max_iterations = max(len(segments) * 2, 1)

        while start_index < len(segments):
            iteration_count += 1
            if iteration_count > max_iterations:
                raise RuntimeError("Chunker exceeded safe iteration budget")

            end_index = self._find_chunk_end(segments, start_index)
            chunk_segments = segments[start_index : end_index + 1]
            chunk = self._build_chunk(chunk_segments, start_index, end_index)

            if self._is_valid_chunk(chunk):
                chunks.append(chunk)

            next_index = self._advance_index(
                segment_starts=starts,
                current_start_index=start_index,
                chunk_end_time=float(chunk["end"]),
            )
            start_index = max(next_index, start_index + 1)

        return chunks

    def _find_chunk_end(self, segments: List[Dict], start_index: int) -> int:
        start_time = float(segments[start_index]["start"])
        index = start_index
        best_index = start_index

        while index < len(segments):
            current_duration = float(segments[index]["end"]) - start_time
            best_index = index

            if current_duration >= self.target_duration and self._is_good_boundary(segments, index):
                return index

            if current_duration >= self.max_duration:
                return self._find_last_boundary(segments, start_index, index)

            index += 1

        return best_index

    def _find_last_boundary(
        self,
        segments: List[Dict],
        start_index: int,
        end_index: int,
    ) -> int:
        for index in range(end_index, start_index - 1, -1):
            if self._is_good_boundary(segments, index):
                return index

        return end_index

    def _is_good_boundary(self, segments: List[Dict], end_index: int) -> bool:
        start = max(0, end_index - self.boundary_lookback)
        tail_text = " ".join(
            (segments[index].get("text") or "").strip()
            for index in range(start, end_index + 1)
        ).lower()
        return self._is_good_ending(tail_text)

    def _is_good_ending(self, text: str) -> bool:
        for pattern in SENTENCE_ENDINGS:
            if re.search(pattern + r"\s*$", text):
                return True

        for pattern in CONCLUSION_PATTERNS:
            if re.search(pattern, text):
                return True

        return False

    def _bad_start(self, text: str) -> bool:
        first_words = " ".join(text.split()[:3]).lower()

        for pattern in START_AVOID_PATTERNS:
            if re.search(pattern, first_words):
                return True

        return False

    def _advance_index(
        self,
        segment_starts: List[float],
        current_start_index: int,
        chunk_end_time: float,
    ) -> int:
        target_time = max(segment_starts[current_start_index], chunk_end_time - self.overlap)

        return bisect_left(
            segment_starts,
            target_time,
            lo=current_start_index + 1,
        )

    def _is_valid_chunk(self, chunk: Dict) -> bool:
        duration = float(chunk["end"]) - float(chunk["start"])
        if duration < self.min_duration:
            return False

        if self._bad_start(chunk["text"]):
            return False

        return True

    def _build_chunk(
        self,
        segments: List[Dict],
        start_index: int,
        end_index: int,
    ) -> Dict:
        unique_speakers = sorted(
            {
                speaker
                for speaker in (
                    segment.get("speaker")
                    for segment in segments
                )
                if speaker
            }
        )

        return {
            "start": float(segments[0]["start"]),
            "end": float(segments[-1]["end"]),
            "text": " ".join(
                (segment.get("text") or "").strip()
                for segment in segments
            ).strip(),
            "segment_count": len(segments),
            "start_segment_index": start_index,
            "end_segment_index": end_index,
            "speaker_count": len(unique_speakers),
            "speakers": unique_speakers,
        }
