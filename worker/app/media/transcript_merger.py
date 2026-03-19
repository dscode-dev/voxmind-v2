from typing import Dict, List


class TranscriptSpeakerMerger:

    def __init__(self, min_overlap_sec: float = 0.15):
        self.min_overlap_sec = min_overlap_sec

    def merge(self, segments: List[Dict], speaker_turns: List[Dict]) -> List[Dict]:
        if not segments:
            return []

        if not speaker_turns:
            return [
                {
                    **segment,
                    "speaker": segment.get("speaker", "UNKNOWN"),
                }
                for segment in segments
            ]

        merged: List[Dict] = []

        for segment in segments:
            speaker = self._assign_speaker(segment, speaker_turns)
            merged.append(
                {
                    **segment,
                    "speaker": speaker,
                }
            )

        return merged

    def _assign_speaker(self, segment: Dict, speaker_turns: List[Dict]) -> str:
        start = float(segment["start"])
        end = float(segment["end"])
        midpoint = start + ((end - start) / 2.0)

        best_speaker = "UNKNOWN"
        best_overlap = 0.0
        midpoint_speaker = "UNKNOWN"

        for turn in speaker_turns:
            overlap = self._overlap(start, end, float(turn["start"]), float(turn["end"]))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = str(turn["speaker"])

            if float(turn["start"]) <= midpoint <= float(turn["end"]):
                midpoint_speaker = str(turn["speaker"])

        if best_overlap >= self.min_overlap_sec:
            return best_speaker

        return midpoint_speaker

    def _overlap(
        self,
        left_start: float,
        left_end: float,
        right_start: float,
        right_end: float,
    ) -> float:
        return max(0.0, min(left_end, right_end) - max(left_start, right_start))
