"""BEFORE/AFTER for the final-output quality gate.

Both arms run over the same real MP4 fixtures and are scored by the same function, so the
comparison is a comparison and not two different definitions of success.

**BEFORE** is the capability that existed before this PR, reconstructed honestly rather than
imagined. ``ClipQA`` was handed the *source cut* files and probed exactly one property of
them — ``format=duration`` — and ``AutoReviewPolicy`` read its scores. Nothing looked at the
assembled MP4. So the BEFORE arm here runs that same logic over the final artefacts: it can
still notice a file whose duration is 0 or unreadable, and nothing else. Where a check did
not exist at all the metric is reported ``unsupported``, never 0 — a zero would claim the old
gate looked and found nothing, when it never looked.

**AFTER** is ``FinalMediaQA``.

The headline number is ``invalid_outputs_auto_ready``: how many technically broken files each
arm would have declared fit to publish without a human watching them.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from app.pipeline.auto_review import AutoReviewPolicy
from app.video.final_media_qa import FinalMediaQA, FinalMediaQAInput, FinalMediaQAPolicy
from app.video.qa import ClipQA
from evaluation.final_qa_fixtures import FinalMediaCase, build_fixtures

UNSUPPORTED = "unsupported"

# Every metric, and which arms can produce a number for it. A metric the BEFORE arm has no
# mechanism for is `unsupported` there — see the module docstring.
_METRICS = (
    "final_files_evaluated",
    "technical_failures_present",
    "technical_failures_detected",
    "failures_detected_with_reason",
    "technical_failures_missed",
    "invalid_outputs_auto_ready",
    "container_failures_detected",
    "duration_mismatch_detected",
    "aspect_ratio_failures_detected",
    "decode_failures_detected",
    "silent_audio_detected",
    "missing_audio_detected",
    "black_or_frozen_detected",
    "subtitle_timeline_failures_detected",
    "valid_outputs_blocked",
    "expected_status_matches",
)

_BEFORE_SUPPORTED = {
    "final_files_evaluated",
    "technical_failures_present",
    "technical_failures_detected",
    "failures_detected_with_reason",
    "technical_failures_missed",
    "invalid_outputs_auto_ready",
    "container_failures_detected",
    "valid_outputs_blocked",
    "expected_status_matches",
}


def _is_failure_case(case: FinalMediaCase) -> bool:
    return case.expected_status != "auto_ready"


def _case_family(case: FinalMediaCase) -> str:
    codes = set(case.expected_codes)
    if codes & {"artifact_missing", "artifact_empty", "invalid_container"}:
        return "container"
    if codes & {"decode_error", "decode_timeout"}:
        return "decode"
    if codes & {"duration_mismatch", "duration_mismatch_severe"}:
        return "duration"
    if "wrong_aspect_ratio" in codes:
        return "aspect"
    if "audio_stream_missing" in codes:
        return "missing_audio"
    if codes & {"audio_fully_silent", "audio_long_silence", "audio_long_silence_severe", "audio_peak_clipping"}:
        return "audio"
    if codes & {"video_mostly_black", "video_frozen", "video_black_segment"}:
        return "picture"
    if any(code.startswith("subtitle_") for code in codes):
        return "subtitle"
    return "none"


def evaluate_after(cases: List[FinalMediaCase]) -> List[Dict[str, Any]]:
    """The AFTER arm: the gate this PR introduces."""
    gate = FinalMediaQA(policy=FinalMediaQAPolicy(), job_id="final-qa-eval")
    outcomes: List[Dict[str, Any]] = []

    for case in cases:
        report = gate.evaluate(
            FinalMediaQAInput(
                final_file=case.path,
                artifact_id=f"eval:{case.case_id}",
                render_plan=case.render_plan,
                subtitle_path=case.subtitle_path,
                video_ratio=case.video_ratio,
                expect_audio=case.expect_audio,
                expect_subtitles=case.expect_subtitles,
            )
        )
        outcomes.append(
            {
                "case_id": case.case_id,
                "expected_status": case.expected_status,
                "expected_codes": list(case.expected_codes),
                "status": report["status"],
                "reasons": report["reasons"],
                "family": _case_family(case),
                "is_failure_case": _is_failure_case(case),
            }
        )
    return outcomes


def evaluate_before(cases: List[FinalMediaCase]) -> List[Dict[str, Any]]:
    """The BEFORE arm: source-cut QA plus auto-review, pointed at the final artefacts.

    This is the most generous honest reconstruction. In the real pre-PR pipeline nothing
    evaluated these files at all, and auto-review ran before they existed; running the old
    logic over them here gives the old code every chance to notice a problem.
    """
    qa = ClipQA(min_duration_sec=1, max_duration_sec=600)
    policy = AutoReviewPolicy()
    outcomes: List[Dict[str, Any]] = []

    for case in cases:
        plan_clips = (case.render_plan or {}).get("clips") or []
        requested = [
            {
                "cut_id": f"{case.case_id}-{index}",
                "safe_start": clip.get("safe_start", 0.0),
                "safe_end": clip.get("safe_end", 0.0),
            }
            for index, clip in enumerate(plan_clips, start=1)
        ]
        # The old QA evaluates one rendered file per requested cut, so a multi-cut plan is
        # collapsed to the single artefact that actually exists.
        requested = requested[:1] or [{"cut_id": case.case_id, "safe_start": 0.0, "safe_end": 6.0}]
        report = qa.evaluate(
            requested_cuts=requested,
            rendered_files=[case.path],
            transcript_segments=[],
            post_metadata={
                "hook": "um hook estruturalmente valido para a avaliacao tecnica",
                "title": "titulo",
                "description": "descricao",
                "hashtags": ["#a", "#b", "#c"],
            },
        )
        automation = policy.evaluate(qa_report=report, cuts=requested)
        status = {
            "auto_ready": "auto_ready",
            "needs_human_review": "needs_review",
            "blocked": "blocked",
        }.get(str(automation.get("status")), "needs_review")

        reasons = sorted(
            {str(issue.get("code")) for clip in report["clips"] for issue in clip["issues"]}
        )
        outcomes.append(
            {
                "case_id": case.case_id,
                "expected_status": case.expected_status,
                "expected_codes": list(case.expected_codes),
                "status": status,
                "reasons": reasons,
                "family": _case_family(case),
                "is_failure_case": _is_failure_case(case),
            }
        )
    return outcomes


def score(outcomes: List[Dict[str, Any]], *, arm: str) -> Dict[str, Any]:
    """One metric definition, applied identically to both arms."""
    supported = _BEFORE_SUPPORTED if arm == "before" else set(_METRICS)
    failures = [o for o in outcomes if o["is_failure_case"]]
    valid = [o for o in outcomes if not o["is_failure_case"]]

    detected = [o for o in failures if o["status"] in {"blocked", "needs_review"}]
    metrics: Dict[str, Any] = {
        "final_files_evaluated": len(outcomes),
        "technical_failures_present": len(failures),
        "technical_failures_detected": len(detected),
        # Detection alone is a weak claim: an arm that refuses to approve ANYTHING scores
        # perfectly on it while naming nothing. This counts detections where the arm
        # actually identified the defect, which is what an operator or a publisher can act
        # on. Read it together with `valid_outputs_blocked`.
        "failures_detected_with_reason": sum(
            1 for o in detected if set(o["expected_codes"]) & set(o["reasons"])
        ),
        "technical_failures_missed": len(failures) - len(detected),
        # The headline: a broken file waved through as publishable.
        "invalid_outputs_auto_ready": sum(1 for o in failures if o["status"] == "auto_ready"),
        "valid_outputs_blocked": sum(1 for o in valid if o["status"] != "auto_ready"),
        "expected_status_matches": sum(1 for o in outcomes if o["status"] == o["expected_status"]),
    }

    for metric, family in (
        ("container_failures_detected", "container"),
        ("duration_mismatch_detected", "duration"),
        ("aspect_ratio_failures_detected", "aspect"),
        ("decode_failures_detected", "decode"),
        ("silent_audio_detected", "audio"),
        ("missing_audio_detected", "missing_audio"),
        ("black_or_frozen_detected", "picture"),
        ("subtitle_timeline_failures_detected", "subtitle"),
    ):
        in_family = [o for o in failures if o["family"] == family]
        metrics[metric] = sum(
            1
            for o in in_family
            if o["status"] in {"blocked", "needs_review"}
            and set(o["expected_codes"]) & set(o["reasons"])
        )

    return {metric: (metrics[metric] if metric in supported else UNSUPPORTED) for metric in _METRICS}


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for metric in _METRICS:
        left, right = before.get(metric), after.get(metric)
        delta = right - left if isinstance(left, int) and isinstance(right, int) else None
        rows.append({"metric": metric, "before": left, "after": right, "delta": delta})
    return rows


def run_final_qa_evaluation(workdir: Path | None = None) -> Dict[str, Any]:
    owned = workdir is None
    root = Path(workdir or tempfile.mkdtemp(prefix="final_qa_eval_"))
    try:
        cases = build_fixtures(root)

        started = time.monotonic()
        after_outcomes = evaluate_after(cases)
        after_seconds = time.monotonic() - started

        before_outcomes = evaluate_before(cases)

        before = score(before_outcomes, arm="before")
        after = score(after_outcomes, arm="after")

        media_seconds = sum(_probe_seconds(case) for case in cases)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cases": len(cases),
            "source_type": "synthetic",
            "before": before,
            "after": after,
            "comparison": compare(before, after),
            "performance": {
                "media_duration_sec": round(media_seconds, 3),
                "qa_duration_sec": round(after_seconds, 3),
                "qa_realtime_factor": round(after_seconds / media_seconds, 4) if media_seconds else None,
            },
            "per_case": [
                {
                    "case_id": a["case_id"],
                    "expected_status": a["expected_status"],
                    "before_status": b["status"],
                    "after_status": a["status"],
                    "after_reasons": a["reasons"],
                    "matched": a["status"] == a["expected_status"],
                }
                for a, b in zip(after_outcomes, before_outcomes)
            ],
        }
    finally:
        if owned:
            shutil.rmtree(root, ignore_errors=True)


def _probe_seconds(case: FinalMediaCase) -> float:
    from app.video.media_probe import probe_media

    if case.path is None or not case.path.exists():
        return 0.0
    return probe_media(case.path).duration_sec


def write_report(report: Dict[str, Any], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
