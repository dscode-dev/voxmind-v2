"""ASR window planning with explicit, absolute offsets.

The previous planner produced adjacent windows with **no overlap**:

    window 0: [0, 900)   window 1: [900, 1800)   window 2: [1800, 2700)

A sentence spoken across t=900 was cut in half by the extraction itself. Whisper then
transcribed each half without the other's context, which routinely truncates or hallucinates
the words at the edge. A 90-minute video has five such seams.

Windows now overlap by a configurable amount, and each window states exactly which region is
shared with its predecessor, so the reconciler can tell a duplicate from a genuine repeat:

    window 0: [0, 900)      overlap_sec=0
    window 1: [895, 1795)   overlap_sec=5   (shared with window 0: 895..900)
    window 2: [1790, 2690)  overlap_sec=5   (shared with window 1: 1790..1795)

A window's shared region is always ``[start, start + overlap_sec]`` — it begins at the
window's own left edge, because that is exactly the span its predecessor already covered.

Timestamp contract
------------------
``AsrWindow.start`` / ``.end`` are **video-relative** (absolute) seconds. Whisper returns
**window-relative** times for the extracted audio file; ``AsrWindow.to_absolute`` is the only
sanctioned conversion, so the two frames of reference never mix implicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class AsrWindow:
    """One extraction window, in absolute (video-relative) seconds."""

    index: int
    start: float
    end: float
    #: Seconds shared with the PREVIOUS window. 0 for the first window.
    overlap_sec: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def overlap_duration(self) -> float:
        """Seconds this window shares with its predecessor. 0 for the first window."""
        return max(0.0, min(self.overlap_sec, self.duration))

    @property
    def overlap_start(self) -> float:
        """Absolute time where the shared region begins: this window's own left edge."""
        return self.start

    @property
    def overlap_end(self) -> float:
        """Absolute time where the shared region ends (== the previous window's end)."""
        return self.start + self.overlap_duration

    @property
    def is_first(self) -> bool:
        return self.index == 0

    def to_absolute(self, window_relative_sec: float) -> float:
        """Convert a Whisper timestamp for this window's audio into video time."""
        return float(window_relative_sec) + self.start

    def touches_start(self, timestamp: float, tolerance: float = 0.05) -> bool:
        """True when a segment begins at this window's left edge — i.e. Whisper had no
        preceding audio and the segment is probably truncated."""
        return timestamp <= self.start + tolerance

    def touches_end(self, timestamp: float, tolerance: float = 0.05) -> bool:
        """True when a segment ends at this window's right edge — probably truncated."""
        return timestamp >= self.end - tolerance

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "overlap_sec": round(self.overlap_sec, 3),
            "overlap_end": round(self.overlap_end, 3),
            "duration": round(self.duration, 3),
            "overlap_duration": round(self.overlap_duration, 3),
        }


def build_windows(
    duration_sec: float,
    window_sec: float,
    overlap_sec: float = 0.0,
) -> List[AsrWindow]:
    """Plan overlapping windows covering [0, duration_sec).

    Each window after the first starts ``overlap_sec`` earlier than the previous one ended,
    so the shared region is transcribed twice with full context on at least one side.

    The advance per window is ``window_sec - overlap_sec``; overlap is clamped below
    ``window_sec`` so progress is always positive.
    """
    duration_sec = max(0.0, float(duration_sec))
    window_sec = float(window_sec)
    if window_sec <= 0:
        raise ValueError("window_sec must be positive")

    overlap_sec = max(0.0, float(overlap_sec))
    if overlap_sec >= window_sec:
        # Never let overlap stall or reverse the scan.
        overlap_sec = window_sec / 2.0

    if duration_sec <= 0:
        return []

    advance = window_sec - overlap_sec
    windows: List[AsrWindow] = []
    index = 0
    start = 0.0

    while start < duration_sec:
        end = min(start + window_sec, duration_sec)
        shared = 0.0 if index == 0 else min(overlap_sec, end - start)
        windows.append(
            AsrWindow(index=index, start=start, end=end, overlap_sec=shared)
        )
        if end >= duration_sec:
            break
        start += advance
        index += 1

    return windows


def total_processed_seconds(windows: List[AsrWindow]) -> float:
    """Audio actually sent to the model, including the duplicated overlap."""
    return sum(window.duration for window in windows)


def overlap_overhead(windows: List[AsrWindow], duration_sec: float) -> dict:
    """The real cost of overlapping, for the performance report."""
    processed = total_processed_seconds(windows)
    overlap_total = sum(window.overlap_duration for window in windows)
    return {
        "audio_duration_sec": round(float(duration_sec), 3),
        "window_count": len(windows),
        "overlap_seconds_total": round(overlap_total, 3),
        "effective_audio_processed_sec": round(processed, 3),
        "overhead_ratio": round(processed / duration_sec, 4) if duration_sec > 0 else 0.0,
    }
