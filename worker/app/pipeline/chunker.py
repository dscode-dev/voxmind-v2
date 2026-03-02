from typing import List, Dict


class Chunker:

    def __init__(self, min_duration=25, max_duration=50, overlap=5):
        self.min_duration = min_duration
        self.max_duration = max_duration
        self.overlap = overlap

    def chunk(self, segments: List[Dict]) -> List[Dict]:

        chunks = []
        i = 0

        while i < len(segments):

            start_time = segments[i]["start"]
            current = []
            j = i

            while j < len(segments):
                current.append(segments[j])
                duration = segments[j]["end"] - start_time

                if duration >= self.min_duration:
                    if duration >= self.max_duration:
                        break
                j += 1

            if current:
                chunk = self._build_chunk(current)

                if chunk["end"] - chunk["start"] >= self.min_duration:
                    chunks.append(chunk)

            i = max(i + 1, j - self._overlap_index(segments, j))

        return chunks

    def _overlap_index(self, segments, index):
        if index >= len(segments):
            return 0

        target_time = segments[index]["end"] - self.overlap

        for i in range(index - 1, -1, -1):
            if segments[i]["start"] <= target_time:
                return index - i

        return 0

    def _build_chunk(self, segments):
        return {
            "start": segments[0]["start"],
            "end": segments[-1]["end"],
            "text": " ".join([s["text"] for s in segments])
        }