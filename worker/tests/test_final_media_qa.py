"""The final-output quality gate (PR-QA-01).

Two kinds of test here, deliberately kept apart:

* **policy tests** hand the evaluator a fabricated measurement, so a verdict can be asserted
  without producing a real corrupt MP4. Fast, and they pin the decision rules exactly.
* **measurement tests** run the real probe over real ffmpeg-generated files. Slower, and
  they are the only thing that proves the parsing survives contact with ffmpeg's output.

Nothing here touches the network.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.video.final_media_qa import (
    AUTO_READY,
    BLOCKED,
    NEEDS_REVIEW,
    FinalMediaQA,
    FinalMediaQAInput,
    FinalMediaQAPolicy,
    classify_failures,
    summarize,
)
from app.video.media_probe import (
    AudioStream,
    MediaAnalysis,
    MediaProbe,
    VideoStream,
    parse_analysis_log,
)

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


# ==========================================================================
# Fabricated measurements: the policy under exact control
# ==========================================================================


def probe(
    *,
    duration=30.0,
    width=1080,
    height=1920,
    audio=True,
    video=True,
    ok=True,
    exists=True,
    size=1_000_000,
    error=None,
) -> MediaProbe:
    return MediaProbe(
        path=Path("final_clip_01.mp4"),
        exists=exists,
        size_bytes=size,
        probe_ok=ok,
        error=error,
        format_name="mov,mp4",
        duration_sec=duration,
        duration_declared=duration > 0,
        video=VideoStream(codec="h264", width=width, height=height, frame_rate=30.0) if video else None,
        audio=AudioStream(codec="aac", sample_rate=48000, channels=2) if audio else None,
        stream_count=int(video) + int(audio),
    )


def analysis(
    *,
    duration=30.0,
    decode_ok=True,
    black=None,
    freeze=None,
    silence=None,
    mean_db=-18.0,
    peak_db=-3.0,
    clipped=0,
    total=1_440_000,
    audio_measured=True,
    timed_out=False,
) -> MediaAnalysis:
    return MediaAnalysis(
        decode_ok=decode_ok,
        timed_out=timed_out,
        black_ranges=black or [],
        freeze_ranges=freeze or [],
        silence_ranges=silence or [],
        mean_volume_db=mean_db,
        max_volume_db=peak_db,
        clipped_samples=clipped,
        total_samples=total,
        audio_measured=audio_measured,
        duration_sec=duration,
    )


def plan(*, clip_seconds=(30.0,), speed=1.0, cold_open=0.0, transition_ms=0):
    clips = []
    cursor = 0.0
    for index, seconds in enumerate(clip_seconds, start=1):
        clips.append(
            {
                "clip_index": index,
                "safe_start": cursor,
                "safe_end": cursor + seconds,
                "duration": seconds,
                "playback_speed": speed,
                "transition_duration_ms": transition_ms,
                "cold_open": (
                    {
                        "enabled": True,
                        "source_clip_index": 1,
                        "duration_sec": cold_open,
                        "relative_start_sec": 0.0,
                    }
                    if index == 1 and cold_open > 0
                    else {"enabled": False}
                ),
            }
        )
        cursor += seconds
    return {"job_id": "t", "playback_speed": speed, "clips": clips}


def evaluate(monkeypatch, *, media_probe, media_analysis=None, subtitle=None, **request):
    """Run the gate with the measurement layer stubbed out."""
    monkeypatch.setattr("app.video.final_media_qa.probe_media", lambda *a, **k: media_probe)
    monkeypatch.setattr(
        "app.video.final_media_qa.analyze_media",
        lambda *a, **k: media_analysis if media_analysis is not None else analysis(),
    )
    payload = {
        "final_file": Path("final_clip_01.mp4"),
        "artifact_id": "job-1:final_clip_01",
        "render_plan": plan(),
        "subtitle_path": subtitle,
        "expect_subtitles": subtitle is not None,
    }
    payload.update(request)
    return FinalMediaQA(policy=FinalMediaQAPolicy()).evaluate(FinalMediaQAInput(**payload))


# -------------------------------------------------------------- structural


def test_valid_media_passes(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe())
    assert report["status"] == AUTO_READY
    assert report["reasons"] == []
    assert all(check["status"] == "pass" for check in report["checks"].values())


def test_missing_file_blocks(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(exists=False, ok=False, error="file_not_found"))
    assert report["status"] == BLOCKED
    assert "artifact_missing" in report["blocking_reasons"]


def test_empty_file_blocks(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(size=0, ok=False))
    assert report["status"] == BLOCKED
    assert "artifact_empty" in report["blocking_reasons"]


def test_invalid_container_blocks(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(ok=False, error="moov atom not found"))
    assert report["status"] == BLOCKED
    assert "invalid_container" in report["blocking_reasons"]


def test_an_unopenable_file_is_not_analysed_further(monkeypatch):
    """A file ffprobe cannot open must never be handed to a reviewer as watchable."""
    report = evaluate(monkeypatch, media_probe=probe(ok=False, error="broken"))
    assert report["analysis"] is None
    assert set(report["checks"]) == {"container_valid"}


def test_missing_video_stream_blocks(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(video=False))
    assert report["status"] == BLOCKED
    assert "video_stream_missing" in report["blocking_reasons"]


def test_missing_audio_stream_blocks_when_audio_is_expected(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(audio=False))
    assert report["status"] == BLOCKED
    assert "audio_stream_missing" in report["blocking_reasons"]


def test_missing_audio_stream_is_fine_when_no_audio_is_expected(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(audio=False), expect_audio=False)
    assert report["checks"]["audio_stream"]["status"] == "pass"


def test_audio_content_is_unmeasurable_not_passing_without_a_stream(monkeypatch):
    """Absence of a measurement is never scored as a pass."""
    report = evaluate(monkeypatch, media_probe=probe(audio=False), expect_audio=False)
    assert report["checks"]["audio_silence"]["status"] == "unmeasurable"
    assert report["checks"]["audio_level"]["status"] == "unmeasurable"


def test_zero_duration_blocks(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(duration=0.0))
    assert report["status"] == BLOCKED
    assert "duration_invalid" in report["blocking_reasons"]


def test_decode_error_blocks(monkeypatch):
    report = evaluate(
        monkeypatch,
        media_probe=probe(),
        media_analysis=analysis(decode_ok=False),
    )
    assert report["status"] == BLOCKED
    assert "decode_error" in report["blocking_reasons"]


def test_decode_timeout_blocks_and_is_reported_separately(monkeypatch):
    report = evaluate(
        monkeypatch,
        media_probe=probe(),
        media_analysis=analysis(decode_ok=False, timed_out=True),
    )
    assert "decode_timeout" in report["blocking_reasons"]
    assert report["retry_classification"] == "retry_may_help"


# ---------------------------------------------------------------- duration


def test_duration_within_tolerance_passes(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(duration=30.4), render_plan=plan(clip_seconds=(30.0,)))
    assert report["checks"]["duration"]["status"] == "pass"


def test_duration_accounts_for_playback_speed(monkeypatch):
    """Comparing against sum(source) would be 4.3s wrong here."""
    report = evaluate(
        monkeypatch,
        media_probe=probe(duration=30.0 / 1.15),
        render_plan=plan(clip_seconds=(30.0,), speed=1.15),
    )
    assert report["checks"]["duration"]["status"] == "pass"


def test_duration_accounts_for_the_cold_open(monkeypatch):
    expected = 4.0 / 1.15 + 30.0 / 1.15
    report = evaluate(
        monkeypatch,
        media_probe=probe(duration=expected),
        render_plan=plan(clip_seconds=(30.0,), speed=1.15, cold_open=4.0),
    )
    assert report["checks"]["duration"]["status"] == "pass"


def test_moderate_duration_mismatch_needs_review(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(duration=33.0), render_plan=plan(clip_seconds=(30.0,)))
    assert report["status"] == NEEDS_REVIEW
    assert "duration_mismatch" in report["reasons"]


def test_severe_duration_mismatch_blocks(monkeypatch):
    """12s where 30s was planned: the render lost real content."""
    report = evaluate(monkeypatch, media_probe=probe(duration=12.0), render_plan=plan(clip_seconds=(30.0,)))
    assert report["status"] == BLOCKED
    assert "duration_mismatch_severe" in report["blocking_reasons"]


def test_duration_is_unmeasurable_without_a_plan(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(), render_plan=None)
    assert report["checks"]["duration"]["status"] == "unmeasurable"


# -------------------------------------------------------------- dimensions


@pytest.mark.parametrize("width,height", [(1080, 1920), (720, 1280), (540, 960)])
def test_any_9x16_resolution_satisfies_the_portrait_contract(monkeypatch, width, height):
    """The contract is the ratio, not one literal resolution."""
    report = evaluate(monkeypatch, media_probe=probe(width=width, height=height))
    assert report["checks"]["dimensions"]["status"] == "pass"


def test_landscape_file_fails_a_portrait_contract(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(width=1920, height=1080))
    assert report["status"] == BLOCKED
    assert "wrong_aspect_ratio" in report["blocking_reasons"]


def test_landscape_contract_accepts_16x9(monkeypatch):
    report = evaluate(
        monkeypatch, media_probe=probe(width=1920, height=1080), video_ratio="landscape"
    )
    assert report["checks"]["dimensions"]["status"] == "pass"


# ------------------------------------------------------------------- audio


def test_fully_silent_audio_blocks(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(), media_analysis=analysis(mean_db=-91.0))
    assert report["status"] == BLOCKED
    assert "audio_fully_silent" in report["blocking_reasons"]


def test_long_silence_needs_review(monkeypatch):
    """A 10s silent tail on a 30s video: the shape of the old 28s soundtrack fade."""
    report = evaluate(
        monkeypatch,
        media_probe=probe(),
        media_analysis=analysis(silence=[(20.0, 30.0)]),
    )
    assert report["status"] == NEEDS_REVIEW
    assert "audio_long_silence" in report["reasons"]


def test_silence_over_half_the_running_time_blocks(monkeypatch):
    report = evaluate(
        monkeypatch,
        media_probe=probe(),
        media_analysis=analysis(silence=[(2.0, 28.0)]),
    )
    assert report["status"] == BLOCKED
    assert "audio_long_silence_severe" in report["blocking_reasons"]


def test_short_silences_are_normal_speech(monkeypatch):
    report = evaluate(
        monkeypatch,
        media_probe=probe(),
        media_analysis=analysis(silence=[(3.0, 4.2), (12.0, 13.5)]),
    )
    assert report["checks"]["audio_silence"]["status"] == "pass"


def test_reaching_full_scale_needs_review(monkeypatch):
    report = evaluate(
        monkeypatch,
        media_probe=probe(),
        media_analysis=analysis(peak_db=0.0, clipped=320_000, total=1_440_000),
    )
    assert report["status"] == NEEDS_REVIEW
    assert "audio_peak_clipping" in report["reasons"]


def test_a_flat_topped_waveform_blocks(monkeypatch):
    report = evaluate(
        monkeypatch,
        media_probe=probe(),
        media_analysis=analysis(peak_db=0.0, clipped=800_000, total=1_440_000),
    )
    assert report["status"] == BLOCKED
    assert "audio_severe_clipping" in report["blocking_reasons"]


def test_a_hot_but_unclipped_mix_passes(monkeypatch):
    """-0.3 dB is the measured peak of a clean full-scale sine; it must not be flagged."""
    report = evaluate(
        monkeypatch,
        media_probe=probe(),
        media_analysis=analysis(peak_db=-0.3, clipped=280_000, total=1_440_000),
    )
    assert report["checks"]["audio_level"]["status"] == "pass"


def test_very_quiet_audio_needs_review(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(), media_analysis=analysis(mean_db=-52.0))
    assert "audio_level_too_low" in report["reasons"]


# ------------------------------------------------------------------- video


def test_mostly_black_video_blocks(monkeypatch):
    report = evaluate(
        monkeypatch, media_probe=probe(), media_analysis=analysis(black=[(0.0, 25.0)])
    )
    assert report["status"] == BLOCKED
    assert "video_mostly_black" in report["blocking_reasons"]


def test_a_black_segment_needs_review(monkeypatch):
    report = evaluate(
        monkeypatch, media_probe=probe(), media_analysis=analysis(black=[(0.0, 9.0)])
    )
    assert report["status"] == NEEDS_REVIEW
    assert "video_black_segment" in report["reasons"]


def test_a_short_black_transition_is_not_a_defect(monkeypatch):
    report = evaluate(
        monkeypatch, media_probe=probe(), media_analysis=analysis(black=[(0.0, 0.6)])
    )
    assert report["checks"]["video_content"]["status"] == "pass"


def test_a_long_freeze_needs_review(monkeypatch):
    report = evaluate(
        monkeypatch, media_probe=probe(), media_analysis=analysis(freeze=[(5.0, 14.0)])
    )
    assert report["status"] == NEEDS_REVIEW
    assert "video_frozen" in report["reasons"]


# --------------------------------------------------------------- subtitles


def write_ass(path: Path, events) -> Path:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for start, end, text in events:
        def stamp(value):
            sign = "-" if value < 0 else ""
            cs = int(round(abs(value) * 100))
            return f"{sign}{cs // 360000}:{(cs % 360000) // 6000:02}:{(cs % 6000) // 100:02}.{cs % 100:02}"

        lines.append(f"Dialogue: 0,{stamp(start)},{stamp(end)},VoxMind,,0,0,0,,{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_subtitles_inside_the_video_pass(monkeypatch, tmp_path):
    subs = write_ass(tmp_path / "s.ass", [(0.5, 4.0, "UM"), (4.2, 29.0, "DOIS")])
    report = evaluate(monkeypatch, media_probe=probe(duration=30.0), subtitle=subs)
    assert report["checks"]["subtitle_timing"]["status"] == "pass"


def test_subtitle_past_the_end_needs_review(monkeypatch, tmp_path):
    subs = write_ass(tmp_path / "s.ass", [(0.5, 4.0, "UM"), (28.0, 44.0, "DEPOIS DO FIM")])
    report = evaluate(monkeypatch, media_probe=probe(duration=30.0), subtitle=subs)
    assert report["status"] == NEEDS_REVIEW
    assert "subtitle_out_of_bounds" in report["reasons"]


def test_subtitle_before_zero_needs_review(monkeypatch, tmp_path):
    subs = write_ass(tmp_path / "s.ass", [(-2.0, 4.0, "ANTES")])
    report = evaluate(monkeypatch, media_probe=probe(duration=30.0), subtitle=subs)
    assert "subtitle_negative_timestamp" in report["reasons"]


def test_subtitle_ordering_is_checked(monkeypatch, tmp_path):
    subs = write_ass(tmp_path / "s.ass", [(10.0, 12.0, "SEGUNDO"), (2.0, 4.0, "PRIMEIRO")])
    report = evaluate(monkeypatch, media_probe=probe(duration=30.0), subtitle=subs)
    assert "subtitle_ordering_invalid" in report["reasons"]


def test_empty_subtitle_file_needs_review(monkeypatch, tmp_path):
    subs = write_ass(tmp_path / "s.ass", [])
    report = evaluate(monkeypatch, media_probe=probe(duration=30.0), subtitle=subs)
    assert "subtitle_file_empty" in report["reasons"]


def test_subtitles_are_never_blocking(monkeypatch, tmp_path):
    """A bad caption timeline is a readability defect, not a broken file."""
    subs = write_ass(tmp_path / "s.ass", [(-5.0, 99.0, "TUDO ERRADO")])
    report = evaluate(monkeypatch, media_probe=probe(duration=30.0), subtitle=subs)
    assert report["status"] == NEEDS_REVIEW
    assert report["blocking_reasons"] == []


def test_no_subtitles_expected_is_a_pass(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(), subtitle=None, expect_subtitles=False)
    assert report["checks"]["subtitle_timing"]["status"] == "pass"


# ------------------------------------------------------------- transitions


def test_a_transition_longer_than_its_clip_is_reported(monkeypatch):
    report = evaluate(
        monkeypatch,
        media_probe=probe(duration=2.0),
        render_plan=plan(clip_seconds=(2.0,), transition_ms=3000),
    )
    assert "transition_exceeds_clip" in report["reasons"]


def test_normal_transitions_pass(monkeypatch):
    report = evaluate(
        monkeypatch, media_probe=probe(), render_plan=plan(clip_seconds=(30.0,), transition_ms=320)
    )
    assert report["checks"]["transitions"]["status"] == "pass"


# ----------------------------------------------------------------- identity


def test_the_verdict_names_the_artifact_it_describes(monkeypatch):
    report = evaluate(
        monkeypatch,
        media_probe=probe(),
        artifact_id="job-7:final_clip_02",
        video_index=2,
        cut_ids=["v2-c1", "v2-c2"],
    )
    assert report["artifact_id"] == "job-7:final_clip_02"
    assert report["video_index"] == 2
    assert report["cut_ids"] == ["v2-c1", "v2-c2"]
    assert report["file_name"] == "final_clip_01.mp4"


def test_the_report_declares_its_scope(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe())
    assert report["qa_scope"] == "final_output"


def test_cold_open_is_recorded_in_the_report(monkeypatch):
    report = evaluate(
        monkeypatch, media_probe=probe(), render_plan=plan(clip_seconds=(30.0,), speed=1.15, cold_open=4.0)
    )
    assert report["cold_open"]["enabled"] is True
    assert report["cold_open"]["replays_source_span"] is True
    assert report["cold_open"]["timeline_duration_sec"] == pytest.approx(4.0 / 1.15, abs=0.01)


# -------------------------------------------------------------- aggregation


def test_one_blocked_artifact_blocks_the_set():
    reports = [
        {"status": AUTO_READY, "reasons": [], "blocking_reasons": []},
        {"status": BLOCKED, "reasons": ["audio_fully_silent"], "blocking_reasons": ["audio_fully_silent"]},
    ]
    summary = summarize(reports)
    assert summary["status"] == BLOCKED
    assert summary["summary"] == {
        "total_artifacts": 2,
        "auto_ready": 1,
        "needs_review": 1 - 1,
        "blocked": 1,
    }


def test_an_empty_set_is_blocked_not_ready():
    """Nothing evaluated is not the same as nothing wrong."""
    summary = summarize([])
    assert summary["status"] == BLOCKED
    assert summary["reasons"] == ["no_final_media_evaluated"]


def test_score_never_looks_healthy_while_the_file_is_broken(monkeypatch):
    report = evaluate(monkeypatch, media_probe=probe(video=False, audio=False))
    assert report["score"] < 45
    assert report["status"] == BLOCKED


# ------------------------------------------------------- retry classification


def test_deterministic_failures_are_marked_not_worth_retrying():
    assert classify_failures(["wrong_aspect_ratio"]) == "retry_will_not_help"
    assert classify_failures(["audio_stream_missing"]) == "retry_will_not_help"


def test_transient_failures_are_marked_retryable():
    assert classify_failures(["artifact_missing"]) == "retry_may_help"


def test_no_failure_needs_no_classification():
    assert classify_failures([]) == "not_applicable"


# ==========================================================================
# Log parsing: the shape ffmpeg actually emits
# ==========================================================================


def test_filter_output_is_parsed_from_a_level_tagged_log():
    log = "\n".join(
        [
            "[info] [blackdetect @ 0x1] black_start:0 black_end:5.96667 black_duration:5.96667",
            "[info] [silencedetect @ 0x2] silence_start: 12.5",
            "[info] [silencedetect @ 0x2] silence_end: 20.25 | silence_duration: 7.75",
            "[info] [Parsed_volumedetect_1 @ 0x3] n_samples: 240000",
            "[info] [Parsed_volumedetect_1 @ 0x3] mean_volume: -21.1 dB",
            "[info] [Parsed_volumedetect_1 @ 0x3] max_volume: -0.0 dB",
            "[info] [Parsed_volumedetect_1 @ 0x3] histogram_0db: 47774",
        ]
    )
    parsed = parse_analysis_log(log, duration_sec=30.0)
    assert parsed["black"] == [(0.0, 5.96667)]
    assert parsed["silence"] == [(12.5, 20.25)]
    assert parsed["max_volume"] == 0.0
    assert parsed["clipped_samples"] == 47774
    # n_samples, NOT the sum of printed buckets: ffmpeg prints only the significant ones.
    assert parsed["total_samples"] == 240000
    assert parsed["errors"] == []


def test_a_range_left_open_runs_to_the_end_of_the_file():
    log = "[info] [freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 4"
    parsed = parse_analysis_log(log, duration_sec=30.0)
    assert parsed["freeze"] == [(4.0, 30.0)]


def test_error_lines_are_separated_from_filter_output():
    log = "\n".join(
        [
            "[error] Invalid data found when processing input",
            "[info] [blackdetect @ 0x1] black_start:0 black_end:1.0 black_duration:1.0",
        ]
    )
    parsed = parse_analysis_log(log, duration_sec=10.0)
    assert parsed["errors"] == ["Invalid data found when processing input"]
    assert parsed["black"] == [(0.0, 1.0)]


# ==========================================================================
# Real files: the measurement layer against real ffmpeg output
# ==========================================================================


@needs_ffmpeg
def test_a_real_render_is_probed_and_passes(tmp_path):
    from evaluation.final_qa_fixtures import _ass, _make_video, _render_plan

    media = _make_video(tmp_path / "final.mp4", seconds=4, size="1080x1920")
    subs = _ass(tmp_path / "final.ass", [(0.2, 1.5, "UM"), (1.7, 3.5, "DOIS")])

    report = FinalMediaQA().evaluate(
        FinalMediaQAInput(
            final_file=media,
            artifact_id="real:final_clip_01",
            render_plan=_render_plan(clip_seconds=[4.0]),
            subtitle_path=subs,
        )
    )
    assert report["status"] == AUTO_READY, report["reasons"]
    assert report["probe"]["video"]["width"] == 1080
    assert report["probe"]["audio"]["codec"] == "aac"
    assert report["analysis"]["decode_ok"] is True


@needs_ffmpeg
def test_a_real_broken_file_blocks(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not an mp4 at all, just some bytes on disk here")

    report = FinalMediaQA().evaluate(
        FinalMediaQAInput(final_file=broken, artifact_id="real:broken", expect_subtitles=False)
    )
    assert report["status"] == BLOCKED
    assert "invalid_container" in report["blocking_reasons"]


@needs_ffmpeg
def test_a_real_silent_render_blocks(tmp_path):
    from evaluation.final_qa_fixtures import _make_video, _render_plan

    media = _make_video(
        tmp_path / "silent.mp4", seconds=4, size="1080x1920",
        audio="anullsrc=r=48000:cl=mono:duration={d}",
    )
    report = FinalMediaQA().evaluate(
        FinalMediaQAInput(
            final_file=media,
            artifact_id="real:silent",
            render_plan=_render_plan(clip_seconds=[4.0]),
            expect_subtitles=False,
        )
    )
    assert report["status"] == BLOCKED
    assert "audio_fully_silent" in report["blocking_reasons"]
