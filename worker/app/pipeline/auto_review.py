"""Composes the two QA layers into one publication decision.

Source/editorial QA answers "were the right moments chosen?". Final Media QA answers "is the
file that came out of the renderer technically fit to publish?". They are different
questions and are kept as separate inputs here: a 96/100 editorial score must never launder
a final MP4 that is silent, black or truncated.

The invariant this class exists to hold (PR-QA-01 §23):

    a technical failure never produces publication eligibility.

No publisher exists yet. ``publication_eligibility`` is what the publisher will read when it
does, and it is computed now so the gate is in place before anything can upload.
"""
from typing import Dict, List

from app.video.final_media_qa import AUTO_READY, BLOCKED, NEEDS_REVIEW


class AutoReviewPolicy:

    def __init__(
        self,
        enabled: bool = True,
        ready_score_threshold: int = 85,
        blocked_score_threshold: int = 45,
        max_review_clips: int = 1,
    ):
        self.enabled = enabled
        self.ready_score_threshold = ready_score_threshold
        self.blocked_score_threshold = blocked_score_threshold
        self.max_review_clips = max_review_clips

    def evaluate(
        self,
        qa_report: Dict | None,
        cuts: List[Dict] | None = None,
        final_media_report: Dict | None = None,
    ) -> Dict:
        cuts = cuts or []
        technical = self._technical_gate(final_media_report)
        if not self.enabled:
            return {
                "enabled": False,
                "status": "disabled",
                "readiness_score": None,
                "recommended_action": "manual_review_only",
                "fast_track_eligible": False,
                "suggested_bulk_action": None,
                "review_required": True,
                "auto_publish_eligible": False,
                "publication_eligibility": self._publication_eligibility(
                    "disabled", technical, ["auto_review_disabled"]
                ),
                "final_media": technical["summary"],
                "reasons": ["auto_review_disabled"],
                "recovery_plan": None,
                "summary": {
                    "total_clips": len((qa_report or {}).get("clips", [])),
                    "auto_ready_clips": 0,
                    "needs_human_review_clips": len((qa_report or {}).get("clips", [])),
                    "blocked_clips": 0,
                },
                "clips": [],
            }

        clips = list((qa_report or {}).get("clips", []))
        if not clips:
            return {
                "enabled": True,
                "status": "blocked",
                "readiness_score": 0,
                "recommended_action": "regenerate_clips",
                "fast_track_eligible": False,
                "suggested_bulk_action": None,
                "review_required": True,
                "auto_publish_eligible": False,
                "publication_eligibility": self._publication_eligibility(
                    "blocked", technical, ["no_clips_available"]
                ),
                "final_media": technical["summary"],
                "reasons": ["no_clips_available"],
                "recovery_plan": {
                    "severity": "high",
                    "recommended_path": "regenerate_clips",
                    "next_steps": [
                        "reprocess_candidate_selection",
                        "validate_clip_durations",
                        "request_human_editor_review",
                    ],
                },
                "summary": {
                    "total_clips": 0,
                    "auto_ready_clips": 0,
                    "needs_human_review_clips": 0,
                    "blocked_clips": 0,
                },
                "clips": [],
            }

        clip_decisions = [
            self._evaluate_clip(
                qa_clip=qa_clip,
                cut=cuts[index] if index < len(cuts) else {},
            )
            for index, qa_clip in enumerate(clips)
        ]

        blocked_count = sum(1 for clip in clip_decisions if clip["status"] == "blocked")
        review_count = sum(1 for clip in clip_decisions if clip["status"] == "needs_human_review")
        auto_ready_count = sum(1 for clip in clip_decisions if clip["status"] == "auto_ready")
        readiness_score = round(
            sum(int(clip["score"]) for clip in clip_decisions) / len(clip_decisions)
        )

        reasons: List[str] = []
        if blocked_count > 0:
            reasons.append("blocked_clips_detected")
        if review_count > self.max_review_clips:
            reasons.append("too_many_review_clips")
        if readiness_score < self.ready_score_threshold:
            reasons.append("readiness_below_fast_track_threshold")

        if blocked_count > 0 or readiness_score <= self.blocked_score_threshold:
            editorial_status = "blocked"
            recommended_action = "regenerate_or_manual_recut"
            if readiness_score <= self.blocked_score_threshold:
                reasons.append("readiness_below_block_threshold")
        elif (
            auto_ready_count > 0
            and review_count <= self.max_review_clips
            and readiness_score >= self.ready_score_threshold
        ):
            editorial_status = "auto_ready"
            recommended_action = "approve_after_spot_check"
            if not reasons:
                reasons.append("high_confidence_ready_for_fast_review")
        else:
            editorial_status = "needs_human_review"
            recommended_action = "human_review_required"
            if not reasons:
                reasons.append("manual_editorial_review_required")

        # The final artefact is what gets published, so it can only ever make the decision
        # stricter. A technically blocked render is blocked no matter how the cuts scored.
        status = self._combine(editorial_status, technical)
        if status != editorial_status:
            recommended_action = (
                "regenerate_or_manual_recut" if status == "blocked" else "human_review_required"
            )
        reasons.extend(technical["reasons"])

        return {
            "enabled": True,
            "status": status,
            "readiness_score": readiness_score,
            "recommended_action": recommended_action,
            "fast_track_eligible": (
                status == "auto_ready"
                and blocked_count == 0
                and review_count == 0
                and auto_ready_count == len(clip_decisions)
                and technical["gate"] == "pass"
            ),
            "suggested_bulk_action": self._suggested_bulk_action(status),
            "review_required": status != "auto_ready",
            "auto_publish_eligible": False,
            "publication_eligibility": self._publication_eligibility(status, technical, reasons),
            "editorial_status": editorial_status,
            "final_media": technical["summary"],
            "reasons": sorted(set(reasons)),
            "recovery_plan": self._recovery_plan(status, clip_decisions, reasons),
            "summary": {
                "total_clips": len(clip_decisions),
                "auto_ready_clips": auto_ready_count,
                "needs_human_review_clips": review_count,
                "blocked_clips": blocked_count,
            },
            "clips": clip_decisions,
        }

    # ------------------------------------------------------------ technical gate

    def _technical_gate(self, final_media_report: Dict | None) -> Dict:
        """Reduce the Final Media QA report to a gate verdict plus its reasons.

        Absence of a report is *never* a pass. A render that was never checked is
        indistinguishable from one that failed silently, and treating "we did not look" as
        "it is fine" is exactly the class of bug this PR exists to close.
        """
        if not final_media_report:
            return {
                "gate": "unmeasurable",
                "status": None,
                "reasons": ["final_media_qa_unavailable"],
                "blocking_reasons": [],
                "summary": {
                    "status": None,
                    "evaluated": False,
                    "reason": "final_media_qa_unavailable",
                },
            }

        status = str(final_media_report.get("status") or "")
        reasons = [str(reason) for reason in final_media_report.get("reasons") or []]
        blocking = [str(reason) for reason in final_media_report.get("blocking_reasons") or []]
        gate = {AUTO_READY: "pass", NEEDS_REVIEW: "review", BLOCKED: "fail"}.get(status, "unmeasurable")

        prefixed = [f"final_media:{reason}" for reason in reasons]
        if gate == "unmeasurable":
            prefixed.append("final_media_status_unrecognised")

        return {
            "gate": gate,
            "status": status,
            "reasons": prefixed,
            "blocking_reasons": blocking,
            "summary": {
                "status": status,
                "evaluated": True,
                "summary": final_media_report.get("summary") or {},
                "reasons": reasons,
                "blocking_reasons": blocking,
            },
        }

    def _combine(self, editorial_status: str, technical: Dict) -> str:
        """Final media QA can only tighten the editorial verdict, never relax it."""
        if technical["gate"] == "fail":
            return "blocked"
        if editorial_status == "blocked":
            return "blocked"
        if technical["gate"] in {"review", "unmeasurable"}:
            return "needs_human_review"
        return editorial_status

    def _publication_eligibility(self, status: str, technical: Dict, reasons: List[str]) -> Dict:
        """What a future publisher may act on.

        No publisher exists in this PR. This field is the contract it will read, computed
        now so the gate predates the upload path rather than being retrofitted around it.
        """
        blocked_by: List[str] = []
        if technical["gate"] != "pass":
            blocked_by.append(f"final_media_qa_{technical['gate']}")
        blocked_by.extend(f"final_media:{code}" for code in technical["blocking_reasons"])
        if status != "auto_ready":
            blocked_by.append(f"review_status_{status}")

        return {
            "eligible": not blocked_by,
            "technical_gate": technical["gate"],
            "editorial_reasons": sorted({r for r in reasons if not r.startswith("final_media")}),
            "blocked_by": sorted(set(blocked_by)),
            # Kept explicit so nothing reads `eligible` as permission to upload today.
            "publisher_available": False,
        }

    def _evaluate_clip(self, qa_clip: Dict, cut: Dict) -> Dict:
        decision = str(qa_clip.get("decision", "needs_review"))
        qa_score = int(qa_clip.get("score", 0))
        reasons = self._clip_reasons(qa_clip, cut)

        if decision == "blocked":
            status = "blocked"
            recommended_action = "regenerate_clip"
        elif qa_score >= self.ready_score_threshold and not self._has_structural_warning(qa_clip):
            status = "auto_ready"
            recommended_action = "fast_track_review"
        else:
            status = "needs_human_review"
            recommended_action = "manual_clip_review"

        return {
            "clip_index": qa_clip.get("clip_index"),
            "file_name": qa_clip.get("file_name"),
            "status": status,
            "score": qa_score,
            "recommended_action": recommended_action,
            "suggested_review_decision": self._suggested_review_decision(status),
            "reasons": reasons,
        }

    def _clip_reasons(self, qa_clip: Dict, cut: Dict) -> List[str]:
        reasons: List[str] = []
        for issue in qa_clip.get("issues", []):
            code = issue.get("code")
            if code:
                reasons.append(str(code))

        warning_codes = set(qa_clip.get("warnings", []))
        if "starts_mid_segment" in warning_codes or "ends_mid_segment" in warning_codes:
            reasons.append("speaker_turn_boundary_risk")

        # Post metadata lives on the final video, not on a cut; QA already reports it.
        for warning in ("missing_title", "missing_description", "missing_hook"):
            if warning in warning_codes:
                reasons.append(warning)

        return sorted(set(reasons))

    def _has_structural_warning(self, qa_clip: Dict) -> bool:
        warning_codes = set(qa_clip.get("warnings", []))
        issue_codes = {issue.get("code") for issue in qa_clip.get("issues", [])}
        return bool(
            {"starts_mid_segment", "ends_mid_segment"} & warning_codes
            or {"speaker_continuity_unmeasurable", "weak_hook"} & warning_codes
            or {"render_duration_mismatch", "too_many_speakers"} & issue_codes
        )

    def _suggested_bulk_action(self, status: str) -> str | None:
        if status == "auto_ready":
            return "approve_all_after_spot_check"
        if status == "needs_human_review":
            return "review_priority_clips_first"
        if status == "blocked":
            return "regenerate_before_approval"
        return None

    def _suggested_review_decision(self, status: str) -> str:
        if status == "auto_ready":
            return "approved"
        if status == "blocked":
            return "needs_changes"
        return "manual_review"

    def _recovery_plan(
        self,
        status: str,
        clip_decisions: List[Dict],
        reasons: List[str],
    ) -> Dict | None:
        if status == "auto_ready":
            return None

        blocked_files = [
            clip.get("file_name")
            for clip in clip_decisions
            if clip.get("status") == "blocked" and clip.get("file_name")
        ]

        if status == "blocked":
            return {
                "severity": "high",
                "recommended_path": "regenerate_or_manual_recut",
                "blocked_files": blocked_files,
                "reason_codes": reasons,
                "next_steps": [
                    "inspect_blocked_clips",
                    "revisit_cut_boundaries",
                    "regenerate_problematic_clips",
                    "request_human_editor_review",
                ],
            }

        return {
            "severity": "medium",
            "recommended_path": "human_review_required",
            "blocked_files": blocked_files,
            "reason_codes": reasons,
            "next_steps": [
                "review_priority_clips_first",
                "spot_check_speaker_boundaries",
                "approve_or_request_adjustments",
            ],
        }
