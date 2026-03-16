from typing import List, Dict
import re


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
    ):
        self.min_duration = min_duration
        self.target_duration = target_duration
        self.max_duration = max_duration
        self.overlap = overlap

    def chunk(self, segments: List[Dict]) -> List[Dict]:

        chunks = []
        i = 0

        while i < len(segments):

            start_time = segments[i]["start"]
            text_parts = []
            j = i

            while j < len(segments):

                text_parts.append(segments[j]["text"])

                duration = segments[j]["end"] - start_time

                text_joined = " ".join(text_parts).lower()

                if duration >= self.target_duration:

                    if self._is_good_ending(text_joined):
                        break

                if duration >= self.max_duration:
                    break

                j += 1

            if j >= len(segments):
                j = len(segments) - 1

            chunk_segments = segments[i : j + 1]

            chunk = self._build_chunk(chunk_segments)

            if chunk["end"] - chunk["start"] >= self.min_duration:

                if not self._bad_start(chunk["text"]):
                    chunks.append(chunk)

            i = self._advance_index(segments, j)

        return chunks

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

    def _advance_index(self, segments: List[Dict], current_index: int) -> int:

        if current_index >= len(segments) - 1:
            return current_index + 1

        target_time = segments[current_index]["end"] - self.overlap

        for i in range(current_index - 1, -1, -1):

            if segments[i]["start"] <= target_time:
                return i

        return current_index

    def _build_chunk(self, segments: List[Dict]) -> Dict:

        return {
            "start": segments[0]["start"],
            "end": segments[-1]["end"],
            "text": " ".join([s["text"] for s in segments]),
        }