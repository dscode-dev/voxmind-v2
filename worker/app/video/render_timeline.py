"""Derives the duration the renderer is *expected* to produce, from the render plan.

Comparing a final MP4 against ``sum(source_cut_duration)`` is wrong, and wrong by a large
margin: the renderer speeds playback up (1.15x by default) and prepends a cold open. On a
40s two-cut sequence with a 4s cold open the naive comparison is off by 1.6s while the
modelled one is off by 0.14s — the naive check would have to run a tolerance so wide that a
genuinely truncated render would slip through it.

What each renderer stage does to duration, read off ``app/video/final_renderer.py``:

* **speed-up** — ``setpts=PTS/s`` and ``atempo=s`` divide each clip by the playback speed;
* **cold open** — prepends a teaser cut from the *prepared* (already sped-up) first clip,
  so it adds ``duration_sec / speed`` seconds and replays that span again afterwards;
* **transitions** — implemented as ``fade``/``afade`` *inside* each clip, not as crossfades
  between clips, so they are duration-neutral. Nothing is consumed at the join;
* **concat** — sums the parts;
* **soundtrack mix** — ``amix=duration=first`` over the programme audio: duration-neutral;
* **subtitle burn** — a video filter: duration-neutral.

So: ``expected = cold_open/speed + sum(cut_duration)/speed``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class ExpectedTimeline:
    """The duration model, with every term kept visible.

    A single number would make a mismatch undiagnosable — the breakdown says which stage the
    discrepancy could have come from.
    """

    expected_duration_sec: float
    source_duration_sec: float
    playback_speed: float
    cold_open_sec: float
    clip_count: int
    cold_open_enabled: bool
    transitions_duration_neutral: bool = True
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "expected_duration_sec": round(self.expected_duration_sec, 3),
            "source_duration_sec": round(self.source_duration_sec, 3),
            "playback_speed": round(self.playback_speed, 4),
            "cold_open_sec": round(self.cold_open_sec, 3),
            "cold_open_enabled": self.cold_open_enabled,
            "clip_count": self.clip_count,
            "transitions_duration_neutral": self.transitions_duration_neutral,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class TransitionIssue:
    clip_index: int
    code: str
    detail: str

    def as_dict(self) -> Dict[str, Any]:
        return {"clip_index": self.clip_index, "code": self.code, "detail": self.detail}


def expected_final_duration(render_plan: Dict[str, Any] | None) -> ExpectedTimeline:
    """Model the final duration from the plan the renderer was handed."""
    plan = render_plan or {}
    clips = [clip for clip in (plan.get("clips") or []) if isinstance(clip, dict)]
    notes: List[str] = []

    plan_speed = _positive_float(plan.get("playback_speed"), default=1.0)
    source_total = 0.0
    for clip in clips:
        source_total += _clip_source_duration(clip)

    # Each clip may override the plan-wide speed, so accumulate per clip rather than
    # dividing the total once.
    played_total = 0.0
    speeds: set[float] = set()
    for clip in clips:
        speed = _positive_float(clip.get("playback_speed"), default=plan_speed)
        speeds.add(round(speed, 4))
        played_total += _clip_source_duration(clip) / speed

    if len(speeds) > 1:
        notes.append("clips_use_different_playback_speeds")

    cold_open_sec, cold_open_enabled = _cold_open_timeline_seconds(clips, plan_speed)
    if cold_open_enabled:
        notes.append("cold_open_replays_a_span_of_the_source_clip")

    return ExpectedTimeline(
        expected_duration_sec=max(0.0, played_total + cold_open_sec),
        source_duration_sec=round(source_total, 3),
        playback_speed=plan_speed,
        cold_open_sec=cold_open_sec,
        clip_count=len(clips),
        cold_open_enabled=cold_open_enabled,
        notes=notes,
    )


def cold_open_metadata(render_plan: Dict[str, Any] | None) -> Dict[str, Any]:
    """Summarise the cold open for the render metadata.

    The hook being heard twice is a deliberate editorial decision (see ``_prepend_cold_open``
    in the renderer). Recording it here means a reviewer can tell an intended replay from an
    accidental duplicate without reverse-engineering the plan.
    """
    plan = render_plan or {}
    clips = [clip for clip in (plan.get("clips") or []) if isinstance(clip, dict)]
    first = clips[0] if clips else {}
    cold_open = first.get("cold_open") or {}
    speed = _positive_float(first.get("playback_speed"), default=_positive_float(plan.get("playback_speed"), default=1.0))

    if not cold_open.get("enabled"):
        return {"enabled": False}

    source_duration = max(0.0, _float(cold_open.get("duration_sec")))
    return {
        "enabled": True,
        "source_clip_index": int(cold_open.get("source_clip_index", 1) or 1),
        "source_duration_sec": round(source_duration, 3),
        "timeline_duration_sec": round(source_duration / speed, 3),
        "relative_start_sec": round(max(0.0, _float(cold_open.get("relative_start_sec"))), 3),
        "playback_speed": round(speed, 4),
        # The teaser span plays once as the opening and again in its own clip. Intentional.
        "replays_source_span": True,
    }


def transition_issues(render_plan: Dict[str, Any] | None) -> List[TransitionIssue]:
    """Technical integrity of the transition plan only — not whether it looks good.

    Fades are applied inside a clip, so a fade longer than the clip would consume the whole
    segment. The renderer refuses to apply one in that case, which protects the file but
    leaves the plan silently unhonoured; QA reports it either way.
    """
    plan = render_plan or {}
    issues: List[TransitionIssue] = []
    clips = [clip for clip in (plan.get("clips") or []) if isinstance(clip, dict)]
    plan_speed = _positive_float(plan.get("playback_speed"), default=1.0)

    for position, clip in enumerate(clips, start=1):
        index = int(clip.get("clip_index", position) or position)
        speed = _positive_float(clip.get("playback_speed"), default=plan_speed)
        played = _clip_source_duration(clip) / speed
        fade_sec = max(0.0, _float(clip.get("transition_duration_ms")) / 1000.0)

        if played <= 0:
            issues.append(TransitionIssue(index, "clip_non_positive_duration", f"duration={played:.3f}s"))
            continue
        if fade_sec <= 0:
            continue
        if fade_sec >= played:
            issues.append(
                TransitionIssue(
                    index,
                    "transition_exceeds_clip",
                    f"fade={fade_sec:.3f}s >= clip={played:.3f}s",
                )
            )
        elif fade_sec * 2 > played:
            issues.append(
                TransitionIssue(
                    index,
                    "transition_dominates_clip",
                    f"fades={fade_sec * 2:.3f}s of clip={played:.3f}s",
                )
            )

    return issues


def _cold_open_timeline_seconds(clips: List[Dict[str, Any]], plan_speed: float) -> tuple[float, bool]:
    if not clips:
        return 0.0, False

    first = clips[0]
    cold_open = first.get("cold_open") or {}
    if not cold_open.get("enabled"):
        return 0.0, False

    speed = _positive_float(first.get("playback_speed"), default=plan_speed)
    source_index = int(cold_open.get("source_clip_index", 1) or 1)
    source_clip = clips[source_index - 1] if 0 < source_index <= len(clips) else first
    source_played = _clip_source_duration(source_clip) / _positive_float(
        source_clip.get("playback_speed"), default=plan_speed
    )

    # The renderer floors the teaser at 0.8s and clamps it to what is left of the source
    # clip after the offset. Mirror that exactly, or the model drifts on short clips.
    requested = max(0.0, _float(cold_open.get("duration_sec"))) / speed
    offset = max(0.0, _float(cold_open.get("relative_start_sec"))) / speed
    if source_played <= 0 or offset >= source_played:
        return 0.0, False

    actual = min(requested, max(0.8, source_played - offset))
    # Not rounded: this term feeds the expected-duration arithmetic. Rounding happens once,
    # in as_dict, where the number is being read rather than used.
    return max(0.0, actual), True


def _clip_source_duration(clip: Dict[str, Any]) -> float:
    """Duration of the range the cutter actually encodes.

    ``VideoCutter`` cuts ``safe_start``..``safe_end``, so the plan's ``duration`` field
    (built from ``source_start``/``source_end``) is only a fallback.
    """
    start = clip.get("safe_start", clip.get("source_start"))
    end = clip.get("safe_end", clip.get("source_end"))
    span = _float(end) - _float(start)
    if span > 0:
        return span
    return max(0.0, _float(clip.get("duration")))


def _float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _positive_float(value: Any, *, default: float) -> float:
    parsed = _float(value, default=default)
    return parsed if parsed > 0 else default
