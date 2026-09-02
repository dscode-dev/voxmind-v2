"""Measurement of a rendered media file. No judgement, no thresholds, no verdicts.

Everything here answers "what is actually in this file?". Whether the answer is acceptable
is decided in ``app/video/final_media_qa.py``. Keeping the two apart is what makes the gate
testable: a policy test can hand the evaluator a fabricated measurement without producing a
real corrupt MP4, and a measurement test can assert numbers without asserting verdicts.

Two external calls per file:

* one ``ffprobe`` for the container and stream declarations;
* one ``ffmpeg -f null -`` decode pass carrying every analysis filter at once
  (blackdetect, freezedetect, silencedetect, volumedetect).

The decode pass *is* the decode-integrity check, so the filters ride along at no extra cost.
Running them separately would decode the file four times over.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.runtime.subprocess_runner import (
    SubprocessError,
    SubprocessTimeout,
    run_command,
    truncate_stderr,
)
from app.settings import settings

# ffmpeg tags each line with its level under `-loglevel level+info`, which is the only
# reliable way to read filter output (info) and decode errors (error) from a single pass.
_LEVEL_LINE = re.compile(r"^\[(\w+)\]\s*(.*)$")
_BLACK = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")
_FREEZE_START = re.compile(r"freeze_start:\s*(-?[\d.]+)")
_FREEZE_END = re.compile(r"freeze_end:\s*(-?[\d.]+)")
_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB")
_MAX_VOLUME = re.compile(r"max_volume:\s*(-?[\d.]+|-inf)\s*dB")
_HISTOGRAM = re.compile(r"histogram_(\d+)db:\s*(\d+)")
_N_SAMPLES = re.compile(r"n_samples:\s*(\d+)")

Range = Tuple[float, float]


@dataclass(frozen=True)
class VideoStream:
    codec: str | None = None
    width: int = 0
    height: int = 0
    frame_rate: float = 0.0
    pixel_format: str | None = None
    bitrate_bps: int | None = None

    @property
    def aspect_ratio(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "frame_rate": round(self.frame_rate, 4),
            "pixel_format": self.pixel_format,
            "bitrate_bps": self.bitrate_bps,
            "aspect_ratio": round(self.aspect_ratio, 4),
        }


@dataclass(frozen=True)
class AudioStream:
    codec: str | None = None
    sample_rate: int = 0
    channels: int = 0
    bitrate_bps: int | None = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bitrate_bps": self.bitrate_bps,
        }


@dataclass(frozen=True)
class MediaProbe:
    """What ffprobe says the container holds."""

    path: Path
    exists: bool = False
    size_bytes: int = 0
    probe_ok: bool = False
    error: str | None = None
    format_name: str | None = None
    duration_sec: float = 0.0
    duration_declared: bool = False
    bitrate_bps: int | None = None
    video: VideoStream | None = None
    audio: AudioStream | None = None
    stream_count: int = 0

    @property
    def has_video(self) -> bool:
        return self.video is not None

    @property
    def has_audio(self) -> bool:
        return self.audio is not None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "file_name": self.path.name,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "probe_ok": self.probe_ok,
            "error": self.error,
            "format_name": self.format_name,
            "duration_sec": round(self.duration_sec, 3),
            "duration_declared": self.duration_declared,
            "bitrate_bps": self.bitrate_bps,
            "stream_count": self.stream_count,
            "video": self.video.as_dict() if self.video else None,
            "audio": self.audio.as_dict() if self.audio else None,
        }


@dataclass(frozen=True)
class MediaAnalysis:
    """What one decode pass observed while playing the file through."""

    decode_ok: bool = False
    decode_errors: str = ""
    timed_out: bool = False
    black_ranges: List[Range] = field(default_factory=list)
    freeze_ranges: List[Range] = field(default_factory=list)
    silence_ranges: List[Range] = field(default_factory=list)
    mean_volume_db: float | None = None
    max_volume_db: float | None = None
    clipped_samples: int = 0
    total_samples: int = 0
    audio_measured: bool = False
    duration_sec: float = 0.0

    @staticmethod
    def _total(ranges: List[Range]) -> float:
        return round(sum(max(0.0, end - start) for start, end in ranges), 3)

    @staticmethod
    def _longest(ranges: List[Range]) -> float:
        return round(max((max(0.0, end - start) for start, end in ranges), default=0.0), 3)

    @property
    def black_total_sec(self) -> float:
        return self._total(self.black_ranges)

    @property
    def black_ratio(self) -> float:
        return (self.black_total_sec / self.duration_sec) if self.duration_sec > 0 else 0.0

    @property
    def longest_freeze_sec(self) -> float:
        return self._longest(self.freeze_ranges)

    @property
    def silence_total_sec(self) -> float:
        return self._total(self.silence_ranges)

    @property
    def longest_silence_sec(self) -> float:
        return self._longest(self.silence_ranges)

    @property
    def silence_ratio(self) -> float:
        return (self.silence_total_sec / self.duration_sec) if self.duration_sec > 0 else 0.0

    @property
    def clipping_ratio(self) -> float:
        return (self.clipped_samples / self.total_samples) if self.total_samples else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "decode_ok": self.decode_ok,
            "decode_errors": self.decode_errors,
            "timed_out": self.timed_out,
            "black_total_sec": self.black_total_sec,
            "black_ratio": round(self.black_ratio, 4),
            "black_ranges": _rounded(self.black_ranges),
            "longest_freeze_sec": self.longest_freeze_sec,
            "freeze_ranges": _rounded(self.freeze_ranges),
            "silence_total_sec": self.silence_total_sec,
            "longest_silence_sec": self.longest_silence_sec,
            "silence_ratio": round(self.silence_ratio, 4),
            "silence_ranges": _rounded(self.silence_ranges),
            "mean_volume_db": _serialisable_db(self.mean_volume_db),
            "max_volume_db": _serialisable_db(self.max_volume_db),
            "clipped_samples": self.clipped_samples,
            "clipping_ratio": round(self.clipping_ratio, 6),
            "audio_measured": self.audio_measured,
        }


def probe_media(path: Path, *, job_id: str | None = None) -> MediaProbe:
    """Read the container. Never raises: an unreadable file is a *result*, not an exception.

    QA must be able to report "this file is broken". Letting ffprobe's failure propagate
    would turn a blockable artefact into a crashed job.
    """
    path = Path(path)
    if not path.exists():
        return MediaProbe(path=path, exists=False, error="file_not_found")

    size_bytes = path.stat().st_size
    if size_bytes == 0:
        return MediaProbe(path=path, exists=True, size_bytes=0, error="file_empty")

    command = [
        "ffprobe",
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(path),
    ]
    try:
        result = run_command(
            command,
            timeout=settings.ffprobe_timeout_sec,
            step="final_qa_probe",
            job_id=job_id,
            capture_stdout=True,
        )
        payload = json.loads((result.stdout or b"").decode("utf-8", errors="replace") or "{}")
    except SubprocessError as exc:
        detail = truncate_stderr(exc.stderr, 400) or str(exc)
        return MediaProbe(path=path, exists=True, size_bytes=size_bytes, error=f"ffprobe_failed: {detail}")
    except ValueError as exc:
        return MediaProbe(
            path=path, exists=True, size_bytes=size_bytes,
            error=f"ffprobe_output_unparseable: {exc}",
        )

    streams = payload.get("streams") or []
    container = payload.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    raw_duration = container.get("duration")

    return MediaProbe(
        path=path,
        exists=True,
        size_bytes=size_bytes,
        probe_ok=True,
        format_name=container.get("format_name"),
        duration_sec=_as_float(raw_duration),
        duration_declared=_is_finite_number(raw_duration),
        bitrate_bps=_as_int(container.get("bit_rate")),
        stream_count=len(streams),
        video=_video_stream(video) if video else None,
        audio=_audio_stream(audio) if audio else None,
    )


def analyze_media(
    path: Path,
    *,
    duration_sec: float,
    silence_noise_db: float,
    silence_min_sec: float,
    black_min_sec: float,
    black_pixel_threshold: float,
    freeze_min_sec: float,
    freeze_noise_db: float,
    timeout_sec: float,
    has_audio: bool = True,
    job_id: str | None = None,
) -> MediaAnalysis:
    """Decode the whole file once, with every analysis filter attached.

    ``duration_sec`` closes ranges that ffmpeg opens but never terminates — a freeze or a
    silence that runs to the end of the file. The log alone does not say where the file ends.
    """
    path = Path(path)
    video_filters = (
        f"blackdetect=d={black_min_sec}:pic_th={black_pixel_threshold}"
        f",freezedetect=n={freeze_noise_db}dB:d={freeze_min_sec}"
    )
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-loglevel", "level+info",
        "-i", str(path),
        "-vf", video_filters,
    ]
    if has_audio:
        command += [
            "-af",
            f"silencedetect=noise={silence_noise_db}dB:d={silence_min_sec},volumedetect",
        ]
    command += ["-f", "null", "-"]

    try:
        completed = run_command(
            command,
            timeout=timeout_sec,
            step="final_qa_decode",
            job_id=job_id,
            check=False,
        )
    except SubprocessTimeout as exc:
        return MediaAnalysis(
            decode_ok=False,
            timed_out=True,
            decode_errors=truncate_stderr(exc.stderr, 800),
            duration_sec=duration_sec,
        )
    except SubprocessError as exc:
        return MediaAnalysis(
            decode_ok=False,
            decode_errors=truncate_stderr(exc.stderr, 800),
            duration_sec=duration_sec,
        )

    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    parsed = parse_analysis_log(stderr, duration_sec=duration_sec)

    return MediaAnalysis(
        decode_ok=completed.returncode == 0 and not parsed["errors"],
        decode_errors="\n".join(parsed["errors"])[-800:],
        black_ranges=parsed["black"],
        freeze_ranges=parsed["freeze"],
        silence_ranges=parsed["silence"],
        mean_volume_db=parsed["mean_volume"],
        max_volume_db=parsed["max_volume"],
        clipped_samples=parsed["clipped_samples"],
        total_samples=parsed["total_samples"],
        audio_measured=has_audio and parsed["max_volume"] is not None,
        duration_sec=duration_sec,
    )


def parse_analysis_log(stderr: str, *, duration_sec: float = 0.0) -> Dict[str, Any]:
    """Extract filter output and decode errors from a level-tagged ffmpeg log."""
    black: List[Range] = []
    freeze: List[Range] = []
    silence: List[Range] = []
    errors: List[str] = []
    open_freeze: float | None = None
    open_silence: float | None = None
    mean_volume: float | None = None
    max_volume: float | None = None
    histogram: Dict[int, int] = {}
    n_samples = 0

    for raw_line in stderr.splitlines():
        match = _LEVEL_LINE.match(raw_line.strip())
        if match:
            level, body = match.group(1), match.group(2)
        else:
            level, body = "info", raw_line

        if level in {"error", "fatal", "panic"}:
            errors.append(body.strip())
            continue

        found = _BLACK.search(body)
        if found:
            black.append((float(found.group(1)), float(found.group(2))))

        found = _FREEZE_START.search(body)
        if found:
            open_freeze = float(found.group(1))
        found = _FREEZE_END.search(body)
        if found and open_freeze is not None:
            freeze.append((open_freeze, float(found.group(1))))
            open_freeze = None

        found = _SILENCE_START.search(body)
        if found:
            open_silence = float(found.group(1))
        found = _SILENCE_END.search(body)
        if found and open_silence is not None:
            silence.append((open_silence, float(found.group(1))))
            open_silence = None

        found = _MEAN_VOLUME.search(body)
        if found:
            mean_volume = _db_value(found.group(1))
        found = _MAX_VOLUME.search(body)
        if found:
            max_volume = _db_value(found.group(1))
        found = _HISTOGRAM.search(body)
        if found:
            histogram[int(found.group(1))] = int(found.group(2))
        found = _N_SAMPLES.search(body)
        if found:
            n_samples = int(found.group(1))

    # A range still open at the end of the log runs to the end of the file.
    if open_freeze is not None and duration_sec > open_freeze:
        freeze.append((open_freeze, duration_sec))
    if open_silence is not None and duration_sec > open_silence:
        silence.append((open_silence, duration_sec))

    return {
        "black": black,
        "freeze": freeze,
        "silence": silence,
        "errors": errors,
        "mean_volume": mean_volume,
        "max_volume": max_volume,
        # volumedetect buckets samples by distance from full scale; bucket 0 holds the ones
        # sitting on the ceiling. The denominator has to be `n_samples`, NOT the sum of the
        # buckets: ffmpeg prints only the most significant buckets, so summing them gave a
        # ratio of exactly 1.0 for any file whose loudest bucket was the only one printed.
        "clipped_samples": histogram.get(0, 0),
        "total_samples": n_samples,
    }


def _video_stream(stream: Dict[str, Any]) -> VideoStream:
    return VideoStream(
        codec=stream.get("codec_name"),
        width=_as_int(stream.get("width")) or 0,
        height=_as_int(stream.get("height")) or 0,
        frame_rate=_frame_rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
        pixel_format=stream.get("pix_fmt"),
        bitrate_bps=_as_int(stream.get("bit_rate")),
    )


def _audio_stream(stream: Dict[str, Any]) -> AudioStream:
    return AudioStream(
        codec=stream.get("codec_name"),
        sample_rate=_as_int(stream.get("sample_rate")) or 0,
        channels=_as_int(stream.get("channels")) or 0,
        bitrate_bps=_as_int(stream.get("bit_rate")),
    )


def _frame_rate(value: Any) -> float:
    text = str(value or "").strip()
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            denom = float(denominator)
        except (TypeError, ValueError):
            return 0.0
        if denom == 0:
            return 0.0
        try:
            return float(numerator) / denom
        except (TypeError, ValueError):
            return 0.0
    return _as_float(text)


def _rounded(ranges: List[Range]) -> List[List[float]]:
    return [[round(start, 3), round(end, 3)] for start, end in ranges]


def _serialisable_db(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, 2)


def _db_value(text: str) -> float:
    return float("-inf") if text.strip() == "-inf" else float(text)


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _as_float(value: Any, *, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
