"""AI context: candidate evidence, span grounding, windows, honest timestamps (PR-CUT-01)."""

import pytest

from app.prompts.api_prompt_builder import ApiPromptBuilder
from app.prompts.context_window import (
    GAP_MARKER,
    build_grounded_context,
    compact_candidate,
    merge_windows,
    ContextWindow,
    snap_to_segments,
)


def transcript(count=60, duration=6.0, speaker="SPEAKER_00"):
    return [
        {
            "start": i * duration,
            "end": i * duration + duration,
            "text": f"linha {i} sobre o jogo de ontem no estadio.",
            "speaker": speaker,
        }
        for i in range(count)
    ]


def span_catalog(segments):
    return [
        {
            "span_id": f"span_{i:04d}",
            "start": s["start"],
            "end": s["end"],
            "text": s["text"],
            "speaker": s["speaker"],
            "hook_score": 1.0,
            "closure_score": 1.0,
            "clean_start": True,
            "clean_end": True,
            "continuation_dependency": False,
        }
        for i, s in enumerate(segments)
    ]


def candidate(cid, start, end, score=9.0):
    return {
        "candidate_id": cid,
        "start": start,
        "end": end,
        "duration": end - start,
        "total_score": score,
        "text": "trecho candidato",
        "narrative_role": "hook",
        "score_breakdown": {"hook_score": 6.0, "audio_score": 2.0},
        "editorial_signals": {"clean_start": True, "strong_ending": True},
        "speakers": ["SPEAKER_00"],
    }


# ==========================================================================
# Case D — span grounding
# ==========================================================================


def test_only_shown_spans_are_offered():
    segments = transcript(200)
    context = build_grounded_context(
        transcript=segments,
        candidates=[candidate("cand_0001", 300.0, 360.0)],
        span_catalog=span_catalog(segments),
        hook_candidates=[],
        max_chars=4000,
    )

    assert context.spans, "some spans must be offered"
    for span in context.spans:
        assert span["text"] in context.transcript_text


def test_no_span_outside_the_window_is_offered():
    segments = transcript(200)
    context = build_grounded_context(
        transcript=segments,
        candidates=[candidate("cand_0001", 300.0, 360.0)],
        span_catalog=span_catalog(segments),
        hook_candidates=[],
        max_chars=4000,
    )

    # The catalogue has 200 spans; only the windowed ones may be selectable.
    assert len(context.spans) < 200
    assert context.stats["selectable_span_count"] == len(context.spans)


def test_hooks_are_restricted_to_offered_spans():
    segments = transcript(120)
    spans = span_catalog(segments)
    hooks = [
        {"hook_id": f"hook_{i:04d}", "span_id": s["span_id"], "start": s["start"],
         "end": s["end"], "text": s["text"], "speaker": s["speaker"]}
        for i, s in enumerate(spans)
    ]

    context = build_grounded_context(
        transcript=segments,
        candidates=[candidate("cand_0001", 120.0, 180.0)],
        span_catalog=spans,
        hook_candidates=hooks,
        max_chars=3000,
    )

    offered = context.selectable_span_ids
    assert context.hook_candidates
    for hook in context.hook_candidates:
        assert hook["span_id"] in offered


def test_omitted_regions_are_marked_explicitly():
    segments = transcript(200)
    context = build_grounded_context(
        transcript=segments,
        candidates=[
            candidate("cand_0001", 60.0, 100.0),
            candidate("cand_0002", 900.0, 960.0, score=8.0),
        ],
        span_catalog=span_catalog(segments),
        hook_candidates=[],
        max_chars=6000,
    )

    assert GAP_MARKER in context.transcript_text


def test_grounding_holds_end_to_end_through_the_prompt_builder():
    segments = transcript(300)
    builder = ApiPromptBuilder(max_context_chars=8000)
    prompt = builder.build(
        transcript=segments,
        candidates=[candidate("cand_0001", 400.0, 460.0)],
        span_catalog=span_catalog(segments),
        hook_candidates=[],
        job_id="job-1",
        clip_mode="short_serie",
        video_ratio="portrait",
        job_preset="short_series",
    )

    for span in builder.last_context.spans:
        assert span["span_id"] in prompt
        assert span["text"] in prompt


# ==========================================================================
# Case C — candidate evidence reaches the model
# ==========================================================================


def test_ranked_candidates_appear_in_the_prompt():
    """The regression: both builders passed candidates=[] and the whole scorer chain was
    computed, written to candidates.json, and discarded."""
    segments = transcript(80)
    builder = ApiPromptBuilder(max_context_chars=8000)
    prompt = builder.build(
        transcript=segments,
        candidates=[candidate("cand_0001", 60.0, 120.0), candidate("cand_0002", 240.0, 300.0, 8.0)],
        span_catalog=span_catalog(segments),
        hook_candidates=[],
        job_id="job-1",
        clip_mode="short_serie",
        video_ratio="portrait",
        job_preset="short_series",
    )

    assert "cand_0001" in prompt
    assert "RANKED CANDIDATES" in prompt


def test_candidates_are_framed_as_advisory_not_authority():
    segments = transcript(60)
    builder = ApiPromptBuilder(max_context_chars=8000)
    prompt = builder.build(
        transcript=segments,
        candidates=[candidate("cand_0001", 60.0, 120.0)],
        span_catalog=span_catalog(segments),
        hook_candidates=[],
        job_id="job-1",
        clip_mode="short_serie",
        video_ratio="portrait",
        job_preset="short_series",
    )

    assert "NOT ground truth" in prompt
    assert "may reject a high-ranked candidate" in prompt


def test_compact_candidate_omits_absent_evidence():
    """No fabricated zeros: a candidate without audio evidence has no audio key."""
    record = compact_candidate(
        {
            "candidate_id": "c1",
            "start": 1.0,
            "end": 2.0,
            "duration": 1.0,
            "total_score": 3.0,
            "score_breakdown": {"hook_score": 2.0, "audio_score": 0.0},
            "speakers": ["UNKNOWN"],
        }
    )

    assert record["evidence"]["hook"] == 2.0
    assert "audio_peak" not in record["evidence"]
    assert "speakers" not in record["evidence"]


# ==========================================================================
# Case: no fabricated timestamps
# ==========================================================================


def test_every_timestamp_in_the_context_comes_from_a_real_segment():
    """The old builder split any >40s segment at the word midpoint and gave it the time
    midpoint, inventing precision the ASR never produced."""
    segments = [
        {"start": 0.0, "end": 90.0, "text": " ".join(f"palavra{i}" for i in range(200)),
         "speaker": "SPEAKER_00"},
        {"start": 90.0, "end": 96.0, "text": "segunda linha.", "speaker": "SPEAKER_00"},
    ]

    context = build_grounded_context(
        transcript=segments,
        candidates=[],
        span_catalog=[],
        hook_candidates=[],
        max_chars=100_000,
    )

    # 00:45 would be the fabricated midpoint of the 90s segment.
    assert "00:45" not in context.transcript_text
    assert "[00:00 - 01:30]" in context.transcript_text


def test_a_long_segment_is_included_whole_or_not_at_all():
    segments = [
        {"start": 0.0, "end": 120.0, "text": "x " * 2000, "speaker": "SPEAKER_00"},
    ]
    context = build_grounded_context(
        transcript=segments, candidates=[], span_catalog=[], hook_candidates=[], max_chars=200
    )
    # It does not fit, so it is omitted rather than halved.
    assert context.transcript_text == ""


# ==========================================================================
# Context windows
# ==========================================================================


def test_window_includes_context_before_and_after_the_candidate():
    segments = transcript(100)
    context = build_grounded_context(
        transcript=segments,
        candidates=[candidate("cand_0001", 300.0, 330.0)],
        span_catalog=span_catalog(segments),
        hook_candidates=[],
        max_chars=20000,
        context_before_sec=30.0,
        context_after_sec=30.0,
    )

    window = context.windows[0]
    assert window.start <= 300.0 - 30.0 + 6.0
    assert window.end >= 330.0 + 30.0 - 6.0


def test_snapping_widens_to_whole_segments_and_never_narrows():
    segments = transcript(20)
    start, end = snap_to_segments(segments, 13.0, 27.0)
    assert start <= 13.0
    assert end >= 27.0
    assert start in {s["start"] for s in segments}


def test_overlapping_windows_are_merged():
    merged = merge_windows(
        [ContextWindow(0.0, 50.0, ["a"]), ContextWindow(40.0, 90.0, ["b"]), ContextWindow(200.0, 240.0, ["c"])]
    )
    assert len(merged) == 2
    assert merged[0].start == 0.0 and merged[0].end == 90.0


def test_context_stays_within_budget():
    segments = transcript(900)
    context = build_grounded_context(
        transcript=segments,
        candidates=[candidate(f"cand_{i:04d}", i * 600.0, i * 600.0 + 60.0, 9.0 - i) for i in range(8)],
        span_catalog=span_catalog(segments),
        hook_candidates=[],
        max_chars=5000,
    )
    assert len(context.transcript_text) <= 5000 * 1.2


def test_no_candidates_shows_a_contiguous_excerpt_not_three_fragments():
    segments = transcript(50)
    context = build_grounded_context(
        transcript=segments, candidates=[], span_catalog=span_catalog(segments),
        hook_candidates=[], max_chars=100_000,
    )
    assert GAP_MARKER not in context.transcript_text
    assert context.stats["shown_segment_count"] == 50


def test_empty_transcript_is_handled():
    context = build_grounded_context(
        transcript=[], candidates=[], span_catalog=[], hook_candidates=[], max_chars=1000
    )
    assert context.transcript_text == ""
    assert context.spans == []
