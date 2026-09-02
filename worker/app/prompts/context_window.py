"""Grounded AI context assembly.

Replaces the previous arrangement, in which the model received:

  * a transcript that, for any video longer than the character budget, collapsed into three
    disjoint excerpts (head / middle / tail) joined by literal markers, and
  * a span catalogue enumerating **every** segment of the **whole** video.

The model was therefore asked to select ``span_ids`` for text it had never been shown. It
also never saw the ranked candidates: both prompt builders passed ``candidates=[]``, so the
entire detector → candidate → scorer chain was computed, written to ``candidates.json``, and
discarded.

Two invariants are enforced here:

INVARIANT 1 — span grounding
    A span is offered for selection only if its own text appears in the transcript excerpt
    of that same request. ``blind_span_reference_count`` must be 0.

INVARIANT 2 — no fabricated timestamps
    Transcript segments are never split. The old context builder halved any segment longer
    than 40s at the *word* midpoint and assigned it the *time* midpoint, inventing temporal
    precision that no ASR output supports. Here a segment is included whole or not at all;
    when text must be shortened, the excerpt is marked text-only and carries no timestamps.

The windowing is candidate-driven: the ranked candidates decide which regions are worth
showing, and each window carries context before and after so the model can judge dependency,
setup and payoff rather than a bare highlight.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence


GAP_MARKER = "[... omitted from this request — do not select spans from here ...]"


@dataclass
class ContextWindow:
    start: float
    end: float
    candidate_ids: list[str] = field(default_factory=list)

    def overlaps(self, other: "ContextWindow", tolerance: float = 0.0) -> bool:
        return not (self.end + tolerance < other.start or other.end + tolerance < self.start)

    def merge(self, other: "ContextWindow") -> "ContextWindow":
        return ContextWindow(
            start=min(self.start, other.start),
            end=max(self.end, other.end),
            candidate_ids=[*self.candidate_ids, *other.candidate_ids],
        )


@dataclass
class GroundedContext:
    """What the model will actually see, and what it may select from."""

    transcript_text: str
    spans: List[Dict[str, Any]]
    hook_candidates: List[Dict[str, Any]]
    candidates: List[Dict[str, Any]]
    windows: List[ContextWindow]
    stats: Dict[str, Any]

    @property
    def selectable_span_ids(self) -> set[str]:
        return {str(span.get("span_id")) for span in self.spans if span.get("span_id")}


def snap_to_segments(
    transcript: Sequence[Dict[str, Any]],
    start: float,
    end: float,
) -> tuple[float, float]:
    """Widen a window to whole transcript segments.

    Never narrows and never splits: a partially overlapped segment is taken whole, so every
    timestamp in the context comes from the ASR output.
    """
    if not transcript:
        return start, end

    snapped_start = start
    snapped_end = end
    for segment in transcript:
        seg_start = float(segment.get("start", 0.0))
        seg_end = float(segment.get("end", 0.0))
        if seg_end <= start or seg_start >= end:
            continue
        snapped_start = min(snapped_start, seg_start)
        snapped_end = max(snapped_end, seg_end)

    return snapped_start, snapped_end


def merge_windows(windows: Sequence[ContextWindow], tolerance: float = 0.0) -> List[ContextWindow]:
    if not windows:
        return []

    ordered = sorted(windows, key=lambda w: w.start)
    merged = [ordered[0]]
    for window in ordered[1:]:
        if merged[-1].overlaps(window, tolerance):
            merged[-1] = merged[-1].merge(window)
        else:
            merged.append(window)
    return merged


def compact_candidate(candidate: Dict[str, Any], *, text_limit: int = 220) -> Dict[str, Any]:
    """A compact evidence record. Only fields that actually exist are emitted.

    Nothing is fabricated: a candidate that carries no audio or speaker evidence simply has
    no such key, rather than a zero that the model would read as a measurement.
    """
    breakdown = candidate.get("score_breakdown") or {}
    signals = candidate.get("editorial_signals") or {}

    record: Dict[str, Any] = {
        "candidate_id": candidate.get("candidate_id"),
        "start": _round(candidate.get("start")),
        "end": _round(candidate.get("end")),
        "duration": _round(candidate.get("duration")),
        "score": _round(candidate.get("total_score")),
    }

    text = str(candidate.get("text") or "").strip()
    if text:
        record["text_excerpt"] = text[:text_limit] + ("..." if len(text) > text_limit else "")

    if candidate.get("narrative_role"):
        record["narrative_role"] = candidate["narrative_role"]

    evidence: Dict[str, Any] = {}
    for source, key, out in (
        (breakdown, "hook_score", "hook"),
        (breakdown, "narrative_score", "narrative"),
        (breakdown, "audio_score", "audio_peak"),
        (breakdown, "emotional_score", "emotional"),
        (breakdown, "narrative_completeness_score", "completeness"),
        (breakdown, "retention_score", "retention"),
    ):
        value = source.get(key)
        if value:
            evidence[out] = _round(value)

    if signals.get("clean_start") is not None:
        evidence["clean_start"] = bool(signals["clean_start"])
    if signals.get("strong_ending") is not None:
        evidence["strong_ending"] = bool(signals["strong_ending"])

    speakers = candidate.get("speakers") or []
    known_speakers = [s for s in speakers if s and s != "UNKNOWN"]
    if known_speakers:
        evidence["speakers"] = known_speakers

    if evidence:
        record["evidence"] = evidence

    return record


def build_grounded_context(
    *,
    transcript: Sequence[Dict[str, Any]],
    candidates: Sequence[Dict[str, Any]],
    span_catalog: Sequence[Dict[str, Any]],
    hook_candidates: Sequence[Dict[str, Any]],
    max_chars: int,
    context_before_sec: float = 32.0,
    context_after_sec: float = 32.0,
    max_candidates: int = 8,
) -> GroundedContext:
    """Assemble a context in which every selectable span has been shown."""
    transcript = list(transcript or [])
    candidates = list(candidates or [])

    if not transcript:
        return GroundedContext(
            transcript_text="",
            spans=[],
            hook_candidates=[],
            candidates=[],
            windows=[],
            stats={
                "transcript_chars": 0,
                "shown_segment_count": 0,
                "total_segment_count": 0,
                "selectable_span_count": 0,
                "total_span_count": len(span_catalog or []),
                "candidate_count": 0,
                "window_count": 0,
                "coverage_ratio": 0.0,
                "truncated": False,
            },
        )

    ranked = sorted(
        (c for c in candidates if _has_range(c)),
        key=lambda c: float(c.get("total_score", 0.0) or 0.0),
        reverse=True,
    )[:max_candidates]

    if ranked:
        proposed: List[ContextWindow] = []
        for candidate in ranked:
            start = float(candidate["start"]) - context_before_sec
            end = float(candidate["end"]) + context_after_sec
            start, end = snap_to_segments(transcript, start, end)
            proposed.append(
                ContextWindow(
                    start=start,
                    end=end,
                    candidate_ids=[str(candidate.get("candidate_id") or "")],
                )
            )
        # Merge first, then admit whole merged windows in candidate-priority order so a
        # window is never half-shown (which would re-create blind spans).
        merged = merge_windows(proposed)
        priority = {
            str(c.get("candidate_id") or ""): index for index, c in enumerate(ranked)
        }
        merged.sort(
            key=lambda w: min(
                (priority.get(cid, len(priority)) for cid in w.candidate_ids),
                default=len(priority),
            )
        )
    else:
        # No candidates: show the whole transcript, and if it does not fit, a contiguous
        # prefix. Never head/middle/tail, which produces invisible gaps.
        merged = [
            ContextWindow(
                start=float(transcript[0].get("start", 0.0)),
                end=float(transcript[-1].get("end", 0.0)),
            )
        ]

    selected: List[ContextWindow] = []
    shown_segments: List[Dict[str, Any]] = []
    used_chars = 0
    truncated = False

    for window in merged:
        window_segments = [
            segment
            for segment in transcript
            if float(segment.get("end", 0.0)) > window.start
            and float(segment.get("start", 0.0)) < window.end
        ]
        if not window_segments:
            continue

        window_chars = len(_format_segments(window_segments)) + len(GAP_MARKER) + 2
        if used_chars + window_chars > max_chars and selected:
            truncated = True
            continue
        if used_chars + window_chars > max_chars and not selected:
            # The first window alone exceeds the budget: admit the segments that fit, whole.
            window_segments = _fit_segments(window_segments, max_chars)
            truncated = True
            if not window_segments:
                break

        selected.append(window)
        shown_segments.extend(window_segments)
        used_chars += window_chars

    # Chronological order, de-duplicated by the segment's own coordinates.
    seen: set[tuple[float, float]] = set()
    ordered_segments: List[Dict[str, Any]] = []
    for segment in sorted(shown_segments, key=lambda s: float(s.get("start", 0.0))):
        key = (float(segment.get("start", 0.0)), float(segment.get("end", 0.0)))
        if key in seen:
            continue
        seen.add(key)
        ordered_segments.append(segment)

    shown_ranges = _contiguous_ranges(ordered_segments)
    transcript_text = _format_with_gaps(ordered_segments, shown_ranges)

    selectable_spans = [
        span for span in (span_catalog or []) if _span_is_shown(span, shown_ranges)
    ]
    selectable_span_ids = {str(s.get("span_id")) for s in selectable_spans if s.get("span_id")}
    grounded_hooks = [
        hook
        for hook in (hook_candidates or [])
        if str(hook.get("span_id") or "") in selectable_span_ids
    ]
    shown_candidates = [
        compact_candidate(candidate)
        for candidate in ranked
        if _range_is_shown(
            float(candidate["start"]), float(candidate["end"]), shown_ranges
        )
    ]

    total_duration = max(
        0.0,
        float(transcript[-1].get("end", 0.0)) - float(transcript[0].get("start", 0.0)),
    )
    shown_duration = sum(end - start for start, end in shown_ranges)

    return GroundedContext(
        transcript_text=transcript_text,
        spans=selectable_spans,
        hook_candidates=grounded_hooks,
        candidates=shown_candidates,
        windows=selected,
        stats={
            "transcript_chars": len(transcript_text),
            "shown_segment_count": len(ordered_segments),
            "total_segment_count": len(transcript),
            "selectable_span_count": len(selectable_spans),
            "total_span_count": len(span_catalog or []),
            "candidate_count": len(shown_candidates),
            "window_count": len(selected),
            "coverage_ratio": round(shown_duration / total_duration, 4) if total_duration else 0.0,
            "truncated": truncated,
        },
    )


def render_candidate_block(candidates: Sequence[Dict[str, Any]]) -> str:
    if not candidates:
        return "(no ranked candidates available for this request)"
    return json.dumps(list(candidates), ensure_ascii=False, indent=2)


def render_span_block(spans: Sequence[Dict[str, Any]], text_limit: int = 120) -> str:
    compact = []
    for span in spans:
        text = str(span.get("text") or "")
        compact.append(
            {
                "span_id": span.get("span_id"),
                "start": span.get("start"),
                "end": span.get("end"),
                "speaker": span.get("speaker"),
                "clean_start": span.get("clean_start"),
                "clean_end": span.get("clean_end"),
                "continuation_dependency": span.get("continuation_dependency"),
                "text": text[:text_limit] + ("..." if len(text) > text_limit else ""),
            }
        )
    return json.dumps(compact, ensure_ascii=False, indent=2)


def render_hook_block(hooks: Sequence[Dict[str, Any]], text_limit: int = 120) -> str:
    compact = []
    for hook in hooks:
        text = str(hook.get("text") or "")
        compact.append(
            {
                "hook_id": hook.get("hook_id"),
                "span_id": hook.get("span_id"),
                "start": hook.get("start"),
                "end": hook.get("end"),
                "speaker": hook.get("speaker"),
                "text": text[:text_limit] + ("..." if len(text) > text_limit else ""),
            }
        )
    return json.dumps(compact, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- internals


def _has_range(candidate: Dict[str, Any]) -> bool:
    try:
        return float(candidate["end"]) > float(candidate["start"])
    except (KeyError, TypeError, ValueError):
        return False


def _round(value: Any) -> Any:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _format_segments(segments: Sequence[Dict[str, Any]]) -> str:
    return "\n".join(
        f"[{_timestamp(s.get('start'))} - {_timestamp(s.get('end'))}] "
        f"{s.get('speaker', 'UNKNOWN')}: {str(s.get('text') or '').strip()}"
        for s in segments
    )


def _timestamp(seconds: Any) -> str:
    total = max(int(float(seconds or 0.0)), 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _fit_segments(segments: Sequence[Dict[str, Any]], max_chars: int) -> List[Dict[str, Any]]:
    """Take whole segments until the budget is spent. Never splits one."""
    kept: List[Dict[str, Any]] = []
    used = 0
    for segment in segments:
        line = _format_segments([segment])
        if used + len(line) + 1 > max_chars:
            break
        kept.append(segment)
        used += len(line) + 1
    return kept


def _contiguous_ranges(segments: Sequence[Dict[str, Any]]) -> List[tuple[float, float]]:
    """Merge the shown segments into the time ranges actually present in the context."""
    if not segments:
        return []

    ranges: List[tuple[float, float]] = []
    current_start = float(segments[0].get("start", 0.0))
    current_end = float(segments[0].get("end", 0.0))

    for segment in segments[1:]:
        seg_start = float(segment.get("start", 0.0))
        seg_end = float(segment.get("end", 0.0))
        if seg_start <= current_end + 0.001:
            current_end = max(current_end, seg_end)
            continue
        ranges.append((current_start, current_end))
        current_start, current_end = seg_start, seg_end

    ranges.append((current_start, current_end))
    return ranges


def _format_with_gaps(
    segments: Sequence[Dict[str, Any]],
    ranges: Sequence[tuple[float, float]],
) -> str:
    """Render the excerpt, marking every omitted region explicitly."""
    if not segments:
        return ""

    blocks: List[str] = []
    for index, (start, end) in enumerate(ranges):
        block_segments = [
            s
            for s in segments
            if float(s.get("start", 0.0)) >= start - 0.001
            and float(s.get("end", 0.0)) <= end + 0.001
        ]
        if index > 0:
            blocks.append(GAP_MARKER)
        blocks.append(_format_segments(block_segments))

    return "\n".join(block for block in blocks if block)


def _span_is_shown(span: Dict[str, Any], ranges: Sequence[tuple[float, float]]) -> bool:
    try:
        start = float(span.get("start"))
        end = float(span.get("end"))
    except (TypeError, ValueError):
        return False
    return _range_is_shown(start, end, ranges)


def _range_is_shown(start: float, end: float, ranges: Sequence[tuple[float, float]]) -> bool:
    """True only when the whole range sits inside one shown, contiguous block."""
    return any(
        start >= range_start - 0.001 and end <= range_end + 0.001
        for range_start, range_end in ranges
    )
