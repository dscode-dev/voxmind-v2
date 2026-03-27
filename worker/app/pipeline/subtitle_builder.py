from pathlib import Path
import re
from typing import Dict, List


class SubtitleBuilder:

    def __init__(self, playback_speed: float = 1.0):
        self.playback_speed = max(0.5, float(playback_speed or 1.0))

    def build_clip_srt(
        self,
        *,
        cut: Dict,
        transcript_segments: List[Dict],
        output_path: Path,
    ) -> Path | None:
        events = self._events_for_cut(
            cut=cut,
            transcript_segments=transcript_segments,
        )
        if not events:
            return None

        ass_path = output_path.with_suffix(".ass")
        ass_path.write_text(self._ass_document(events), encoding="utf-8")
        return ass_path

    def build_final_reel_srt(
        self,
        *,
        cuts: List[Dict],
        transcript_segments: List[Dict],
        output_path: Path,
        lead_in_sec: float = 0.0,
        cold_open: Dict | None = None,
    ) -> Path | None:
        events = self._events_for_final_reel(
            cuts=cuts,
            transcript_segments=transcript_segments,
            lead_in_sec=lead_in_sec,
            cold_open=cold_open,
        )
        if not events:
            return None

        ass_path = output_path.with_suffix(".ass")
        ass_path.write_text(self._ass_document(events), encoding="utf-8")
        return ass_path

    def _events_for_final_reel(
        self,
        *,
        cuts: List[Dict],
        transcript_segments: List[Dict],
        lead_in_sec: float = 0.0,
        cold_open: Dict | None = None,
    ) -> List[Dict]:
        events: List[Dict] = []
        accumulated_offset = 0.0
        cold_open = dict(cold_open or {})
        consumed_first_cut_until = None

        if cuts and cold_open.get("enabled") and int(cold_open.get("source_clip_index", 1) or 1) == 1:
            first_cut = cuts[0]
            first_start = float(first_cut.get("safe_start", first_cut.get("start", 0.0)))
            first_end = float(first_cut.get("safe_end", first_cut.get("end", 0.0)))
            relative_start = max(0.0, float(cold_open.get("relative_start_sec", 0.0) or 0.0))
            teaser_duration = max(0.0, float(cold_open.get("duration_sec", 0.0) or 0.0))
            teaser_start = first_start + relative_start
            teaser_end = min(first_end, teaser_start + teaser_duration)

            if teaser_end > teaser_start:
                events.extend(
                    self._events_for_range(
                        transcript_segments=transcript_segments,
                        source_start=teaser_start,
                        source_end=teaser_end,
                        timeline_offset=0.0,
                    )
                )
                accumulated_offset = max(0.0, float(lead_in_sec or 0.0))
                consumed_first_cut_until = teaser_end
            else:
                accumulated_offset = max(0.0, float(lead_in_sec or 0.0))
        else:
            accumulated_offset = max(0.0, float(lead_in_sec or 0.0))

        for cut_index, cut in enumerate(cuts):
            start = float(cut.get("safe_start", cut.get("start", 0.0)))
            end = float(cut.get("safe_end", cut.get("end", 0.0)))
            if end <= start:
                continue

            visible_start = start
            if cut_index == 0 and consumed_first_cut_until is not None:
                visible_start = max(visible_start, consumed_first_cut_until)
            if end <= visible_start:
                continue

            events.extend(
                self._events_for_range(
                    transcript_segments=transcript_segments,
                    source_start=visible_start,
                    source_end=end,
                    timeline_offset=accumulated_offset,
                )
            )
            accumulated_offset += max(0.0, end - visible_start) / self.playback_speed

        return events

    def _events_for_range(
        self,
        *,
        transcript_segments: List[Dict],
        source_start: float,
        source_end: float,
        timeline_offset: float,
    ) -> List[Dict]:
        events: List[Dict] = []
        segments = self._segments_for_cut(
            transcript_segments=transcript_segments,
            cut_start=source_start,
            cut_end=source_end,
        )
        for segment in segments:
            clipped_start = max(source_start, float(segment.get("start", 0.0)))
            clipped_end = min(source_end, float(segment.get("end", 0.0)))
            local_start = max(0.0, clipped_start - source_start) / self.playback_speed
            local_end = max(0.0, clipped_end - source_start) / self.playback_speed
            if local_end <= local_start:
                continue
            events.extend(
                self._chunked_events_for_segment(
                    start=timeline_offset + local_start,
                    end=timeline_offset + local_end,
                    text=str(segment.get("text") or "").strip(),
                )
            )
        return events

    def _events_for_cut(
        self,
        *,
        cut: Dict,
        transcript_segments: List[Dict],
    ) -> List[Dict]:
        start = float(cut.get("safe_start", cut.get("start", 0.0)))
        end = float(cut.get("safe_end", cut.get("end", 0.0)))
        if end <= start:
            return []

        events: List[Dict] = []
        segments = self._segments_for_cut(
            transcript_segments=transcript_segments,
            cut_start=start,
            cut_end=end,
        )
        for segment in segments:
            local_start = max(0.0, float(segment.get("start", 0.0)) - start) / self.playback_speed
            local_end = (min(end, float(segment.get("end", 0.0))) - start) / self.playback_speed
            if local_end <= local_start:
                continue
            events.extend(
                self._chunked_events_for_segment(
                    start=local_start,
                    end=local_end,
                    text=str(segment.get("text") or "").strip(),
                )
            )

        return events

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

    def _chunked_events_for_segment(
        self,
        *,
        start: float,
        end: float,
        text: str,
    ) -> List[Dict]:
        words = self._tokenize_words(text)
        if not words:
            return []

        duration = max(0.0, end - start)
        if duration <= 0.0:
            return []

        chunks = self._chunk_words(words, max_words=2, max_chars=14)
        total_chars = sum(max(1, len(" ".join(chunk))) for chunk in chunks)
        cursor = start
        events: List[Dict] = []

        for index, chunk in enumerate(chunks):
            weight = max(1, len(" ".join(chunk)))
            chunk_duration = duration * (weight / total_chars)
            chunk_start = cursor
            chunk_end = end if index == len(chunks) - 1 else min(end, cursor + chunk_duration)
            if chunk_end - chunk_start < 0.12:
                continue
            events.append(
                {
                    "start": round(chunk_start, 3),
                    "end": round(chunk_end, 3),
                    "text": " ".join(chunk).upper(),
                }
            )
            cursor = chunk_end

        return self._merge_adjacent_events(events)


    def _tokenize_words(self, text: str) -> List[str]:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        if not cleaned:
            return []
        return cleaned.split(" ")

    def _chunk_words(self, words: List[str], *, max_words: int, max_chars: int) -> List[List[str]]:
        chunks: List[List[str]] = []
        current: List[str] = []

        for word in words:
            tentative = current + [word]
            tentative_text = " ".join(tentative)
            if current and (len(tentative) > max_words or len(tentative_text) > max_chars):
                chunks.append(current)
                current = [word]
                continue
            current = tentative

        if current:
            chunks.append(current)
        return chunks

    def _merge_adjacent_events(self, events: List[Dict]) -> List[Dict]:
        if not events:
            return []

        merged = [events[0]]
        for event in events[1:]:
            previous = merged[-1]
            if previous["text"] == event["text"] and abs(previous["end"] - event["start"]) <= 0.04:
                previous["end"] = event["end"]
                continue
            merged.append(event)
        return merged

    def _ass_document(self, events: List[Dict]) -> str:
        header = """[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: VoxMind,DejaVu Sans,76,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,1,0,0,0,100,100,1.2,0,3,1,0,2,80,80,190,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""

        lines = [header.rstrip()]
        for event in events:
            lines.append(
                "Dialogue: 0,"
                f"{self._format_ass_timestamp(event['start'])},"
                f"{self._format_ass_timestamp(event['end'])},"
                "VoxMind,,0,0,0,,"
                f"{self._escape_ass_text(event['text'])}"
            )
        lines.append("")
        return "\n".join(lines)

    def _format_ass_timestamp(self, seconds: float) -> str:
        total_cs = max(0, int(round(seconds * 100)))
        hours = total_cs // 360000
        minutes = (total_cs % 360000) // 6000
        secs = (total_cs % 6000) // 100
        centis = total_cs % 100
        return f"{hours}:{minutes:02}:{secs:02}.{centis:02}"

    def _escape_ass_text(self, text: str) -> str:
        return (
            str(text or "")
            .replace("\\", r"\\")
            .replace("{", r"\{")
            .replace("}", r"\}")
        )
