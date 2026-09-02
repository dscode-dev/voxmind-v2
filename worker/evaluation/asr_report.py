"""ASR seam evaluation: BEFORE (naive concatenation) vs AFTER (reconciled).

Both arms consume the same fixtures and the same metric definitions, so the two numbers are
directly comparable. BEFORE is not a historical artefact here — it is naive concatenation
computed live, which is exactly what the pipeline did with non-overlapping windows and what
overlapping windows would produce with no reconciliation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from app.media.asr_windows import build_windows, overlap_overhead
from app.media.seam_reconciler import (
    SeamPolicy,
    count_duplicate_pairs,
    normalize_for_match,
    reconcile_windows,
)
from evaluation.asr_fixtures import (
    concatenate_without_reconciliation,
    load_seam_cases,
)


def _ordering_failures(segments: List[Dict[str, Any]]) -> int:
    failures = 0
    for index, segment in enumerate(segments):
        if float(segment["end"]) < float(segment["start"]):
            failures += 1
        if index and float(segment["start"]) < float(segments[index - 1]["start"]):
            failures += 1
    return failures


def _overlapping_pairs(segments: List[Dict[str, Any]]) -> int:
    return sum(
        1
        for a, b in zip(segments, segments[1:])
        if float(b["start"]) < float(a["end"]) - 0.001
    )


def _texts(segments: List[Dict[str, Any]]) -> List[str]:
    return [normalize_for_match(s.get("text", "")) for s in segments]


def _missing_expected(segments: List[Dict[str, Any]], expected: List[str]) -> int:
    """Expected utterances absent from the result — the seam-loss metric."""
    produced = _texts(segments)
    missing = 0
    for want in expected:
        target = normalize_for_match(want)
        if not any(target == got or target in got for got in produced):
            missing += 1
    return missing


def evaluate_arm(reconciled: bool, policy: SeamPolicy | None = None) -> Dict[str, Any]:
    policy = policy or SeamPolicy()
    cases: List[Dict[str, Any]] = []

    totals = {
        "asr_windows": 0,
        "asr_seams": 0,
        "segments_out": 0,
        "duplicate_segments": 0,
        "missing_seam_segments": 0,
        "timestamp_ordering_failures": 0,
        "overlapping_transcript_segments": 0,
        "expected_seam_text_recovered": 0,
        "expected_seam_text_total": 0,
    }

    for case in load_seam_cases():
        if reconciled:
            segments, stats = reconcile_windows(case.window_segments, policy)
            duplicates_removed = stats.duplicates_removed
        else:
            segments = concatenate_without_reconciliation(case.window_segments)
            duplicates_removed = 0

        duplicates = count_duplicate_pairs(segments, policy)
        missing = _missing_expected(segments, case.expected_texts)
        ordering = _ordering_failures(segments)
        overlaps = _overlapping_pairs(segments)
        recovered = len(case.expected_texts) - missing

        totals["asr_windows"] += len(case.window_segments)
        totals["asr_seams"] += max(0, len(case.window_segments) - 1)
        totals["segments_out"] += len(segments)
        totals["duplicate_segments"] += duplicates
        totals["missing_seam_segments"] += missing
        totals["timestamp_ordering_failures"] += ordering
        totals["overlapping_transcript_segments"] += overlaps
        totals["expected_seam_text_recovered"] += recovered
        totals["expected_seam_text_total"] += len(case.expected_texts)

        cases.append(
            {
                "case_id": case.case_id,
                "description": case.description,
                "segments_out": len(segments),
                "duplicate_segments": duplicates,
                "duplicates_removed": duplicates_removed,
                "missing_seam_segments": missing,
                "expected_recovered": f"{recovered}/{len(case.expected_texts)}",
                "ordering_failures": ordering,
                "overlapping_segments": overlaps,
                "texts": [s.get("text") for s in segments],
            }
        )

    return {"totals": totals, "cases": cases}


def performance_profile(
    audio_duration_sec: float = 5400.0,
    window_sec: float = 900.0,
    overlaps: tuple = (0.0, 5.0, 10.0, 30.0),
) -> List[Dict[str, Any]]:
    """The real cost of overlapping, for a representative 90-minute video."""
    return [
        overlap_overhead(build_windows(audio_duration_sec, window_sec, overlap), audio_duration_sec)
        | {"overlap_sec": overlap}
        for overlap in overlaps
    ]


def run_asr_evaluation() -> Dict[str, Any]:
    before = evaluate_arm(reconciled=False)
    after = evaluate_arm(reconciled=True)

    comparison = []
    for metric in sorted(before["totals"]):
        b, a = before["totals"][metric], after["totals"][metric]
        comparison.append({"metric": metric, "before": b, "after": a, "delta": a - b})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_type": "synthetic",
        "cases": len(load_seam_cases()),
        "before": before,
        "after": after,
        "comparison": comparison,
        "performance": performance_profile(),
    }


def write_report(report: Dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
