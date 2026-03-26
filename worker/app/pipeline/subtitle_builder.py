from pathlib import Path
import textwrap
from typing import Dict, List


class SubtitleBuilder:

    def build_clip_srt(
        self,
        *,
        cut: Dict,
        transcript_segments: List[Dict],
        output_path: Path,
    ) -> Path | None:
        entries = self._entries_for_cut(
            cut=cut,
            transcript_segments=transcript_segments,
        )
        if not entries:
            return None

        lines: List[str] = []
        for index, entry in enumerate(entries, start=1):
            lines.append(str(index))
            lines.append(
                f"{self._format_timestamp(entry['start'])} --> {self._format_timestamp(entry['end'])}"
            )
            lines.append(entry["text"])
            lines.append("")

        output_path.write_text("\n".join(lines), encoding="utf-8")
        return output_path

    def build_final_reel_srt(
        self,
        *,
        cuts: List[Dict],
        transcript_segments: List[Dict],
        output_path: Path,
    ) -> Path | None:
        entries = self._entries_for_final_reel(
            cuts=cuts,
            transcript_segments=transcript_segments,
        )
        if not entries:
            return None

        lines: List[str] = []
        for index, entry in enumerate(entries, start=1):
            lines.append(str(index))
            lines.append(
                f"{self._format_timestamp(entry['start'])} --> {self._format_timestamp(entry['end'])}"
            )
            lines.append(entry["text"])
            lines.append("")

        output_path.write_text("\n".join(lines), encoding="utf-8")
        return output_path

    def _entries_for_final_reel(
        self,
        *,
        cuts: List[Dict],
        transcript_segments: List[Dict],
    ) -> List[Dict]:
        entries: List[Dict] = []
        accumulated_offset = 0.0

        for cut in cuts:
            start = float(cut.get("safe_start", cut.get("start", 0.0)))
            end = float(cut.get("safe_end", cut.get("end", 0.0)))
            if end <= start:
                continue

            segments = self._segments_for_cut(
                transcript_segments=transcript_segments,
                cut_start=start,
                cut_end=end,
            )
            for segment in segments:
                local_start = max(0.0, float(segment.get("start", 0.0)) - start)
                local_end = min(end, float(segment.get("end", 0.0))) - start
                if local_end <= local_start:
                    continue

                text = self._subtitle_text(segment)
                if not text:
                    continue

                entries.append(
                    {
                        "start": round(accumulated_offset + local_start, 3),
                        "end": round(accumulated_offset + local_end, 3),
                        "text": text,
                    }
                )

            accumulated_offset += end - start

        return self._merge_adjacent_entries(entries)

    def _entries_for_cut(
        self,
        *,
        cut: Dict,
        transcript_segments: List[Dict],
    ) -> List[Dict]:
        start = float(cut.get("safe_start", cut.get("start", 0.0)))
        end = float(cut.get("safe_end", cut.get("end", 0.0)))
        if end <= start:
            return []

        entries: List[Dict] = []
        segments = self._segments_for_cut(
            transcript_segments=transcript_segments,
            cut_start=start,
            cut_end=end,
        )
        for segment in segments:
            local_start = max(0.0, float(segment.get("start", 0.0)) - start)
            local_end = min(end, float(segment.get("end", 0.0))) - start
            if local_end <= local_start:
                continue

            text = self._subtitle_text(segment)
            if not text:
                continue

            entries.append(
                {
                    "start": round(local_start, 3),
                    "end": round(local_end, 3),
                    "text": text,
                }
            )

        return self._merge_adjacent_entries(entries)

    def _segments_for_cut(
        self,
        *,
        transcript_segments: List[Dict],
        cut_start: float,
        cut_end: float,
    ) -> List[Dict]:
        return [
            segment
            for segment in transcript_segments
            if float(segment.get("end", 0.0)) > cut_start and float(segment.get("start", 0.0)) < cut_end
        ]

    def _subtitle_text(self, segment: Dict) -> str:
        text = str(segment.get("text") or "").strip()
        if not text:
            return ""
        wrapped = textwrap.wrap(text, width=34, break_long_words=False, break_on_hyphens=False)
        if not wrapped:
            return ""
        return "\n".join(wrapped[:2])

    def _merge_adjacent_entries(self, entries: List[Dict]) -> List[Dict]:
        if not entries:
            return []

        merged = [entries[0]]
        for entry in entries[1:]:
            previous = merged[-1]
            if (
                previous["text"] == entry["text"]
                and abs(previous["end"] - entry["start"]) <= 0.08
            ):
                previous["end"] = entry["end"]
                continue
            merged.append(entry)
        return merged

    def _format_timestamp(self, seconds: float) -> str:
        total_ms = max(0, int(round(seconds * 1000)))
        hours = total_ms // 3_600_000
        minutes = (total_ms % 3_600_000) // 60_000
        secs = (total_ms % 60_000) // 1000
        millis = total_ms % 1000
        return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"
