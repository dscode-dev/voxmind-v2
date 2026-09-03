"""Offline evaluation of the selection engine.

Runs the real ``SelectionEngine`` over the fixture dataset with no database, no network and
no model — the deterministic path only, so results are reproducible byte for byte.

**What this measures and what it does not.** There are no human labels, so there is no
precision, no NDCG and no "quality improved by X%". What is measured is *behaviour*: how old
the selected set is, how concentrated it is by channel and source, whether ineligible
candidates leak through, and how the ordering compares with the recency-only baseline the
system had before. Those are checkable claims. An accuracy figure computed against my own
fixtures would only measure agreement with my own assumptions.

    python -m evaluation.selection
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.selection.engine import CandidateView, SelectionEngine, recency_baseline
from app.selection.policy import SelectionConfig
from evaluation.selection.fixtures import NOW, TOPIC, load_candidates, load_cases

# Eligibility reasons that must never appear in a selected set, whichever arm produced it.
HARD_VIOLATIONS = {
    "unavailable": lambda c: c.available is False,
    "upcoming_live": lambda c: (c.live_status or "").lower() == "upcoming",
    "currently_live": lambda c: (c.live_status or "").lower() == "live",
    "already_selected": lambda c: c.status == "selected",
    "already_consumed": lambda c: c.status == "consumed",
    "previously_rejected": lambda c: c.status == "rejected",
    "duration_too_short": lambda c: c.duration_sec is not None and c.duration_sec < 60,
    "duration_too_long": lambda c: c.duration_sec is not None and c.duration_sec > 14_400,
    "outside_freshness_window": lambda c: (
        c.published_at is not None
        and (NOW - c.published_at).total_seconds() / 3600.0 > 72.0
    ),
}


def _age_hours(candidate: CandidateView) -> float | None:
    if candidate.published_at is None:
        return None
    return (NOW - candidate.published_at).total_seconds() / 3600.0


def measure(selected: list[CandidateView], *, arm: str) -> dict[str, Any]:
    """One metric definition, applied identically to both arms."""
    ages = [age for age in (_age_hours(c) for c in selected) if age is not None]
    channels = [c.channel_key for c in selected if c.channel_key]
    sources = [c.source_id for c in selected if c.source_id]

    violations: list[str] = []
    for candidate in selected:
        for name, predicate in HARD_VIOLATIONS.items():
            if predicate(candidate):
                violations.append(f"{candidate.candidate_id}:{name}")

    return {
        "arm": arm,
        "selected": len(selected),
        "eligibility_violations": len(violations),
        "violation_detail": violations,
        "distinct_channels": len(set(channels)),
        "distinct_sources": len(set(sources)),
        "max_per_channel": max(
            (channels.count(channel) for channel in set(channels)), default=0
        ),
        "average_selected_age_hours": round(statistics.mean(ages), 2) if ages else None,
        "oldest_selected_age_hours": round(max(ages), 2) if ages else None,
        "selected_with_view_counts": sum(1 for c in selected if c.view_count is not None),
        "selected_without_view_counts": sum(1 for c in selected if c.view_count is None),
        "selected_ids": [c.candidate_id for c in selected],
    }


def run_evaluation(*, limit: int = 3) -> dict[str, Any]:
    candidates = load_candidates()
    config = SelectionConfig()

    # ---- baseline: newest first, which is what discovery already offered ----
    baseline_selected = recency_baseline(candidates, limit=limit)
    baseline = measure(baseline_selected, arm="recency_only")

    # ---- selection-v1, deterministic path (no model configured) ----
    engine = SelectionEngine(config=config)
    outcome = engine.run(topic=TOPIC, candidates=candidates, config=config, now=NOW)
    engine_selected = [item.candidate for item in outcome.selected]
    after = measure(engine_selected, arm="selection_v1")

    scores = [item.final_score for item in outcome.ranked]
    after["score_spread"] = (
        round(max(scores) - min(scores), 4) if len(scores) > 1 else 0.0
    )
    after["score_max"] = round(max(scores), 4) if scores else None
    after["score_min"] = round(min(scores), 4) if scores else None
    after["distinct_scores"] = len({round(score, 3) for score in scores})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_type": "synthetic",
        "note": (
            "No human labels exist for this dataset. These are behaviour metrics, not "
            "quality metrics; no precision or NDCG is reported."
        ),
        "dataset": {
            "cases": len(candidates),
            "evaluated_at": NOW.isoformat(),
            "topic": TOPIC.name,
        },
        "config": config.as_dict(),
        "baseline": baseline,
        "selection_v1": after,
        "engine": {
            "considered": outcome.considered,
            "eligible": outcome.eligible,
            "ineligible": outcome.ineligible,
            "semantic_evaluated": outcome.semantic_evaluated,
            "semantic_failures": outcome.semantic_failures,
            "semantic_provider": outcome.semantic_provider,
            "duration_ms": outcome.duration_ms,
        },
        "ranking": [
            {
                "rank": item.rank,
                "candidate_id": item.candidate.candidate_id,
                "score": item.final_score,
                "decision": item.decision,
                "reasons": item.reasons,
                "blocked_by": item.blocked_by,
            }
            for item in outcome.ranked
        ],
        "ineligible": [
            {
                "candidate_id": item.candidate.candidate_id,
                "reasons": item.eligibility.reasons,
                "permanent": item.eligibility.permanent,
            }
            for item in outcome.ineligible_items
        ],
        "expectations": [
            {"case_id": case.case_id, "expectation": case.expectation}
            for case in load_cases()
        ],
    }


def compare(report: dict[str, Any]) -> list[dict[str, Any]]:
    baseline = report["baseline"]
    after = report["selection_v1"]
    rows = []
    for metric in (
        "selected",
        "eligibility_violations",
        "distinct_channels",
        "distinct_sources",
        "max_per_channel",
        "average_selected_age_hours",
        "selected_without_view_counts",
    ):
        left, right = baseline.get(metric), after.get(metric)
        delta = (
            round(right - left, 2)
            if isinstance(left, (int, float)) and isinstance(right, (int, float))
            else None
        )
        rows.append({"metric": metric, "recency_only": left, "selection_v1": right, "delta": delta})
    return rows


def write_report(report: dict[str, Any], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
