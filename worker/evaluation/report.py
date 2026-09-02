"""Aggregates case results into a comparable report.

The same metric definitions produce BEFORE and AFTER, so the two files can be diffed
directly. Anything the dataset cannot measure is reported as unmeasurable rather than
silently counted as a pass.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from evaluation.metrics import MetricAccumulator
from evaluation.runner import CaseResult, discover_cases, run_case


def aggregate(results: List[CaseResult]) -> Dict[str, Any]:
    acc = MetricAccumulator()
    per_case: List[Dict[str, Any]] = []

    for result in results:
        acc.cases += 1
        detail = result.detail

        if not result.ok:
            acc.errors.append(f"{result.case_id}: {result.error}")
            per_case.append({"case_id": result.case_id, "ok": False, "error": result.error})
            continue

        if detail.get("structurally_valid"):
            acc.valid_structural_outputs += 1
        else:
            acc.invalid_structural_outputs += 1

        ai = detail.get("ai") or {}
        if ai.get("repair_attempted"):
            acc.repair_attempts += 1
        if ai.get("repair_success"):
            acc.repair_success += 1

        context = detail.get("context") or {}
        acc.context_chars.append(int(context.get("transcript_chars") or 0))
        acc.candidate_counts.append(int(context.get("candidate_count") or 0))
        acc.span_counts.append(int(context.get("selectable_span_count") or 0))
        # Offered = what the prompt advertised as selectable. Shown = what it actually
        # displayed. The gap between them is the blind-selection surface.
        acc.spans_offered += int(
            detail.get("spans_offered_to_model") or context.get("selectable_span_count") or 0
        )
        acc.spans_shown += int(detail.get("spans_shown_in_prompt") or 0)

        acc.blind_span_references += len(detail.get("blind_span_references") or [])

        if detail.get("diarization_status") != "available":
            acc.cases_with_degraded_diarization += 1

        for video in detail.get("videos") or []:
            ledger = video.get("ledger") or {}
            acc.silent_dropped_cuts += int(ledger.get("silent_drop_count") or 0)
            acc.explained_rejections += len(ledger.get("rejections") or [])

            duration = video.get("duration") or {}
            acc.duration_contract_failures += int(duration.get("failure_count") or 0)

            for boundary in video.get("boundaries") or []:
                if not boundary.get("measurable"):
                    continue
                acc.boundary_measurable += 1
                if boundary.get("start_failure"):
                    acc.boundary_start_failures += 1
                if boundary.get("end_failure"):
                    acc.boundary_end_failures += 1

            for speaker in video.get("speaker") or []:
                if not speaker.get("measurable"):
                    continue
                acc.speaker_continuity_measurable += 1
                if speaker.get("failure"):
                    acc.speaker_continuity_failures += 1

            coverage = video.get("candidate_coverage") or {}
            if coverage.get("measurable"):
                acc.candidate_backed_cuts += int(coverage.get("backed") or 0)
                acc.candidate_near_cuts += int(coverage.get("near") or 0)
                acc.candidate_ignored_cuts += int(coverage.get("ignored") or 0)

            acc.cuts_evaluated += int(video.get("cuts") or 0)

        qa = detail.get("qa") or {}
        status = qa.get("automation_status")
        if status == "auto_ready":
            acc.qa_auto_ready += 1
        elif status == "blocked":
            acc.qa_blocked += 1
        elif status is not None:
            acc.qa_needs_review += 1

        per_case.append(
            {
                "case_id": result.case_id,
                "ok": True,
                "source_type": result.source_type,
                "diarization_status": detail.get("diarization_status"),
                "candidates_ranked": detail.get("candidates_ranked"),
                "candidates_in_prompt": detail.get("candidates_in_prompt"),
                "context": context,
                "blind_span_references": len(detail.get("blind_span_references") or []),
                "structurally_valid": detail.get("structurally_valid"),
                "final_video_specs": detail.get("final_video_specs"),
                "videos": [
                    {
                        "video_index": v.get("video_index"),
                        "cuts": v.get("cuts"),
                        "renderable": v.get("renderable"),
                        "silent_drop_count": (v.get("ledger") or {}).get("silent_drop_count"),
                        "rejections": (v.get("ledger") or {}).get("rejections"),
                        "duration_failures": (v.get("duration") or {}).get("failure_count"),
                        "candidate_coverage": v.get("candidate_coverage"),
                    }
                    for v in (detail.get("videos") or [])
                ],
                "qa": qa,
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": acc.as_dict(),
        "cases": per_case,
    }


def run_evaluation(dataset: str = "voxmind") -> Dict[str, Any]:
    results: List[CaseResult] = []
    for case in discover_cases(dataset):
        try:
            results.append(run_case(case))
        except Exception as exc:  # noqa: BLE001 - a harness must survive one bad case
            results.append(
                CaseResult(case.case_id, case.source_type, ok=False, error=f"{type(exc).__name__}: {exc}")
            )
    report = aggregate(results)
    report["dataset"] = dataset
    report["dataset_summary"] = summarize_dataset(dataset)
    return report


def summarize_dataset(dataset: str = "voxmind") -> Dict[str, Any]:
    cases = discover_cases(dataset)
    return {
        "cases_total": len(cases),
        "real_cases": sum(1 for c in cases if c.source_type == "real"),
        "synthetic_cases": sum(1 for c in cases if c.source_type == "synthetic"),
        "labeled_cases": sum(1 for c in cases if c.is_labeled),
        "case_ids": [c.case_id for c in cases],
    }


def write_report(report: Dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Row-per-metric before/after with a delta, for the report table."""
    rows = []
    before_totals = before.get("totals", {})
    after_totals = after.get("totals", {})
    for key in sorted(set(before_totals) | set(after_totals)):
        b = before_totals.get(key)
        a = after_totals.get(key)
        if isinstance(b, (int, float)) and isinstance(a, (int, float)):
            delta = round(a - b, 2)
        else:
            delta = None
        rows.append({"metric": key, "before": b, "after": a, "delta": delta})
    return rows
