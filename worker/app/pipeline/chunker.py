from typing import List, Dict


class Chunker:

    def __init__(self, min_duration=20, max_duration=60):
        self.min_duration = min_duration
        self.max_duration = max_duration

    def chunk(self, segments: List[Dict]) -> List[Dict]:

        chunks = []
        current_chunk = []
        start_time = None

        for segment in segments:
            if not current_chunk:
                start_time = segment["start"]

            current_chunk.append(segment)

            duration = segment["end"] - start_time

            if duration >= self.min_duration:
                if duration <= self.max_duration:
                    chunks.append(self._build_chunk(current_chunk))
                    current_chunk = []
                else:
                    chunks.append(self._build_chunk(current_chunk))
                    current_chunk = []

        if current_chunk:
            chunks.append(self._build_chunk(current_chunk))

        return chunks

    def _build_chunk(self, segments):
        return {
            "start": segments[0]["start"],
            "end": segments[-1]["end"],
            "text": " ".join([s["text"] for s in segments])
        }