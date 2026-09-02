"""Decomposable cut-quality metrics.

Deliberately not a single quality_score: each metric answers one question, so a regression in
one dimension cannot be hidden by an improvement in another. Every metric records only what
the dataset can actually support — a case without diarization reports
``speaker_continuity: unmeasurable`` rather than a passing score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence


UNMEASURABLE = "unmeasurable"

# Structural, language-light signals. A cut opening on a bare conjunction or pronoun is
# grammatically dependent on the sentence before it, whatever the topic is.
DEPENDENT_OPENERS = re.compile(
    r"^\s*(e|mas|ent[aã]o|porque|por isso|s[oó] que|a[ií]|da[ií]|ele|ela|isso|essa|esse|"
    r"and|but|so|because|then|he|she|it|that|this)\b",
    re.IGNORECASE,
)
SENTENCE_TERMINATOR = re.compile(r"[.!?]\s*$")


@dataclass
class MetricAccumulator:
    """Aggregates per-case results into the report totals."""

    cases: int = 0
    cuts_evaluated: int = 0

    invalid_structural_outputs: int = 0
    valid_structural_outputs: int = 0
    repair_attempts: int = 0
    repair_success: int = 0

    blind_span_references: int = 0
    spans_offered: int = 0
    spans_shown: int = 0

    silent_dropped_cuts: int = 0
    explained_rejections: int = 0

    duration_contract_failures: int = 0

    boundary_start_failures: int = 0
    boundary_end_failures: int = 0
    boundary_measurable: int = 0

    speaker_continuity_failures: int = 0
    speaker_continuity_measurable: int = 0
    cases_with_degraded_diarization: int = 0

    candidate_backed_cuts: int = 0
    candidate_near_cuts: int = 0
    candidate_ignored_cuts: int = 0

    qa_auto_ready: int = 0
    qa_needs_review: int = 0
    qa_blocked: int = 0

    context_chars: List[int] = field(default_factory=list)
    candidate_counts: List[int] = field(default_factory=list)
    span_counts: List[int] = field(default_factory=list)

    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cases": self.cases,
            "cuts_evaluated": self.cuts_evaluated,
            "invalid_structural_outputs": self.invalid_structural_outputs,
            "valid_structural_outputs": self.valid_structural_outputs,
            "repair_attempts": self.repair_attempts,
            "repair_success": self.repair_success,
            "blind_span_references": self.blind_span_references,
            "spans_offered": self.spans_offered,
            "spans_shown": self.spans_shown,
            "silent_dropped_cuts": self.silent_dropped_cuts,
            "explained_rejections": self.explained_rejections,
            "duration_contract_failures": self.duration_contract_failures,
            "boundary_start_failures": self.boundary_start_failures,
            "boundary_end_failures": self.boundary_end_failures,
            "boundary_measurable": self.boundary_measurable,
            "speaker_continuity_failures": self.speaker_continuity_failures,
            "speaker_continuity_measurable": self.speaker_continuity_measurable,
            "cases_with_degraded_diarization": self.cases_with_degraded_diarization,
            "candidate_backed_cuts": self.candidate_backed_cuts,
            "candidate_near_cuts": self.candidate_near_cuts,
            "candidate_ignored_cuts": self.candidate_ignored_cuts,
            "qa_auto_ready": self.qa_auto_ready,
            "qa_needs_review": self.qa_needs_review,
            "qa_blocked": self.qa_blocked,
            "avg_context_chars": _mean(self.context_chars),
            "max_context_chars": max(self.context_chars) if self.context_chars else 0,
            "avg_candidates_in_context": _mean(self.candidate_counts),
            "avg_spans_in_context": _mean(self.span_counts),
            "errors": list(self.errors),
        }


# ------------------------------------------------------------------ 5.1 boundary quality


def segment_covering(segments: Sequence[Dict], timestamp: float) -> Dict | None:
    for segment in segments:
        if float(segment.get("start", 0.0)) <= timestamp <= float(segment.get("end", 0.0)):
            return segment
    return None


def evaluate_boundaries(cut: Dict, segments: Sequence[Dict]) -> Dict[str, Any]:
    """Does the cut open on a self-contained clause and close on a completed one?"""
    if not segments:
        return {"measurable": False}

    start = float(cut.get("safe_start", cut.get("start", 0.0)) or 0.0)
    end = float(cut.get("safe_end", cut.get("end", 0.0)) or 0.0)

    start_segment = segment_covering(segments, start)
    end_segment = segment_covering(segments, end)

    start_text = str((start_segment or {}).get("text") or "").strip()
    end_text = str((end_segment or {}).get("text") or "").strip()

    starts_mid_segment = bool(
        start_segment and float(start_segment.get("start", 0.0)) < start - 0.05
    )
    ends_mid_segment = bool(
        end_segment and float(end_segment.get("end", 0.0)) > end + 0.05
    )
    starts_dependent = bool(start_text and DEPENDENT_OPENERS.match(start_text))
    ends_unterminated = bool(end_text and not SENTENCE_TERMINATOR.search(end_text))

    return {
        "measurable": True,
        "starts_mid_segment": starts_mid_segment,
        "ends_mid_segment": ends_mid_segment,
        "starts_dependent_clause": starts_dependent,
        "ends_without_terminator": ends_unterminated,
        "start_failure": starts_mid_segment or starts_dependent,
        "end_failure": ends_mid_segment or ends_unterminated,
    }


# --------------------------------------------------------------- 5.2 duration validity


def evaluate_duration_contract(cuts: Sequence[Dict], contract) -> Dict[str, Any]:
    """Every stage must agree. A violation is a cut accepted upstream that a downstream
    stage would reject."""
    failures: List[Dict[str, Any]] = []
    total = 0.0

    for cut in cuts:
        start = float(cut.get("safe_start", cut.get("start", 0.0)) or 0.0)
        end = float(cut.get("safe_end", cut.get("end", 0.0)) or 0.0)
        duration = max(0.0, end - start)
        total += duration

        if duration < contract.min_renderable_cut_duration_sec:
            failures.append(
                {
                    "cut_id": cut.get("cut_id"),
                    "reason": "below_technical_floor",
                    "duration": round(duration, 2),
                }
            )
        elif duration < contract.min_internal_cut_duration_sec:
            failures.append(
                {
                    "cut_id": cut.get("cut_id"),
                    "reason": "below_editorial_minimum",
                    "duration": round(duration, 2),
                }
            )

    video_failures: List[Dict[str, Any]] = []
    if cuts:
        if total < contract.min_final_video_duration_sec:
            video_failures.append({"reason": "final_video_below_min", "total": round(total, 2)})
        if total > contract.max_final_video_duration_sec:
            video_failures.append({"reason": "final_video_above_max", "total": round(total, 2)})

    return {
        "cut_failures": failures,
        "video_failures": video_failures,
        "total_duration_sec": round(total, 2),
        "failure_count": len(failures) + len(video_failures),
    }


# ------------------------------------------------------------- 5.4 candidate coverage


def evaluate_candidate_coverage(
    cuts: Sequence[Dict],
    candidates: Sequence[Dict],
    *,
    near_tolerance_sec: float = 15.0,
) -> Dict[str, Any]:
    """How much of the precomputed intelligence the selection actually used.

    Candidate score is evidence, never authority: a cut that ignores every candidate is
    reported, not penalised.
    """
    if not candidates:
        return {"measurable": False, "backed": 0, "near": 0, "ignored": len(cuts)}

    backed = near = ignored = 0
    for cut in cuts:
        start = float(cut.get("safe_start", cut.get("start", 0.0)) or 0.0)
        end = float(cut.get("safe_end", cut.get("end", 0.0)) or 0.0)

        best_overlap = 0.0
        best_distance = float("inf")
        for candidate in candidates:
            c_start = float(candidate.get("start", 0.0))
            c_end = float(candidate.get("end", 0.0))
            overlap = max(0.0, min(end, c_end) - max(start, c_start))
            best_overlap = max(best_overlap, overlap)
            distance = min(abs(start - c_start), abs(end - c_end))
            best_distance = min(best_distance, distance)

        cut_duration = max(0.001, end - start)
        if best_overlap / cut_duration >= 0.5:
            backed += 1
        elif best_distance <= near_tolerance_sec:
            near += 1
        else:
            ignored += 1

    return {"measurable": True, "backed": backed, "near": near, "ignored": ignored}


# ----------------------------------------------------------------- 5.7 speaker continuity


def evaluate_speaker_continuity(
    cut: Dict,
    segments: Sequence[Dict],
    diarization_status: str,
) -> Dict[str, Any]:
    """Unmeasurable when diarization degraded — absence of data is not success."""
    if diarization_status != "available":
        return {"measurable": False, "reason": diarization_status}

    start = float(cut.get("safe_start", cut.get("start", 0.0)) or 0.0)
    end = float(cut.get("safe_end", cut.get("end", 0.0)) or 0.0)

    covered = [
        s
        for s in segments
        if float(s.get("end", 0.0)) > start and float(s.get("start", 0.0)) < end
    ]
    if not covered:
        return {"measurable": False, "reason": "no_segments_in_range"}

    speakers = [str(s.get("speaker") or "UNKNOWN") for s in covered]
    distinct = sorted(set(speakers))
    if distinct == ["UNKNOWN"]:
        return {"measurable": False, "reason": "all_unknown"}

    boundary_before = segment_covering(segments, start - 0.01)
    boundary_after = segment_covering(segments, end + 0.01)

    starts_mid_turn = bool(
        boundary_before
        and str(boundary_before.get("speaker")) == speakers[0]
        and float(boundary_before.get("end", 0.0)) > start + 0.05
    )
    ends_mid_turn = bool(
        boundary_after
        and str(boundary_after.get("speaker")) == speakers[-1]
        and float(boundary_after.get("start", 0.0)) < end - 0.05
    )
    turn_changes = sum(1 for a, b in zip(speakers, speakers[1:]) if a != b)

    return {
        "measurable": True,
        "distinct_speakers": len(distinct),
        "turn_changes": turn_changes,
        "starts_mid_turn": starts_mid_turn,
        "ends_mid_turn": ends_mid_turn,
        "failure": starts_mid_turn or ends_mid_turn or turn_changes > 6,
    }


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0
