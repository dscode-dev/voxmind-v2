from app.pipeline.auto_review import AutoReviewPolicy


def test_auto_review_marks_job_auto_ready_when_scores_are_high():
    policy = AutoReviewPolicy(
        enabled=True,
        ready_score_threshold=85,
        blocked_score_threshold=45,
        max_review_clips=1,
    )

    report = policy.evaluate(
        qa_report={
            "clips": [
                {
                    "clip_index": 1,
                    "file_name": "cut_01.mp4",
                    "decision": "approved",
                    "score": 94,
                    "issues": [],
                    "warnings": [],
                },
                {
                    "clip_index": 2,
                    "file_name": "cut_02.mp4",
                    "decision": "approved",
                    "score": 90,
                    "issues": [],
                    "warnings": ["missing_hook"],
                },
            ]
        },
        cuts=[
            {"title": "clip 1", "description": "desc"},
            {"title": "clip 2", "description": "desc"},
        ],
        # PR-QA-01: auto_ready now requires BOTH layers. Editorial scores alone can no
        # longer declare a job ready, because nothing in them has seen the rendered file.
        final_media_report={
            "qa_scope": "final_output",
            "status": "auto_ready",
            "reasons": [],
            "blocking_reasons": [],
            "summary": {"total_artifacts": 2, "auto_ready": 2},
        },
    )

    assert report["status"] == "auto_ready"
    assert report["readiness_score"] >= 85
    assert report["summary"]["auto_ready_clips"] >= 1
    assert report["fast_track_eligible"] is True
    assert report["suggested_bulk_action"] == "approve_all_after_spot_check"
    assert report["recovery_plan"] is None
    assert report["publication_eligibility"]["technical_gate"] == "pass"


def test_auto_review_will_not_declare_a_job_ready_without_a_final_media_verdict():
    """Fail-closed (PR-QA-01 §23): not having looked is not the same as nothing wrong."""
    policy = AutoReviewPolicy(enabled=True)

    report = policy.evaluate(
        qa_report={
            "clips": [
                {
                    "clip_index": 1,
                    "file_name": "cut_01.mp4",
                    "decision": "approved",
                    "score": 99,
                    "issues": [],
                    "warnings": [],
                }
            ]
        },
        cuts=[{"title": "clip 1", "description": "desc"}],
        final_media_report=None,
    )

    assert report["editorial_status"] == "auto_ready"
    assert report["status"] == "needs_human_review"
    assert report["fast_track_eligible"] is False
    assert report["publication_eligibility"]["eligible"] is False


def test_auto_review_blocks_job_when_clip_is_blocked():
    policy = AutoReviewPolicy(enabled=True)

    report = policy.evaluate(
        qa_report={
            "clips": [
                {
                    "clip_index": 1,
                    "file_name": "cut_01.mp4",
                    "decision": "blocked",
                    "score": 30,
                    "issues": [{"severity": "blocked", "code": "render_invalid_duration"}],
                    "warnings": [],
                }
            ]
        },
        cuts=[{"title": "clip 1", "description": "desc"}],
    )

    assert report["status"] == "blocked"
    assert report["summary"]["blocked_clips"] == 1
    assert report["suggested_bulk_action"] == "regenerate_before_approval"
    assert report["recovery_plan"]["severity"] == "high"
