"""Final-media fixtures: real MP4 files, generated with ffmpeg at run time.

These are deliberately *real* files rather than fabricated probe dictionaries. The gate's job
is to catch what ffmpeg actually produces, and a fixture built from a Python dict would only
prove the policy agrees with itself. A 3-second 1080x1920 clip costs well under a second to
generate, so nothing large is versioned: ``build_fixtures`` writes them into a temporary
directory and the caller deletes it.

Each case declares what a correct gate must conclude. ``expected_status`` is the verdict;
``expected_codes`` are reason codes that must appear. A case that a gate cannot detect at all
is what the BEFORE arm looks like — see ``evaluation/final_qa_report.py``.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

PORTRAIT = "1080x1920"
LANDSCAPE = "1920x1080"


@dataclass
class FinalMediaCase:
    case_id: str
    description: str
    expected_status: str
    expected_codes: List[str] = field(default_factory=list)
    video_ratio: str = "portrait"
    expect_audio: bool = True
    expect_subtitles: bool = True
    # Filled by build_fixtures.
    path: Path | None = None
    render_plan: Dict[str, Any] | None = None
    subtitle_path: Path | None = None
    source_type: str = "synthetic"


def _run(command: List[str]) -> None:
    subprocess.run(command, check=True, capture_output=True)


def _render_plan(
    *,
    clip_seconds: List[float],
    playback_speed: float = 1.0,
    cold_open_sec: float = 0.0,
    transition_ms: int = 0,
    video_ratio: str = "portrait",
) -> Dict[str, Any]:
    clips: List[Dict[str, Any]] = []
    cursor = 0.0
    for index, seconds in enumerate(clip_seconds, start=1):
        clip: Dict[str, Any] = {
            "clip_index": index,
            "source_start": cursor,
            "source_end": cursor + seconds,
            "safe_start": cursor,
            "safe_end": cursor + seconds,
            "duration": seconds,
            "video_ratio": video_ratio,
            "playback_speed": playback_speed,
            "transition_after": "fade" if transition_ms else "hard_cut",
            "transition_duration_ms": transition_ms,
            "cold_open": {"enabled": False},
        }
        if index == 1 and cold_open_sec > 0:
            clip["cold_open"] = {
                "enabled": True,
                "source_clip_index": 1,
                "duration_sec": cold_open_sec,
                "relative_start_sec": 0.0,
                "transition_after": "fade",
                "transition_duration_ms": 90,
            }
        clips.append(clip)
        cursor += seconds
    return {
        "job_id": "final-qa-fixture",
        "video_ratio": video_ratio,
        "playback_speed": playback_speed,
        "clips": clips,
    }


def _ass(path: Path, events: List[tuple[float, float, str]]) -> Path:
    def stamp(seconds: float) -> str:
        sign = "-" if seconds < 0 else ""
        total_cs = int(round(abs(seconds) * 100))
        return (
            f"{sign}{total_cs // 360000}:{(total_cs % 360000) // 6000:02}:"
            f"{(total_cs % 6000) // 100:02}.{total_cs % 100:02}"
        )

    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        "Style: VoxMind,DejaVu Sans,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "1,0,0,0,100,100,1.2,0,3,1,0,2,80,80,190,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    lines = [header.rstrip()]
    for start, end, text in events:
        lines.append(f"Dialogue: 0,{stamp(start)},{stamp(end)},VoxMind,,0,0,0,,{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_video(
    path: Path,
    *,
    seconds: float,
    size: str = PORTRAIT,
    audio: str | None = "sine=frequency=400:duration={d}",
    video: str = "testsrc2",
) -> Path:
    """`audio` is a lavfi spec with `{d}` where the duration goes.

    A template rather than an appended `:duration=`, because a spec like
    `sine=...,volume=12` is a filter *chain* and the duration belongs on the source, not on
    the last link.
    """
    # `testsrc2` takes its first option with `=`; `color=c=black` already has one, so the
    # next option must be separated with `:`.
    separator = ":" if "=" in video else "="
    command = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"{video}{separator}size={size}:duration={seconds}:rate=30",
    ]
    if audio is not None:
        command += ["-f", "lavfi", "-i", audio.format(d=seconds)]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio is not None:
        command += ["-c:a", "aac", "-ar", "48000"]
    command += ["-t", str(seconds), "-shortest", str(path)]
    _run(command)
    return path


CASES: List[FinalMediaCase] = [
    FinalMediaCase(
        case_id="valid_portrait",
        description="A well-formed 9:16 render: video, audio, subtitles inside the duration.",
        expected_status="auto_ready",
    ),
    FinalMediaCase(
        case_id="valid_landscape",
        description="A well-formed 16:9 render.",
        expected_status="auto_ready",
        video_ratio="landscape",
    ),
    FinalMediaCase(
        case_id="missing_file",
        description="The render step reported success but produced no file.",
        expected_status="blocked",
        expected_codes=["artifact_missing"],
    ),
    FinalMediaCase(
        case_id="zero_byte_file",
        description="ffmpeg created the output then died before writing anything.",
        expected_status="blocked",
        expected_codes=["artifact_empty"],
    ),
    FinalMediaCase(
        case_id="invalid_container",
        description="Bytes on disk that are not a media file at all.",
        expected_status="blocked",
        expected_codes=["invalid_container"],
    ),
    FinalMediaCase(
        case_id="truncated_container",
        description="A real MP4 cut off mid-stream: opens, then fails to decode.",
        expected_status="blocked",
        expected_codes=["invalid_container", "decode_error"],
    ),
    FinalMediaCase(
        case_id="missing_audio",
        description="The mix stage dropped the audio; the video is otherwise fine.",
        expected_status="blocked",
        expected_codes=["audio_stream_missing"],
    ),
    FinalMediaCase(
        case_id="wrong_aspect_ratio",
        description="A 16:9 file produced for a portrait contract.",
        expected_status="blocked",
        expected_codes=["wrong_aspect_ratio"],
    ),
    FinalMediaCase(
        case_id="duration_mismatch",
        description="The plan describes 30s; the renderer emitted 12s.",
        expected_status="blocked",
        expected_codes=["duration_mismatch_severe"],
    ),
    FinalMediaCase(
        case_id="long_silence",
        description="Audible for 8s, then silent to the end - the shape of the "
                    "hardcoded 28s soundtrack fade on a longer video.",
        expected_status="needs_review",
        expected_codes=["audio_long_silence"],
    ),
    FinalMediaCase(
        case_id="fully_silent_audio",
        description="An audio stream exists but carries no signal at all.",
        expected_status="blocked",
        expected_codes=["audio_fully_silent"],
    ),
    FinalMediaCase(
        case_id="audio_clipping",
        description="The mix was driven well past full scale.",
        expected_status="needs_review",
        expected_codes=["audio_peak_clipping"],
    ),
    FinalMediaCase(
        case_id="severe_clipping",
        description="A fully flat-topped waveform: at the ceiling for essentially every sample.",
        expected_status="blocked",
        expected_codes=["audio_severe_clipping"],
    ),
    FinalMediaCase(
        case_id="decode_error",
        description="The container opens and declares valid streams, but the picture data "
                    "is corrupt - only a decode pass finds it.",
        expected_status="blocked",
        expected_codes=["decode_error"],
    ),
    FinalMediaCase(
        case_id="black_video",
        description="The picture is black for the whole running time.",
        expected_status="blocked",
        expected_codes=["video_mostly_black"],
    ),
    FinalMediaCase(
        case_id="frozen_video",
        description="A single frozen frame held for the whole clip.",
        expected_status="needs_review",
        expected_codes=["video_frozen"],
    ),
    FinalMediaCase(
        case_id="subtitle_beyond_duration",
        description="Captions are timed past the end of the video they were burned into.",
        expected_status="needs_review",
        expected_codes=["subtitle_out_of_bounds"],
    ),
    FinalMediaCase(
        case_id="subtitle_negative_start",
        description="A caption starts before the video does.",
        expected_status="needs_review",
        expected_codes=["subtitle_negative_timestamp"],
    ),
    FinalMediaCase(
        case_id="subtitle_empty_file",
        description="A subtitle file that parses but declares no events.",
        expected_status="needs_review",
        expected_codes=["subtitle_file_empty"],
    ),
]


def build_fixtures(root: Path) -> List[FinalMediaCase]:
    """Materialise every case under ``root``. Requires ffmpeg."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    builders: Dict[str, Callable[[FinalMediaCase], None]] = {
        "valid_portrait": _build_valid_portrait,
        "valid_landscape": _build_valid_landscape,
        "missing_file": _build_missing_file,
        "zero_byte_file": _build_zero_byte,
        "invalid_container": _build_invalid_container,
        "truncated_container": _build_truncated,
        "missing_audio": _build_missing_audio,
        "wrong_aspect_ratio": _build_wrong_aspect,
        "duration_mismatch": _build_duration_mismatch,
        "long_silence": _build_long_silence,
        "fully_silent_audio": _build_fully_silent,
        "audio_clipping": _build_clipping,
        "severe_clipping": _build_severe_clipping,
        "decode_error": _build_decode_error,
        "black_video": _build_black,
        "frozen_video": _build_frozen,
        "subtitle_beyond_duration": _build_subtitle_beyond,
        "subtitle_negative_start": _build_subtitle_negative,
        "subtitle_empty_file": _build_subtitle_empty,
    }

    built: List[FinalMediaCase] = []
    for case in CASES:
        prepared = FinalMediaCase(**{**case.__dict__})
        prepared.path = root / f"{case.case_id}.mp4"
        prepared.subtitle_path = root / f"{case.case_id}.ass"
        builders[case.case_id](prepared)
        built.append(prepared)
    return built


# ------------------------------------------------------------------------ builders
# Each builder produces the file AND the plan describing what the renderer intended, so the
# gate compares a real artefact against a real expectation.

_DEFAULT_SUBS = [(0.5, 2.0, "PRIMEIRA LINHA"), (2.2, 5.5, "SEGUNDA LINHA")]


def _build_valid_portrait(case: FinalMediaCase) -> None:
    _make_video(case.path, seconds=6, size=PORTRAIT)
    case.render_plan = _render_plan(clip_seconds=[6.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_valid_landscape(case: FinalMediaCase) -> None:
    _make_video(case.path, seconds=6, size=LANDSCAPE)
    case.render_plan = _render_plan(clip_seconds=[6.0], video_ratio="landscape")
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_missing_file(case: FinalMediaCase) -> None:
    case.path.unlink(missing_ok=True)
    case.render_plan = _render_plan(clip_seconds=[6.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_zero_byte(case: FinalMediaCase) -> None:
    case.path.write_bytes(b"")
    case.render_plan = _render_plan(clip_seconds=[6.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_invalid_container(case: FinalMediaCase) -> None:
    case.path.write_bytes(b"this is not an mp4, it is 64 bytes of nonsense on disk 012345678")
    case.render_plan = _render_plan(clip_seconds=[6.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_truncated(case: FinalMediaCase) -> None:
    intact = case.path.with_name("_truncated_source.mp4")
    _make_video(intact, seconds=6, size=PORTRAIT)
    # Keep the first third: enough to look like a file, not enough to decode.
    case.path.write_bytes(intact.read_bytes()[: max(2048, intact.stat().st_size // 3)])
    intact.unlink(missing_ok=True)
    case.render_plan = _render_plan(clip_seconds=[6.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_missing_audio(case: FinalMediaCase) -> None:
    _make_video(case.path, seconds=6, size=PORTRAIT, audio=None)
    case.render_plan = _render_plan(clip_seconds=[6.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_wrong_aspect(case: FinalMediaCase) -> None:
    _make_video(case.path, seconds=6, size=LANDSCAPE)
    case.render_plan = _render_plan(clip_seconds=[6.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_duration_mismatch(case: FinalMediaCase) -> None:
    _make_video(case.path, seconds=12, size=PORTRAIT)
    # The plan says two 15s clips; the file is 12s. Content was lost.
    case.render_plan = _render_plan(clip_seconds=[15.0, 15.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_long_silence(case: FinalMediaCase) -> None:
    """Audible for 20s of 30s, then silent.

    Deliberately *moderate*: a 33% silence ratio with a 10s silent tail. It exceeds the 8s
    long-silence limit but stays under the 50% ratio that blocks, so it exercises the review
    band rather than collapsing into the same verdict as `fully_silent_audio`. This is the
    shape the hardcoded 28s soundtrack fade produced on longer renders.
    """
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={PORTRAIT}:duration=30:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=400:duration=20",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-af", "apad=whole_dur=30",
        "-c:a", "aac", "-ar", "48000", "-t", "30", str(case.path),
    ])
    case.render_plan = _render_plan(clip_seconds=[30.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_fully_silent(case: FinalMediaCase) -> None:
    _make_video(case.path, seconds=8, size=PORTRAIT, audio="anullsrc=r=48000:cl=mono:duration={d}")
    case.render_plan = _render_plan(clip_seconds=[8.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_clipping(case: FinalMediaCase) -> None:
    """A tone driven past the ceiling.

    `sine` leaves lavfi around -18 dBFS, so x12 lands the peak on 0.0 dB. Measured on this
    build: the same tone at x8 peaks at -0.3 dB and passes, at x12 it reads 0.0 dB and is
    flagged. Not a square wave - that is the severe case, and it would test the block band
    rather than this one.
    """
    _make_video(case.path, seconds=8, size=PORTRAIT, audio="sine=frequency=440:duration={d},volume=12")
    case.render_plan = _render_plan(clip_seconds=[8.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_severe_clipping(case: FinalMediaCase) -> None:
    """A fully flat-topped waveform: at the ceiling for essentially every sample."""
    _make_video(
        case.path, seconds=8, size=PORTRAIT,
        audio="aevalsrc=0.999*sgn(sin(2*PI*440*t)):s=48000:d={d}",
    )
    case.render_plan = _render_plan(clip_seconds=[8.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_decode_error(case: FinalMediaCase) -> None:
    """A container that opens but whose picture data is corrupt.

    Distinct from `truncated_container`: `+faststart` puts the moov atom at the front, so
    ffprobe reads the streams happily and only the decode pass discovers the damage. This is
    the case that justifies decoding the file rather than trusting the probe.
    """
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={PORTRAIT}:duration=8:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=400:duration=8",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-movflags", "+faststart",
        "-t", "8", str(case.path),
    ])
    data = bytearray(case.path.read_bytes())
    # Overwrite the back half of the payload, well past the header atoms.
    start = len(data) // 2
    data[start:] = bytes((index * 37 + 11) % 256 for index in range(len(data) - start))
    case.path.write_bytes(bytes(data))
    case.render_plan = _render_plan(clip_seconds=[8.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_black(case: FinalMediaCase) -> None:
    _make_video(case.path, seconds=8, size=PORTRAIT, video="color=c=black")
    case.render_plan = _render_plan(clip_seconds=[8.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_frozen(case: FinalMediaCase) -> None:
    # A static non-black picture: decodes fine, never changes.
    _make_video(case.path, seconds=10, size=PORTRAIT, video="color=c=darkgreen")
    case.render_plan = _render_plan(clip_seconds=[10.0])
    _ass(case.subtitle_path, _DEFAULT_SUBS)


def _build_subtitle_beyond(case: FinalMediaCase) -> None:
    _make_video(case.path, seconds=6, size=PORTRAIT)
    case.render_plan = _render_plan(clip_seconds=[6.0])
    _ass(case.subtitle_path, [(0.5, 2.0, "DENTRO"), (4.0, 11.5, "ALEM DO FIM")])


def _build_subtitle_negative(case: FinalMediaCase) -> None:
    _make_video(case.path, seconds=6, size=PORTRAIT)
    case.render_plan = _render_plan(clip_seconds=[6.0])
    _ass(case.subtitle_path, [(-1.5, 2.0, "ANTES DO INICIO"), (2.2, 5.0, "NORMAL")])


def _build_subtitle_empty(case: FinalMediaCase) -> None:
    _make_video(case.path, seconds=6, size=PORTRAIT)
    case.render_plan = _render_plan(clip_seconds=[6.0])
    _ass(case.subtitle_path, [])
