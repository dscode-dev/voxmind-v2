import json
from typing import Dict, List


def format_timestamp(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    return f"{minutes:02d}:{secs:02d}"


def build_transcript_context(
    transcript: List[Dict],
    candidates: List[Dict],
    max_chars: int,
    context_padding_sec: int = 12,
) -> str:
    full_text = _format_transcript_segments(transcript)
    if len(full_text) <= max_chars:
        return full_text

    if not candidates:
        return full_text[:max_chars]

    selected_segments: List[Dict] = []
    seen_ranges: set[tuple[float, float]] = set()

    for candidate in candidates:
        start = float(candidate["start"]) - context_padding_sec
        end = float(candidate["end"]) + context_padding_sec

        for segment in transcript:
            segment_start = float(segment["start"])
            segment_end = float(segment["end"])
            overlaps = segment_end >= start and segment_start <= end
            if not overlaps:
                continue

            key = (segment_start, segment_end)
            if key in seen_ranges:
                continue

            seen_ranges.add(key)
            selected_segments.append(segment)

    selected_segments.sort(key=lambda item: float(item["start"]))
    focused_text = _format_transcript_segments(selected_segments)

    if len(focused_text) <= max_chars:
        return focused_text

    return focused_text[:max_chars]


def build_candidate_context(candidates: List[Dict], max_chars: int) -> str:
    compact_candidates = []

    for candidate in candidates:
        compact_candidates.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "start": candidate.get("start"),
                "end": candidate.get("end"),
                "duration": candidate.get("duration"),
                "total_score": candidate.get("total_score"),
                "narrative_role": candidate.get("narrative_role"),
                "speakers": candidate.get("speakers", []),
                "editorial_signals": candidate.get("editorial_signals", {}),
                "reason_signals": candidate.get("score_breakdown", {}),
                "text": _truncate_text(candidate.get("text", ""), 280),
            }
        )

    serialized = json.dumps(compact_candidates, ensure_ascii=False, indent=2)
    return serialized[:max_chars]


def _format_transcript_segments(segments: List[Dict]) -> str:
    lines = []

    for segment in segments:
        speaker = segment.get("speaker", "UNKNOWN")
        start = format_timestamp(float(segment["start"]))
        end = format_timestamp(float(segment["end"]))
        text = (segment.get("text") or "").strip()
        lines.append(f"[{start} - {end}] {speaker}: {text}")

    return "\n".join(lines)


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
