"""Seam reconciliation for overlapping ASR windows.

Overlapping windows transcribe the shared region twice. Concatenating them would duplicate
speech; dropping one side blindly would lose the very content the overlap exists to recover.
This module decides, per seam, which version of a duplicated utterance to keep.

The decision rule is grounded in *where the window edges are*, not in a similarity score
alone:

    window N   [.....................|============]        (right edge truncates)
    window N+1              [========|.....................]  (left edge truncates)
                            ^        ^
                     overlap_start   N.end

* A segment that runs into ``window N``'s right edge was cut off there — window N+1 saw the
  same speech with the rest of the sentence following it, so **N+1's version wins**. This is
  the sentence-crosses-boundary case the overlap exists to fix.
* A segment that starts at ``window N+1``'s left edge was cut off there — window N had the
  preceding audio, so **N's version wins**.
* When neither is truncated, both windows saw the utterance fully; the more complete text
  wins, ties broken toward the earlier window so the result is deterministic.

No LLM is involved, and no word is invented: reconciliation only ever *chooses between*
transcriptions the model already produced, or keeps both when they are not duplicates.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class SeamPolicy:
    """Every threshold that governs duplicate detection, in one place.

    Scattered magic numbers were what made the old boundary heuristics unreviewable; these
    are named, defaulted together, and overridable from settings.
    """

    #: Minimum temporal intersection-over-union for two segments to be *considered* a pair.
    min_temporal_iou: float = 0.30
    #: Minimum normalized-text similarity for a temporally overlapping pair to be a duplicate.
    min_text_similarity: float = 0.60
    #: Text similarity high enough to call a duplicate even with weak temporal overlap
    #: (Whisper can shift a repeated utterance by a second or more between windows).
    strong_text_similarity: float = 0.85
    #: How close a timestamp must be to a window edge to count as truncated by it.
    edge_tolerance_sec: float = 0.25
    #: Segments closer than this after reconciliation are treated as a seam artifact and
    #: clamped so the transcript stays monotonic.
    max_clamp_sec: float = 0.50
    #: One text fully contained in the other, overlapping in time: the truncated-fragment
    #: case ("o problema foi" inside "o problema foi que o time perdeu").
    min_containment: float = 0.90
    #: Temporal agreement strong enough to call two segments the same speech even when the
    #: text barely matches — which is exactly what a word truncated at a window edge looks
    #: like ("a comissao tec" vs "a comissao tecnica sabia do problema").
    same_span_temporal_iou: float = 0.50


@dataclass
class ReconciliationStats:
    """What the reconciler did, for the evaluation harness and the logs."""

    windows: int = 0
    seams: int = 0
    segments_in: int = 0
    segments_out: int = 0
    duplicates_before: int = 0
    duplicates_removed: int = 0
    partial_pairs_preserved: int = 0
    clamped_overlaps: int = 0
    ordering_failures: int = 0
    dropped_by_window: Dict[int, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "windows": self.windows,
            "seams": self.seams,
            "segments_in": self.segments_in,
            "segments_out": self.segments_out,
            "duplicates_before_reconciliation": self.duplicates_before,
            "duplicates_removed": self.duplicates_removed,
            "duplicates_after_reconciliation": max(
                0, self.duplicates_before - self.duplicates_removed
            ),
            "partial_pairs_preserved": self.partial_pairs_preserved,
            "clamped_overlaps": self.clamped_overlaps,
            "ordering_failures": self.ordering_failures,
            "dropped_by_window": dict(self.dropped_by_window),
        }


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def normalize_for_match(text: str) -> str:
    """Casefolded, accent-stripped, punctuation-free form used ONLY for comparison.

    Never stored as transcript text: matching wants an aggressive form, the transcript wants
    what was actually said.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_WORD_RE.findall(stripped.casefold()))


def text_similarity(left: str, right: str) -> float:
    """Token-set similarity over the normalized forms (Jaccard, order-insensitive)."""
    left_tokens = set(normalize_for_match(left).split())
    right_tokens = set(normalize_for_match(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union if union else 0.0


def containment(left: str, right: str) -> float:
    """How much of the shorter token set is present in the longer one.

    Catches the partial-sentence case: "e o problema foi que o Palmeiras" is fully contained
    in "e o problema foi que o Palmeiras nao conseguiu vencer", which Jaccard would score
    only moderately.
    """
    left_tokens = set(normalize_for_match(left).split())
    right_tokens = set(normalize_for_match(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    smaller = min(len(left_tokens), len(right_tokens))
    return len(left_tokens & right_tokens) / smaller if smaller else 0.0


def temporal_iou(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    a_start, a_end = float(a["start"]), float(a["end"])
    b_start, b_end = float(b["start"]), float(b["end"])
    intersection = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    if intersection <= 0:
        return 0.0
    union = max(a_end, b_end) - min(a_start, b_start)
    return intersection / union if union > 0 else 0.0


def reconcile_windows(
    window_segments: Sequence[Tuple[Any, List[Dict[str, Any]]]],
    policy: SeamPolicy | None = None,
) -> Tuple[List[Dict[str, Any]], ReconciliationStats]:
    """Merge per-window segments into one transcript, resolving overlap duplicates.

    ``window_segments`` is an ordered sequence of ``(AsrWindow, segments)`` where every
    segment already carries **absolute** start/end.
    """
    policy = policy or SeamPolicy()
    stats = ReconciliationStats(windows=len(window_segments))

    if not window_segments:
        return [], stats

    kept: List[Dict[str, Any]] = []
    previous_window = None
    previous_kept: List[Dict[str, Any]] = []

    for window, segments in window_segments:
        segments = [dict(segment) for segment in segments]
        stats.segments_in += len(segments)
        for segment in segments:
            segment.setdefault("window_index", getattr(window, "index", None))

        if previous_window is None:
            previous_window, previous_kept = window, segments
            kept.extend(segments)
            continue

        stats.seams += 1
        surviving_previous, surviving_current = _resolve_seam(
            previous_window,
            previous_kept,
            window,
            segments,
            policy,
            stats,
        )

        # Replace the previous window's contribution with what survived the seam.
        for _ in range(len(previous_kept)):
            kept.pop()
        kept.extend(surviving_previous)
        kept.extend(surviving_current)

        previous_window, previous_kept = window, surviving_current

    ordered = _enforce_ordering(kept, policy, stats)
    stats.segments_out = len(ordered)
    return ordered, stats


def _resolve_seam(
    previous_window,
    previous_segments: List[Dict[str, Any]],
    current_window,
    current_segments: List[Dict[str, Any]],
    policy: SeamPolicy,
    stats: ReconciliationStats,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Decide, for one seam, which side of each duplicated utterance to keep."""
    # The shared region: from the current window's left edge to where the previous window
    # ended. Both windows transcribed exactly this span.
    region_start = float(current_window.start)
    region_end = min(float(previous_window.end), float(current_window.overlap_end))

    if region_end <= region_start:
        return previous_segments, current_segments

    tail = [s for s in previous_segments if _intersects(s, region_start, region_end)]
    head = [s for s in current_segments if _intersects(s, region_start, region_end)]

    dropped_previous: set[int] = set()
    dropped_current: set[int] = set()

    for tail_segment in tail:
        best_match = None
        best_score = 0.0

        tail_truncated = previous_window.touches_end(
            float(tail_segment["end"]), policy.edge_tolerance_sec
        )
        for head_segment in head:
            if id(head_segment) in dropped_current:
                continue
            head_truncated = current_window.touches_start(
                float(head_segment["start"]), policy.edge_tolerance_sec
            )
            score = _duplicate_score(
                tail_segment,
                head_segment,
                policy,
                edge_truncated=tail_truncated or head_truncated,
            )
            if score > best_score:
                best_score, best_match = score, head_segment

        if best_match is None:
            continue

        stats.duplicates_before += 1
        winner = _choose_winner(
            tail_segment, best_match, previous_window, current_window, policy
        )

        if winner is tail_segment:
            dropped_current.add(id(best_match))
            _record_drop(stats, current_window)
        else:
            dropped_previous.add(id(tail_segment))
            _record_drop(stats, previous_window)
        stats.duplicates_removed += 1

    # Complementary partials: a truncated tail and a truncated head that together form one
    # utterance are NOT duplicates. They were never matched above (low similarity), so both
    # survive here — counted so the behaviour is visible rather than assumed.
    for tail_segment in tail:
        if id(tail_segment) in dropped_previous:
            continue
        if not previous_window.touches_end(
            float(tail_segment["end"]), policy.edge_tolerance_sec
        ):
            continue
        for head_segment in head:
            if id(head_segment) in dropped_current:
                continue
            if containment(tail_segment.get("text", ""), head_segment.get("text", "")) > 0:
                continue
            if float(head_segment["start"]) >= float(tail_segment["end"]) - 0.5:
                stats.partial_pairs_preserved += 1
                break

    surviving_previous = [s for s in previous_segments if id(s) not in dropped_previous]
    surviving_current = [s for s in current_segments if id(s) not in dropped_current]
    return surviving_previous, surviving_current


def _duplicate_score(
    left: Dict[str, Any],
    right: Dict[str, Any],
    policy: SeamPolicy,
    edge_truncated: bool = False,
) -> float:
    """Non-zero when the pair is a duplicate candidate; higher is a better match."""
    iou = temporal_iou(left, right)
    similarity = text_similarity(left.get("text", ""), right.get("text", ""))
    contained = containment(left.get("text", ""), right.get("text", ""))

    # Whisper can shift a repeated utterance between windows, so very high text agreement
    # alone is enough; otherwise both signals must agree.
    if similarity >= policy.strong_text_similarity:
        return similarity + iou
    if iou >= policy.min_temporal_iou and similarity >= policy.min_text_similarity:
        return similarity + iou
    # A fully contained fragment overlapping in time is the truncated-at-the-edge case.
    if iou >= policy.min_temporal_iou and contained >= policy.min_containment:
        return contained + iou
    # Same span, one side cut off by a window edge: the words differ precisely *because*
    # one transcription was truncated mid-word, so text similarity cannot be trusted here.
    if edge_truncated and iou >= policy.same_span_temporal_iou:
        return iou
    return 0.0


def _choose_winner(
    tail_segment: Dict[str, Any],
    head_segment: Dict[str, Any],
    previous_window,
    current_window,
    policy: SeamPolicy,
) -> Dict[str, Any]:
    """Which transcription of the same speech to keep. See the module docstring."""
    tail_truncated = previous_window.touches_end(
        float(tail_segment["end"]), policy.edge_tolerance_sec
    )
    head_truncated = current_window.touches_start(
        float(head_segment["start"]), policy.edge_tolerance_sec
    )

    if tail_truncated and not head_truncated:
        return head_segment
    if head_truncated and not tail_truncated:
        return tail_segment

    # Neither (or both) truncated: prefer the more complete transcription.
    tail_words = len(normalize_for_match(tail_segment.get("text", "")).split())
    head_words = len(normalize_for_match(head_segment.get("text", "")).split())
    if head_words > tail_words:
        return head_segment
    if tail_words > head_words:
        return tail_segment

    # Word-level timings, when the model produced them, break the remaining ties: more
    # aligned words means a better-anchored segment.
    tail_word_count = len(tail_segment.get("words") or [])
    head_word_count = len(head_segment.get("words") or [])
    if head_word_count > tail_word_count:
        return head_segment

    # Deterministic default: the earlier window.
    return tail_segment


def _record_drop(stats: ReconciliationStats, window) -> None:
    index = getattr(window, "index", None)
    if index is None:
        return
    stats.dropped_by_window[index] = stats.dropped_by_window.get(index, 0) + 1


def _intersects(segment: Dict[str, Any], start: float, end: float) -> bool:
    return float(segment["end"]) > start and float(segment["start"]) < end


def _enforce_ordering(
    segments: List[Dict[str, Any]],
    policy: SeamPolicy,
    stats: ReconciliationStats,
) -> List[Dict[str, Any]]:
    """Guarantee the temporal invariants on the final transcript."""
    ordered = sorted(segments, key=lambda s: (float(s["start"]), float(s["end"])))

    result: List[Dict[str, Any]] = []
    for segment in ordered:
        segment = dict(segment)
        start = float(segment["start"])
        end = float(segment["end"])

        if end < start:
            stats.ordering_failures += 1
            start, end = end, start

        if result:
            previous_end = float(result[-1]["end"])
            if start < previous_end:
                # A small residual overlap is a seam artifact: nudge the start so the
                # transcript stays monotonic. A large one is real (e.g. crosstalk) and is
                # left alone rather than silently rewritten.
                if previous_end - start <= policy.max_clamp_sec:
                    start = previous_end
                    stats.clamped_overlaps += 1
                if end <= start:
                    continue

        segment["start"] = round(start, 3)
        segment["end"] = round(end, 3)
        result.append(segment)

    return result


def count_duplicate_pairs(
    segments: Iterable[Dict[str, Any]],
    policy: SeamPolicy | None = None,
) -> int:
    """Duplicate pairs remaining in a finished transcript. Used by the harness to measure
    the BEFORE case, where naive concatenation leaves them in."""
    policy = policy or SeamPolicy()
    items = list(segments)
    duplicates = 0
    for i, left in enumerate(items):
        for right in items[i + 1 : i + 6]:
            if float(right["start"]) > float(left["end"]) + 5.0:
                break
            # Segments from two different windows covering the same span are duplicates
            # regardless of how their text compares — the edge-truncation case.
            cross_window = (
                left.get("window_index") is not None
                and right.get("window_index") is not None
                and left["window_index"] != right["window_index"]
            )
            if _duplicate_score(left, right, policy, edge_truncated=cross_window) > 0:
                duplicates += 1
    return duplicates
