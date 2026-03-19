from typing import Dict, List


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

    def evaluate(self, qa_report: Dict | None, cuts: List[Dict] | None = None) -> Dict:
        cuts = cuts or []
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
            status = "blocked"
            recommended_action = "regenerate_or_manual_recut"
            if readiness_score <= self.blocked_score_threshold:
                reasons.append("readiness_below_block_threshold")
        elif review_count <= self.max_review_clips and readiness_score >= self.ready_score_threshold:
            status = "auto_ready"
            recommended_action = "approve_after_spot_check"
            if not reasons:
                reasons.append("high_confidence_ready_for_fast_review")
        else:
            status = "needs_human_review"
            recommended_action = "human_review_required"
            if not reasons:
                reasons.append("manual_editorial_review_required")

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
            ),
            "suggested_bulk_action": self._suggested_bulk_action(status),
            "review_required": status != "auto_ready",
            "auto_publish_eligible": False,
            "reasons": reasons,
            "recovery_plan": self._recovery_plan(status, clip_decisions, reasons),
            "summary": {
                "total_clips": len(clip_decisions),
                "auto_ready_clips": auto_ready_count,
                "needs_human_review_clips": review_count,
                "blocked_clips": blocked_count,
            },
            "clips": clip_decisions,
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

        if not cut.get("title"):
            reasons.append("missing_title")
        if not cut.get("description"):
            reasons.append("missing_description")

        return sorted(set(reasons))

    def _has_structural_warning(self, qa_clip: Dict) -> bool:
        warning_codes = set(qa_clip.get("warnings", []))
        issue_codes = {issue.get("code") for issue in qa_clip.get("issues", [])}
        return bool(
            {"starts_mid_segment", "ends_mid_segment"} & warning_codes
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
