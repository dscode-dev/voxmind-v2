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
    context_padding_sec: int = 32,
    max_candidates: int = 8,
    max_segments_per_candidate: int = 18,
    min_total_segments: int = 28,
) -> str:
    full_text = _format_transcript_segments(transcript)
    if len(full_text) <= max_chars:
        return full_text

    full_limited_segments = _limit_transcript_segments(
        transcript,
        max_segment_duration_sec=40.0,
    )
    full_limited_text = _format_transcript_segments(full_limited_segments)
    if len(full_limited_text) <= max_chars:
        return full_limited_text

    if not candidates:
        return _truncate_lines(full_limited_text, max_chars)

    prioritized_candidates = _prioritize_prompt_candidates(
        candidates,
        max_candidates=max_candidates,
    )
    focused_candidates = _select_focus_candidates(prioritized_candidates)
    selected_segments: List[Dict] = []
    seen_ranges: set[tuple[float, float]] = set()

    for candidate in focused_candidates:
        start = float(candidate["start"]) - context_padding_sec
        end = float(candidate["end"]) + context_padding_sec
        included_for_candidate = 0

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
            included_for_candidate += 1

            if included_for_candidate >= max_segments_per_candidate:
                break

    selected_segments = _expand_selected_segments(
        transcript=transcript,
        selected_segments=selected_segments,
        min_total_segments=min_total_segments,
    )
    selected_segments.sort(key=lambda item: float(item["start"]))
    focused_segments = _limit_transcript_segments(
        selected_segments,
        max_segment_duration_sec=40.0,
    )
    focused_text = _format_transcript_segments(focused_segments)

    if len(focused_text) <= max_chars:
        return focused_text

    return _truncate_lines(focused_text, max_chars)


def _expand_selected_segments(
    *,
    transcript: List[Dict],
    selected_segments: List[Dict],
    min_total_segments: int,
) -> List[Dict]:
    if len(selected_segments) >= min_total_segments or not transcript or not selected_segments:
        return selected_segments

    positions = {
        (float(segment.get("start", 0.0)), float(segment.get("end", 0.0))): index
        for index, segment in enumerate(transcript)
    }
    selected_keys = {
        (float(segment.get("start", 0.0)), float(segment.get("end", 0.0)))
        for segment in selected_segments
    }

    frontier = sorted(
        positions[key]
        for key in selected_keys
        if key in positions
    )
    if not frontier:
        return selected_segments

    left = frontier[0] - 1
    right = frontier[-1] + 1
    expanded = list(selected_segments)

    while len(expanded) < min_total_segments and (left >= 0 or right < len(transcript)):
        if left >= 0:
            segment = transcript[left]
            key = (float(segment.get("start", 0.0)), float(segment.get("end", 0.0)))
            if key not in selected_keys:
                expanded.append(segment)
                selected_keys.add(key)
                if len(expanded) >= min_total_segments:
                    break
            left -= 1

        if right < len(transcript):
            segment = transcript[right]
            key = (float(segment.get("start", 0.0)), float(segment.get("end", 0.0)))
            if key not in selected_keys:
                expanded.append(segment)
                selected_keys.add(key)
            right += 1

    return expanded


def build_candidate_context(candidates: List[Dict], max_chars: int) -> str:
    compact_candidates = []

    for candidate in _prioritize_prompt_candidates(candidates, max_candidates=len(candidates)):
        compact_candidates.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "source": candidate.get("source", "heuristic"),
                "start": candidate.get("start"),
                "end": candidate.get("end"),
                "duration": candidate.get("duration"),
                "total_score": candidate.get("total_score"),
                "narrative_role": candidate.get("narrative_role"),
                "speakers": candidate.get("speakers", []),
                "editorial_signals": candidate.get("editorial_signals", {}),
                "reason_signals": candidate.get("score_breakdown", {}),
                "text": _truncate_text(candidate.get("text", ""), 220),
            }
        )

    return _serialize_json_items_with_limit(compact_candidates, max_chars)


def build_timeline_context(
    transcript: List[Dict],
    max_chars: int,
    block_size_sec: int = 35,
) -> str:
    if not transcript:
        return ""

    blocks: List[Dict] = []
    current_segments: List[Dict] = []
    block_start = float(transcript[0].get("start", 0.0))
    block_end = block_start + block_size_sec

    for segment in transcript:
        segment_start = float(segment.get("start", 0.0))
        if current_segments and segment_start >= block_end:
            blocks.append(_build_timeline_block(current_segments))
            current_segments = []
            block_start = segment_start
            block_end = block_start + block_size_sec

        current_segments.append(segment)

    if current_segments:
        blocks.append(_build_timeline_block(current_segments))

    return _serialize_json_items_with_limit(blocks, max_chars)


def build_candidate_neighborhood_context(
    transcript: List[Dict],
    candidates: List[Dict],
    max_chars: int,
    max_candidates: int = 6,
    neighbor_segments: int = 7,
) -> str:
    if not transcript or not candidates:
        return ""

    neighborhoods: List[Dict] = []
    prioritized_candidates = _prioritize_prompt_candidates(candidates, max_candidates=max_candidates)

    for candidate in prioritized_candidates:
        start = float(candidate.get("start", 0.0))
        end = float(candidate.get("end", 0.0))
        matching_indexes = [
            index
            for index, segment in enumerate(transcript)
            if float(segment.get("end", 0.0)) >= start and float(segment.get("start", 0.0)) <= end
        ]
        if not matching_indexes:
            continue

        first_index = max(0, matching_indexes[0] - neighbor_segments)
        last_index = min(len(transcript), matching_indexes[-1] + neighbor_segments + 1)
        context_segments = transcript[first_index:last_index]
        neighborhoods.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "range": {
                    "start": start,
                    "end": end,
                },
                "text": _truncate_text(candidate.get("text", ""), 220),
                "source": candidate.get("source", "heuristic"),
                "neighbor_segments": [
                    {
                        "start": float(segment.get("start", 0.0)),
                        "end": float(segment.get("end", 0.0)),
                        "speaker": segment.get("speaker", "UNKNOWN"),
                        "text": _truncate_text(str(segment.get("text", "")).strip(), 100),
                    }
                    for segment in context_segments
                ],
            }
        )

    return _serialize_json_items_with_limit(neighborhoods, max_chars, compact_single_item=True)


def _select_dominant_candidate_cluster(
    candidates: List[Dict],
    max_gap_sec: float = 75.0,
) -> List[Dict]:
    if not candidates:
        return []

    ordered = sorted(candidates, key=lambda item: float(item.get("start", 0.0)))
    clusters: List[List[Dict]] = [[ordered[0]]]

    for candidate in ordered[1:]:
        previous = clusters[-1][-1]
        previous_end = float(previous.get("end", previous.get("start", 0.0)))
        candidate_start = float(candidate.get("start", 0.0))
        if candidate_start - previous_end <= max_gap_sec:
            clusters[-1].append(candidate)
            continue
        clusters.append([candidate])

    return max(
        clusters,
        key=lambda cluster: (
            sum(float(item.get("total_score", 0.0)) for item in cluster),
            -float(cluster[0].get("start", 0.0)),
        ),
    )


def _select_focus_candidates(candidates: List[Dict]) -> List[Dict]:
    if not candidates:
        return []

    primary_cluster = _select_dominant_candidate_cluster(candidates)
    selected = list(primary_cluster)
    selected_ids = {candidate.get("candidate_id") for candidate in selected}

    clipsai_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("source") == "clipsai" and candidate.get("candidate_id") not in selected_ids
    ][:2]
    selected.extend(clipsai_candidates)

    return sorted(selected, key=lambda item: float(item.get("start", 0.0)))


def _prioritize_prompt_candidates(candidates: List[Dict], max_candidates: int) -> List[Dict]:
    if not candidates:
        return []

    clipsai_candidates = sorted(
        [candidate for candidate in candidates if candidate.get("source") == "clipsai"],
        key=lambda item: item.get("total_score", 0.0),
        reverse=True,
    )
    heuristic_candidates = sorted(
        [candidate for candidate in candidates if candidate.get("source") != "clipsai"],
        key=lambda item: item.get("total_score", 0.0),
        reverse=True,
    )

    prioritized: List[Dict] = []
    clipsai_quota = min(2, max_candidates)
    heuristic_quota = max_candidates - clipsai_quota

    prioritized.extend(clipsai_candidates[:clipsai_quota])
    prioritized.extend(heuristic_candidates[:max(heuristic_quota, 0)])

    if len(prioritized) < max_candidates:
        overflow = clipsai_candidates[clipsai_quota:] + heuristic_candidates[max(heuristic_quota, 0):]
        for candidate in overflow:
            if candidate in prioritized:
                continue
            prioritized.append(candidate)
            if len(prioritized) >= max_candidates:
                break

    return prioritized


def _format_transcript_segments(segments: List[Dict]) -> str:
    lines = []

    for segment in segments:
        speaker = segment.get("speaker", "UNKNOWN")
        start = format_timestamp(float(segment["start"]))
        end = format_timestamp(float(segment["end"]))
        text = (segment.get("text") or "").strip()
        lines.append(f"[{start} - {end}] {speaker}: {text}")

    return "\n".join(lines)


def _build_timeline_block(segments: List[Dict]) -> Dict:
    start = float(segments[0].get("start", 0.0))
    end = float(segments[-1].get("end", 0.0))
    speakers = sorted(
        {
            str(segment.get("speaker", "UNKNOWN"))
            for segment in segments
            if str(segment.get("speaker", "")).strip()
        }
    )
    combined_text = " ".join(str(segment.get("text", "")).strip() for segment in segments).strip()
    return {
        "start": start,
        "end": end,
        "window": f"{format_timestamp(start)} - {format_timestamp(end)}",
        "speakers": speakers,
        "summary": _truncate_text(combined_text, 320),
    }


def _limit_transcript_segments(
    segments: List[Dict],
    max_segment_duration_sec: float = 24.0,
) -> List[Dict]:
    limited = []

    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        if end - start <= max_segment_duration_sec:
            limited.append(segment)
            continue

        text = (segment.get("text") or "").strip()
        if not text:
            continue

        midpoint = start + ((end - start) / 2)
        words = text.split()
        midpoint_index = max(1, len(words) // 2)

        limited.append(
            {
                **segment,
                "start": start,
                "end": round(midpoint, 2),
                "text": " ".join(words[:midpoint_index]).strip(),
            }
        )
        limited.append(
            {
                **segment,
                "start": round(midpoint, 2),
                "end": end,
                "text": " ".join(words[midpoint_index:]).strip(),
            }
        )

    return limited


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    truncated = text[: limit - 3].rstrip()
    truncated = truncated.rsplit(" ", 1)[0].rstrip(" ,;:")
    return truncated + "..."


def _serialize_json_items_with_limit(
    items: List[Dict],
    max_chars: int,
    compact_single_item: bool = False,
) -> str:
    serialized = json.dumps(items, ensure_ascii=False, indent=2)
    if len(serialized) <= max_chars:
        return serialized

    limited: List[Dict] = []
    for item in items:
        candidate = json.dumps([*limited, item], ensure_ascii=False, indent=2)
        if len(candidate) > max_chars:
            break
        limited.append(item)

    if limited:
        return json.dumps(limited, ensure_ascii=False, indent=2)

    if compact_single_item and items:
        compact_item = _compact_json_item(items[0])
        compact_serialized = json.dumps([compact_item], ensure_ascii=False, indent=2)
        if len(compact_serialized) <= max_chars:
            return compact_serialized

        compact_item["neighbor_segments"] = compact_item.get("neighbor_segments", [])[:2]
        compact_item["text"] = _truncate_text(str(compact_item.get("text", "")), 80)
        compact_serialized = json.dumps([compact_item], ensure_ascii=False, indent=2)
        if len(compact_serialized) <= max_chars:
            return compact_serialized

        compact_item["neighbor_segments"] = [
            {
                "start": segment.get("start"),
                "end": segment.get("end"),
                "speaker": segment.get("speaker"),
            }
            for segment in compact_item.get("neighbor_segments", [])[:1]
        ]
        compact_item["text"] = _truncate_text(str(compact_item.get("text", "")), 48)
        compact_serialized = json.dumps([compact_item], ensure_ascii=False, indent=2)
        if len(compact_serialized) <= max_chars:
            return compact_serialized

    return json.dumps(limited, ensure_ascii=False, indent=2)


def _compact_json_item(item: Dict) -> Dict:
    compact = dict(item)
    if "text" in compact:
        compact["text"] = _truncate_text(str(compact.get("text", "")), 120)
    if "neighbor_segments" in compact:
        compact["neighbor_segments"] = [
            {
                "start": segment.get("start"),
                "end": segment.get("end"),
                "speaker": segment.get("speaker"),
                "text": _truncate_text(str(segment.get("text", "")), 60),
            }
            for segment in compact.get("neighbor_segments", [])[:4]
        ]
    return compact


def _truncate_lines(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()
    selected: List[str] = []
    current_length = 0

    for line in lines:
        line_length = len(line) + (1 if selected else 0)
        if current_length + line_length > max_chars:
            break
        selected.append(line)
        current_length += line_length

    return "\n".join(selected)
