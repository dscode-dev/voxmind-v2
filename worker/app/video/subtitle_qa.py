"""Validates the subtitle artefact against the final video's timeline.

ClipFlow burns subtitles into the picture, so once rendering is done the only way to check
them would be OCR. But the ``.ass`` file that *produced* the burn is right there, and it is
deterministically checkable: if its last event ends after the video does, those words were
burned onto frames that do not exist.

This validates the artefact, not the wording. Whether a caption reads well is editorial.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

# `Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text`
_DIALOGUE = re.compile(r"^Dialogue:\s*(.*)$", re.IGNORECASE)
# A leading `-` cannot come out of our own builder (it clamps at zero), but a
# hand-edited or third-party file can carry one, and reporting it as "negative"
# is far more useful than reporting the whole file as unparseable.
_TIMESTAMP = re.compile(r"^(-?)(\d+):(\d{1,2}):(\d{1,2})[.,](\d{1,3})$")


@dataclass(frozen=True)
class SubtitleEvent:
    index: int
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SubtitleTimeline:
    path: Path | None = None
    present: bool = False
    parse_ok: bool = False
    error: str | None = None
    events: List[SubtitleEvent] = field(default_factory=list)

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def first_start(self) -> float | None:
        return min((event.start for event in self.events), default=None)

    @property
    def last_end(self) -> float | None:
        return max((event.end for event in self.events), default=None)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "file_name": self.path.name if self.path else None,
            "present": self.present,
            "parse_ok": self.parse_ok,
            "error": self.error,
            "event_count": self.event_count,
            "first_start_sec": round(self.first_start, 3) if self.first_start is not None else None,
            "last_end_sec": round(self.last_end, 3) if self.last_end is not None else None,
        }


@dataclass(frozen=True)
class SubtitleFinding:
    code: str
    detail: str
    event_index: int | None = None

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "event_index": self.event_index}


def parse_subtitle_file(path: Path | None) -> SubtitleTimeline:
    """Read an ``.ass`` (or ``.srt``) file into events. Never raises."""
    if path is None:
        return SubtitleTimeline(path=None, present=False)

    path = Path(path)
    if not path.exists():
        return SubtitleTimeline(path=path, present=False, error="subtitle_file_missing")

    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return SubtitleTimeline(path=path, present=True, error=f"subtitle_file_unreadable: {exc}")

    if path.suffix.lower() == ".srt":
        events, error = _parse_srt(body)
    else:
        events, error = _parse_ass(body)

    return SubtitleTimeline(
        path=path,
        present=True,
        parse_ok=error is None,
        error=error,
        events=events,
    )


def check_subtitle_timeline(
    timeline: SubtitleTimeline,
    *,
    video_duration_sec: float,
    tolerance_sec: float,
    expect_subtitles: bool = True,
) -> List[SubtitleFinding]:
    """Every invariant the burned-in subtitles must satisfy against the final video."""
    findings: List[SubtitleFinding] = []

    if not timeline.present:
        if expect_subtitles:
            findings.append(SubtitleFinding("subtitle_missing", timeline.error or "no subtitle artefact"))
        return findings

    if not timeline.parse_ok:
        findings.append(SubtitleFinding("subtitle_unparseable", timeline.error or "unknown parse failure"))
        return findings

    if not timeline.events:
        findings.append(SubtitleFinding("subtitle_file_empty", "the file parsed but declares no events"))
        return findings

    previous: SubtitleEvent | None = None
    for event in timeline.events:
        if event.start < 0:
            findings.append(
                SubtitleFinding("subtitle_negative_timestamp", f"start={event.start:.3f}s", event.index)
            )
        if event.end <= event.start:
            findings.append(
                SubtitleFinding(
                    "subtitle_impossible_range",
                    f"start={event.start:.3f}s end={event.end:.3f}s",
                    event.index,
                )
            )
        if previous is not None and event.start < previous.start:
            findings.append(
                SubtitleFinding(
                    "subtitle_ordering_invalid",
                    f"event {event.index} starts at {event.start:.3f}s, before event "
                    f"{previous.index} at {previous.start:.3f}s",
                    event.index,
                )
            )
        if video_duration_sec > 0 and event.end > video_duration_sec + tolerance_sec:
            findings.append(
                SubtitleFinding(
                    "subtitle_out_of_bounds",
                    f"ends at {event.end:.3f}s, video is {video_duration_sec:.3f}s "
                    f"(tolerance {tolerance_sec:.2f}s)",
                    event.index,
                )
            )
        previous = event

    return findings


def _parse_ass(body: str) -> tuple[List[SubtitleEvent], str | None]:
    events: List[SubtitleEvent] = []
    for line in body.splitlines():
        match = _DIALOGUE.match(line.strip())
        if not match:
            continue
        # Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
        fields = match.group(1).split(",", 9)
        if len(fields) < 10:
            return events, f"malformed Dialogue line: {line.strip()[:80]}"
        start = _parse_timestamp(fields[1])
        end = _parse_timestamp(fields[2])
        if start is None or end is None:
            return events, f"unparseable timestamp in: {line.strip()[:80]}"
        events.append(SubtitleEvent(index=len(events) + 1, start=start, end=end, text=fields[9].strip()))
    return events, None


def _parse_srt(body: str) -> tuple[List[SubtitleEvent], str | None]:
    events: List[SubtitleEvent] = []
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing = next((line for line in lines if "-->" in line), None)
        if timing is None:
            continue
        left, _, right = timing.partition("-->")
        start = _parse_timestamp(left.strip())
        end = _parse_timestamp(right.strip())
        if start is None or end is None:
            return events, f"unparseable timestamp in: {timing[:80]}"
        text = " ".join(line for line in lines if line is not timing and "-->" not in line)
        events.append(SubtitleEvent(index=len(events) + 1, start=start, end=end, text=text))
    return events, None


def _parse_timestamp(text: str) -> float | None:
    match = _TIMESTAMP.match(text.strip())
    if not match:
        return None
    sign, hours, minutes, seconds, fraction = match.groups()
    # ASS uses centiseconds, SRT milliseconds; scale by the digits actually present.
    fractional = int(fraction) / (10 ** len(fraction))
    total = int(hours) * 3600 + int(minutes) * 60 + int(seconds) + fractional
    return -total if sign else total
