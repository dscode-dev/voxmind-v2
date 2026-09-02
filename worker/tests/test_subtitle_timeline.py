"""Subtitle artefact validation, and the cold-open timeline bug it exists to catch."""
from __future__ import annotations

import pytest

from app.pipeline.subtitle_builder import SubtitleBuilder
from app.video.subtitle_qa import check_subtitle_timeline, parse_subtitle_file


def write_ass(path, events):
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for start, end, text in events:
        sign = "-" if start < 0 else ""
        cs = int(round(abs(start) * 100))
        begin = f"{sign}{cs // 360000}:{(cs % 360000) // 6000:02}:{(cs % 6000) // 100:02}.{cs % 100:02}"
        ec = int(round(end * 100))
        finish = f"{ec // 360000}:{(ec % 360000) // 6000:02}:{(ec % 6000) // 100:02}.{ec % 100:02}"
        lines.append(f"Dialogue: 0,{begin},{finish},VoxMind,,0,0,0,,{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ==========================================================================
# Parsing
# ==========================================================================


def test_ass_events_are_parsed(tmp_path):
    timeline = parse_subtitle_file(write_ass(tmp_path / "a.ass", [(1.25, 3.5, "OLA"), (4.0, 6.0, "MUNDO")]))
    assert timeline.parse_ok
    assert timeline.event_count == 2
    assert timeline.events[0].start == pytest.approx(1.25)
    assert timeline.events[1].text == "MUNDO"


def test_a_comma_in_the_caption_text_survives(tmp_path):
    """The text field is the 10th, so the split must stop there."""
    timeline = parse_subtitle_file(write_ass(tmp_path / "a.ass", [(0.0, 1.0, "SIM, CLARO")]))
    assert timeline.events[0].text == "SIM, CLARO"


def test_srt_is_also_readable(tmp_path):
    path = tmp_path / "a.srt"
    path.write_text(
        "1\n00:00:01,500 --> 00:00:03,250\nPRIMEIRA\n\n2\n00:00:04,000 --> 00:00:06,000\nSEGUNDA\n",
        encoding="utf-8",
    )
    timeline = parse_subtitle_file(path)
    assert timeline.parse_ok
    assert timeline.events[0].start == pytest.approx(1.5)
    assert timeline.events[0].end == pytest.approx(3.25)


def test_a_missing_file_is_reported_not_raised():
    timeline = parse_subtitle_file(None)
    assert timeline.present is False


def test_a_malformed_line_is_reported(tmp_path):
    path = tmp_path / "a.ass"
    path.write_text("[Events]\nDialogue: 0,not-a-time,0:00:02.00,V,,0,0,0,,X\n", encoding="utf-8")
    timeline = parse_subtitle_file(path)
    assert timeline.parse_ok is False
    assert "unparseable timestamp" in (timeline.error or "")


# ==========================================================================
# Invariants
# ==========================================================================


def test_subtitles_inside_the_video_are_clean(tmp_path):
    timeline = parse_subtitle_file(write_ass(tmp_path / "a.ass", [(0.5, 3.0, "A"), (3.2, 9.0, "B")]))
    assert check_subtitle_timeline(timeline, video_duration_sec=10.0, tolerance_sec=0.5) == []


def test_an_event_past_the_end_is_out_of_bounds(tmp_path):
    timeline = parse_subtitle_file(write_ass(tmp_path / "a.ass", [(0.5, 14.0, "A")]))
    findings = check_subtitle_timeline(timeline, video_duration_sec=10.0, tolerance_sec=0.5)
    assert [f.code for f in findings] == ["subtitle_out_of_bounds"]


def test_the_tolerance_absorbs_encoder_rounding(tmp_path):
    timeline = parse_subtitle_file(write_ass(tmp_path / "a.ass", [(0.5, 10.3, "A")]))
    assert check_subtitle_timeline(timeline, video_duration_sec=10.0, tolerance_sec=0.5) == []


def test_a_negative_start_is_reported_as_negative_not_unparseable(tmp_path):
    timeline = parse_subtitle_file(write_ass(tmp_path / "a.ass", [(-1.5, 3.0, "A")]))
    findings = check_subtitle_timeline(timeline, video_duration_sec=10.0, tolerance_sec=0.5)
    assert "subtitle_negative_timestamp" in [f.code for f in findings]


def test_out_of_order_events_are_reported(tmp_path):
    timeline = parse_subtitle_file(write_ass(tmp_path / "a.ass", [(5.0, 6.0, "B"), (1.0, 2.0, "A")]))
    findings = check_subtitle_timeline(timeline, video_duration_sec=10.0, tolerance_sec=0.5)
    assert "subtitle_ordering_invalid" in [f.code for f in findings]


def test_an_impossible_range_is_reported(tmp_path):
    timeline = parse_subtitle_file(write_ass(tmp_path / "a.ass", [(5.0, 5.0, "A")]))
    findings = check_subtitle_timeline(timeline, video_duration_sec=10.0, tolerance_sec=0.5)
    assert "subtitle_impossible_range" in [f.code for f in findings]


def test_an_empty_file_is_reported(tmp_path):
    timeline = parse_subtitle_file(write_ass(tmp_path / "a.ass", []))
    findings = check_subtitle_timeline(timeline, video_duration_sec=10.0, tolerance_sec=0.5)
    assert [f.code for f in findings] == ["subtitle_file_empty"]


def test_absent_subtitles_are_only_a_finding_when_expected(tmp_path):
    timeline = parse_subtitle_file(tmp_path / "nope.ass")
    assert check_subtitle_timeline(timeline, video_duration_sec=10.0, tolerance_sec=0.5) != []
    assert check_subtitle_timeline(
        timeline, video_duration_sec=10.0, tolerance_sec=0.5, expect_subtitles=False
    ) == []


# ==========================================================================
# SubtitleBuilder: captions must land on the FINAL timeline
# ==========================================================================

SPEED = 1.15
CUTS = [{"start": 100.0, "end": 140.0, "safe_start": 100.0, "safe_end": 140.0}]
SEGMENTS = [
    {"start": 100.0, "end": 104.0, "text": "o tecnico assumiu o erro", "speaker": "S0"},
    {"start": 130.0, "end": 138.0, "text": "ninguem esqueceu daquilo", "speaker": "S0"},
]
COLD_OPEN = {"enabled": True, "source_clip_index": 1, "relative_start_sec": 0.0, "duration_sec": 4.0}


def build(cold_open=None, lead_in=0.0):
    return SubtitleBuilder(playback_speed=SPEED)._events_for_final_reel(
        cuts=CUTS,
        transcript_segments=SEGMENTS,
        lead_in_sec=lead_in,
        cold_open=cold_open,
    )


def test_playback_speed_is_applied_to_the_caption_timeline():
    events = build()
    # A line spoken 30s into the cut appears at 30/1.15 on screen.
    assert any(e["start"] == pytest.approx(30.0 / SPEED, abs=0.05) for e in events)


def test_captions_stay_in_sync_across_a_cold_open():
    """The regression this fixes.

    The builder used to skip the span the cold open had already shown, then advance its
    running offset by the shortened span - while the renderer replays clip 1 in full. Every
    caption after the teaser landed early by exactly the cold open's length (measured: a line
    at source 130.0s was captioned at 26.087s when it is on screen at 29.565s).
    """
    lead_in = 4.0 / SPEED
    events = build(cold_open=COLD_OPEN, lead_in=lead_in)

    on_screen_at = lead_in + (130.0 - 100.0) / SPEED
    starts = [e["start"] for e in events if "NINGUEM" in e["text"]]
    assert starts, "the line must be captioned"
    assert starts[0] == pytest.approx(on_screen_at, abs=0.05)


def test_the_cold_open_span_is_captioned_both_times_it_is_heard():
    """The renderer replays it deliberately, so the captions must too."""
    events = build(cold_open=COLD_OPEN, lead_in=4.0 / SPEED)
    assert sum(1 for e in events if e["text"] == "O TECNICO") == 2


def test_no_caption_runs_past_the_final_video():
    lead_in = 4.0 / SPEED
    events = build(cold_open=COLD_OPEN, lead_in=lead_in)
    final_duration = lead_in + 40.0 / SPEED
    assert max(e["end"] for e in events) <= final_duration + 0.01


def test_captions_are_ordered_and_non_negative():
    events = build(cold_open=COLD_OPEN, lead_in=4.0 / SPEED)
    assert all(e["start"] >= 0 for e in events)
    assert all(e["end"] > e["start"] for e in events)
    for previous, current in zip(events, events[1:]):
        assert previous["start"] <= current["start"]


def test_multiple_cuts_accumulate_the_full_played_length():
    cuts = [
        {"start": 0.0, "end": 20.0, "safe_start": 0.0, "safe_end": 20.0},
        {"start": 50.0, "end": 70.0, "safe_start": 50.0, "safe_end": 70.0},
    ]
    segments = [{"start": 60.0, "end": 62.0, "text": "segundo corte", "speaker": "S0"}]
    events = SubtitleBuilder(playback_speed=SPEED)._events_for_final_reel(
        cuts=cuts, transcript_segments=segments, lead_in_sec=0.0, cold_open=None
    )
    # Cut 1 occupies 20/1.15 on the timeline; the line is 10s into cut 2.
    assert events[0]["start"] == pytest.approx(20.0 / SPEED + 10.0 / SPEED, abs=0.05)
