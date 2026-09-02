"""The duration model and the composition of the two QA layers (PR-QA-01)."""
from __future__ import annotations

import pytest

from app.pipeline.auto_review import AutoReviewPolicy
from app.video.render_timeline import (
    cold_open_metadata,
    expected_final_duration,
    transition_issues,
)


def clip(index, seconds, *, start=0.0, speed=1.0, transition_ms=0, cold_open=None):
    return {
        "clip_index": index,
        "safe_start": start,
        "safe_end": start + seconds,
        "source_start": start,
        "source_end": start + seconds,
        "duration": seconds,
        "playback_speed": speed,
        "transition_duration_ms": transition_ms,
        "cold_open": cold_open or {"enabled": False},
    }


def plan(clips, speed=1.0):
    return {"job_id": "t", "playback_speed": speed, "clips": clips}


# ==========================================================================
# Expected duration
# ==========================================================================


def test_a_single_clip_at_normal_speed():
    result = expected_final_duration(plan([clip(1, 30.0)]))
    assert result.expected_duration_sec == pytest.approx(30.0)
    assert result.source_duration_sec == pytest.approx(30.0)


def test_playback_speed_shortens_the_output():
    result = expected_final_duration(plan([clip(1, 30.0, speed=1.15)], speed=1.15))
    assert result.expected_duration_sec == pytest.approx(30.0 / 1.15)
    # The naive comparison a duration check must NOT make.
    assert result.source_duration_sec == pytest.approx(30.0)


def test_clips_are_summed():
    clips = [clip(1, 20.0), clip(2, 15.0, start=20.0), clip(3, 25.0, start=35.0)]
    assert expected_final_duration(plan(clips)).expected_duration_sec == pytest.approx(60.0)


def test_transitions_are_duration_neutral():
    """Fades are applied inside a clip, not as crossfades, so nothing is consumed."""
    without = expected_final_duration(plan([clip(1, 20.0), clip(2, 20.0, start=20.0)]))
    with_fades = expected_final_duration(
        plan([clip(1, 20.0, transition_ms=320), clip(2, 20.0, start=20.0, transition_ms=320)])
    )
    assert without.expected_duration_sec == with_fades.expected_duration_sec
    assert with_fades.transitions_duration_neutral is True


def test_the_cold_open_adds_its_own_length_at_the_final_speed():
    clips = [
        clip(1, 30.0, speed=1.15, cold_open={
            "enabled": True, "source_clip_index": 1,
            "duration_sec": 4.0, "relative_start_sec": 0.0,
        }),
    ]
    result = expected_final_duration(plan(clips, speed=1.15))
    assert result.expected_duration_sec == pytest.approx(4.0 / 1.15 + 30.0 / 1.15)
    assert result.cold_open_enabled is True


def test_the_cold_open_cannot_exceed_what_is_left_of_its_source_clip():
    """Mirrors the renderer, which clamps the teaser to the clip it is cut from."""
    clips = [
        clip(1, 5.0, cold_open={
            "enabled": True, "source_clip_index": 1,
            "duration_sec": 20.0, "relative_start_sec": 0.0,
        }),
    ]
    result = expected_final_duration(plan(clips))
    assert result.cold_open_sec == pytest.approx(5.0)


def test_a_cold_open_starting_past_its_source_clip_is_not_applied():
    clips = [
        clip(1, 5.0, cold_open={
            "enabled": True, "source_clip_index": 1,
            "duration_sec": 3.0, "relative_start_sec": 9.0,
        }),
    ]
    assert expected_final_duration(plan(clips)).cold_open_enabled is False


def test_per_clip_speed_overrides_are_honoured_and_flagged():
    clips = [clip(1, 30.0, speed=1.0), clip(2, 30.0, start=30.0, speed=1.5)]
    result = expected_final_duration(plan(clips))
    assert result.expected_duration_sec == pytest.approx(30.0 + 20.0)
    assert "clips_use_different_playback_speeds" in result.notes


def test_an_empty_plan_yields_no_expectation():
    result = expected_final_duration(None)
    assert result.clip_count == 0
    assert result.expected_duration_sec == 0.0


def test_the_cutters_range_wins_over_the_plan_duration_field():
    """VideoCutter encodes safe_start..safe_end, so that is the range that reaches ffmpeg."""
    odd = clip(1, 30.0)
    odd["duration"] = 999.0
    assert expected_final_duration(plan([odd])).expected_duration_sec == pytest.approx(30.0)


# ==========================================================================
# Cold open metadata
# ==========================================================================


def test_cold_open_metadata_records_the_intended_replay():
    clips = [
        clip(1, 30.0, speed=1.15, cold_open={
            "enabled": True, "source_clip_index": 1,
            "duration_sec": 4.0, "relative_start_sec": 2.0,
        }),
    ]
    metadata = cold_open_metadata(plan(clips, speed=1.15))
    assert metadata["enabled"] is True
    assert metadata["source_duration_sec"] == 4.0
    assert metadata["timeline_duration_sec"] == pytest.approx(4.0 / 1.15, abs=0.001)
    assert metadata["relative_start_sec"] == 2.0
    # The hook is heard twice by design; recording it distinguishes intent from a bug.
    assert metadata["replays_source_span"] is True


def test_no_cold_open_is_recorded_as_disabled():
    assert cold_open_metadata(plan([clip(1, 30.0)])) == {"enabled": False}


# ==========================================================================
# Transition integrity
# ==========================================================================


def test_a_fade_longer_than_its_clip_is_an_issue():
    issues = transition_issues(plan([clip(1, 2.0, transition_ms=3000)]))
    assert [issue.code for issue in issues] == ["transition_exceeds_clip"]


def test_fades_consuming_most_of_a_clip_are_an_issue():
    issues = transition_issues(plan([clip(1, 1.0, transition_ms=600)]))
    assert [issue.code for issue in issues] == ["transition_dominates_clip"]


def test_ordinary_fades_are_fine():
    assert transition_issues(plan([clip(1, 30.0, transition_ms=320)])) == []


def test_a_non_positive_clip_is_an_issue():
    assert [i.code for i in transition_issues(plan([clip(1, 0.0)]))] == ["clip_non_positive_duration"]


# ==========================================================================
# Composing the two QA layers
# ==========================================================================


def qa_report(decision="approved", score=95, clips=1):
    return {
        "decision": decision,
        "qa_scope": "source_cut",
        "clips": [
            {
                "clip_index": index,
                "file_name": f"cut_{index:02d}.mp4",
                "decision": decision,
                "score": score,
                "issues": [],
                "warnings": [],
            }
            for index in range(1, clips + 1)
        ],
    }


def final_media(status, reasons=(), blocking=()):
    return {
        "qa_scope": "final_output",
        "status": status,
        "reasons": list(reasons),
        "blocking_reasons": list(blocking),
        "summary": {"total_artifacts": 1},
    }


def test_auto_ready_requires_both_layers():
    result = AutoReviewPolicy().evaluate(
        qa_report=qa_report(),
        final_media_report=final_media("auto_ready"),
    )
    assert result["status"] == "auto_ready"
    assert result["publication_eligibility"]["eligible"] is True
    assert result["publication_eligibility"]["technical_gate"] == "pass"


def test_a_technical_block_overrides_a_perfect_editorial_score():
    """The core invariant: a high score must not launder a broken file."""
    result = AutoReviewPolicy().evaluate(
        qa_report=qa_report(score=100),
        final_media_report=final_media(
            "blocked", ["audio_fully_silent"], ["audio_fully_silent"]
        ),
    )
    assert result["editorial_status"] == "auto_ready"
    assert result["status"] == "blocked"
    assert "final_media:audio_fully_silent" in result["reasons"]


def test_a_technical_review_downgrades_an_auto_ready_job():
    result = AutoReviewPolicy().evaluate(
        qa_report=qa_report(score=100),
        final_media_report=final_media("needs_review", ["subtitle_out_of_bounds"]),
    )
    assert result["status"] == "needs_human_review"
    assert result["publication_eligibility"]["eligible"] is False


def test_final_media_qa_can_never_rescue_a_blocked_edit():
    result = AutoReviewPolicy().evaluate(
        qa_report=qa_report(decision="blocked", score=10),
        final_media_report=final_media("auto_ready"),
    )
    assert result["status"] == "blocked"


def test_a_missing_final_media_report_is_not_a_pass():
    """Never having looked is not the same as having found nothing."""
    result = AutoReviewPolicy().evaluate(qa_report=qa_report(score=100), final_media_report=None)
    assert result["status"] == "needs_human_review"
    assert result["publication_eligibility"]["technical_gate"] == "unmeasurable"
    assert result["publication_eligibility"]["eligible"] is False
    assert "final_media_qa_unavailable" in result["reasons"]


def test_publication_is_never_eligible_while_technically_blocked():
    """PR-QA-01 §23, exhaustively."""
    for status in ("blocked", "needs_review"):
        result = AutoReviewPolicy().evaluate(
            qa_report=qa_report(score=100),
            final_media_report=final_media(status, ["x"], ["x"] if status == "blocked" else []),
        )
        assert result["publication_eligibility"]["eligible"] is False, status


def test_no_publisher_is_claimed_to_exist():
    result = AutoReviewPolicy().evaluate(
        qa_report=qa_report(), final_media_report=final_media("auto_ready")
    )
    assert result["auto_publish_eligible"] is False
    assert result["publication_eligibility"]["publisher_available"] is False


def test_fast_track_requires_the_technical_gate():
    result = AutoReviewPolicy().evaluate(
        qa_report=qa_report(score=100),
        final_media_report=final_media("needs_review", ["video_frozen"]),
    )
    assert result["fast_track_eligible"] is False


def test_the_technical_verdict_is_reported_alongside_the_editorial_one():
    result = AutoReviewPolicy().evaluate(
        qa_report=qa_report(),
        final_media_report=final_media("needs_review", ["audio_long_silence"]),
    )
    assert result["final_media"]["status"] == "needs_review"
    assert result["final_media"]["reasons"] == ["audio_long_silence"]
    assert result["editorial_status"] == "auto_ready"


def test_the_previous_signature_still_works():
    """Callers that predate final-media QA must not break; they get `unmeasurable`."""
    result = AutoReviewPolicy().evaluate(qa_report(), [])
    assert result["status"] in {"blocked", "needs_human_review", "auto_ready"}
    assert result["publication_eligibility"]["technical_gate"] == "unmeasurable"
