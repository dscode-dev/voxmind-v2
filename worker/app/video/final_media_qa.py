"""Final Media QA — the technical gate over the MP4 that would actually be published.

``app/video/qa.py`` evaluates the *source cuts*: were the right ranges chosen, do they land
on speaker boundaries, is the post metadata there. It is handed the intermediate cut files
and it answers editorial questions.

Between that report and the publishable artefact the renderer applies a playback-speed
change, a cold open, transitions, a concat, a soundtrack mix and a subtitle burn — six
transformations, any of which can produce a file that is silent, black, truncated, the wrong
shape or undecodable while every source-cut check still passes. Nothing looked at the result.

This module looks at the result. It answers one question — *is this file technically fit to
publish without a human watching it first?* — and it answers per check, never as a single
opaque number:

    AUTO_READY      every check passed
    NEEDS_REVIEW    something is off but the file is intact
    BLOCKED         the file is broken, or violates a contract publishing depends on

The two layers stay separate. A high editorial score must never launder a structurally
broken render, and a technical pass says nothing about whether the clip is worth posting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from app.observability import get_logger
from app.video.media_probe import (
    MediaAnalysis,
    MediaProbe,
    analyze_media,
    probe_media,
)
from app.video.render_timeline import (
    ExpectedTimeline,
    cold_open_metadata,
    expected_final_duration,
    transition_issues,
)
from app.video.subtitle_qa import (
    SubtitleTimeline,
    check_subtitle_timeline,
    parse_subtitle_file,
)

logger = get_logger(__name__)

AUTO_READY = "auto_ready"
NEEDS_REVIEW = "needs_review"
BLOCKED = "blocked"

PASS = "pass"
REVIEW = "review"
FAIL = "fail"
UNMEASURABLE = "unmeasurable"

QA_SCOPE = "final_output"

# Ranked worst-first, so a report's headline status is the worst thing in it.
_SEVERITY_ORDER = {PASS: 0, UNMEASURABLE: 1, REVIEW: 2, FAIL: 3}

# Failure codes a retry cannot fix: the same plan re-rendered produces the same file.
# Used to classify, never to decide the queue's behaviour (PR-RUNTIME-01 owns that).
DETERMINISTIC_FAILURE_CODES = frozenset(
    {
        "wrong_aspect_ratio",
        "dimensions_not_positive",
        "audio_stream_missing",
        "video_stream_missing",
        "duration_mismatch",
        "duration_mismatch_severe",
        "subtitle_out_of_bounds",
        "subtitle_ordering_invalid",
        "subtitle_impossible_range",
        "subtitle_negative_timestamp",
        "subtitle_file_empty",
        "transition_exceeds_clip",
        "audio_fully_silent",
        "video_mostly_black",
    }
)

# Failure codes where a retry plausibly helps: a transient tool or filesystem fault.
TRANSIENT_FAILURE_CODES = frozenset(
    {
        "artifact_missing",
        "probe_failed",
        "decode_timeout",
    }
)


@dataclass(frozen=True)
class FinalMediaQAPolicy:
    """Every threshold the gate uses, in one place.

    PR-CUT-01 inherited thresholds scattered through the scorer and it took a measurement
    harness to find the contradictions. Same mistake, not repeated.
    """

    # Duration. Tolerance is absolute + relative: encoder/timebase error grows with length.
    duration_tolerance_sec: float = 1.5
    duration_tolerance_ratio: float = 0.02
    # Beyond this multiple of the tolerance the render lost or duplicated real content.
    duration_severe_multiplier: float = 4.0

    # Shape. The contract is the ratio, not a literal resolution: presets may vary.
    aspect_ratio_tolerance: float = 0.02

    # Video content.
    black_ratio_review: float = 0.25
    black_ratio_block: float = 0.60
    max_freeze_sec: float = 4.0
    black_min_sec: float = 0.5
    black_pixel_threshold: float = 0.98
    freeze_min_sec: float = 2.0
    freeze_noise_db: float = -60.0

    # Audio content.
    silence_noise_db: float = -50.0
    silence_min_sec: float = 1.0
    max_silence_sec: float = 8.0
    silence_ratio_block: float = 0.50
    min_mean_volume_db: float = -45.0
    # Clipping is read from the PEAK, not from a sample count. Measured on this ffmpeg
    # build, volumedetect's `histogram_0db` bucket holds 19.6% of samples for a clean
    # full-scale sine and 19.9% for a hard-clipped one - it tracks waveform shape, not
    # distortion, so it cannot be the detector. What does separate them is whether the
    # signal reaches digital full scale at all: the clean sine peaked at -0.3 dB, while
    # mild overdrive, hard clipping and a square wave all read 0.0 dB.
    #
    # A mixed render touching 0 dBFS means the amix drove past the ceiling. That is worth a
    # human ear, not an automatic block: the peak says the ceiling was reached, not how much
    # of the programme was damaged. Only a grossly flat-topped waveform blocks.
    max_peak_db: float = -0.1
    clipping_flat_ratio_block: float = 0.40

    # Subtitles.
    subtitle_tolerance_sec: float = 0.5

    decode_timeout_sec: float = 900.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "duration_tolerance_sec": self.duration_tolerance_sec,
            "duration_tolerance_ratio": self.duration_tolerance_ratio,
            "duration_severe_multiplier": self.duration_severe_multiplier,
            "aspect_ratio_tolerance": self.aspect_ratio_tolerance,
            "black_ratio_review": self.black_ratio_review,
            "black_ratio_block": self.black_ratio_block,
            "max_freeze_sec": self.max_freeze_sec,
            "silence_noise_db": self.silence_noise_db,
            "max_silence_sec": self.max_silence_sec,
            "silence_ratio_block": self.silence_ratio_block,
            "min_mean_volume_db": self.min_mean_volume_db,
            "max_peak_db": self.max_peak_db,
            "clipping_flat_ratio_block": self.clipping_flat_ratio_block,
            "subtitle_tolerance_sec": self.subtitle_tolerance_sec,
            "decode_timeout_sec": self.decode_timeout_sec,
        }

    def duration_tolerance_for(self, expected_sec: float) -> float:
        return max(
            self.duration_tolerance_sec,
            abs(expected_sec) * self.duration_tolerance_ratio,
        )


@dataclass(frozen=True)
class FinalMediaQAInput:
    """What the gate is asked to evaluate.

    ``artifact_id`` binds the verdict to a specific file. A QA result that cannot say which
    artefact it describes is worse than no result: it invites approving ``final_01.mp4`` and
    shipping ``final_02.mp4``.
    """

    final_file: Path
    artifact_id: str
    render_plan: Dict[str, Any] | None = None
    subtitle_path: Path | None = None
    post_metadata: Dict[str, Any] = field(default_factory=dict)
    video_ratio: str = "portrait"
    video_index: int | None = None
    cut_ids: List[str] = field(default_factory=list)
    expect_audio: bool = True
    expect_subtitles: bool = True


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    code: str | None = None
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "code": self.code, "detail": self.detail}


class FinalMediaQA:
    """Probes the artefact, then applies the policy. Measurement and judgement stay apart."""

    def __init__(self, policy: FinalMediaQAPolicy | None = None, job_id: str | None = None):
        self.policy = policy or FinalMediaQAPolicy()
        self.job_id = job_id

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, request: FinalMediaQAInput) -> Dict[str, Any]:
        probe = probe_media(request.final_file, job_id=self.job_id)
        expected = expected_final_duration(request.render_plan)

        structural = self._structural_checks(probe, request)
        if _worst_status(structural) == FAIL:
            # An unopenable or empty file cannot be analysed further, and must never be
            # handed to a human reviewer as though it were watchable.
            return self._report(
                request=request,
                probe=probe,
                analysis=None,
                subtitles=SubtitleTimeline(path=request.subtitle_path, present=False),
                expected=expected,
                checks=structural,
            )

        analysis = analyze_media(
            request.final_file,
            duration_sec=probe.duration_sec,
            silence_noise_db=self.policy.silence_noise_db,
            silence_min_sec=self.policy.silence_min_sec,
            black_min_sec=self.policy.black_min_sec,
            black_pixel_threshold=self.policy.black_pixel_threshold,
            freeze_min_sec=self.policy.freeze_min_sec,
            freeze_noise_db=self.policy.freeze_noise_db,
            timeout_sec=self.policy.decode_timeout_sec,
            has_audio=probe.has_audio,
            job_id=self.job_id,
        )
        subtitles = parse_subtitle_file(request.subtitle_path)

        checks = dict(structural)
        checks.update(self._decode_check(analysis))
        checks.update(self._duration_check(probe, expected))
        checks.update(self._dimensions_check(probe, request))
        checks.update(self._video_content_checks(analysis))
        checks.update(self._audio_content_checks(probe, analysis, request))
        checks.update(self._subtitle_check(subtitles, probe, request))
        checks.update(self._transition_check(request.render_plan))

        return self._report(
            request=request,
            probe=probe,
            analysis=analysis,
            subtitles=subtitles,
            expected=expected,
            checks=checks,
        )

    def evaluate_many(self, requests: List[FinalMediaQAInput]) -> Dict[str, Any]:
        reports = [self.evaluate(request) for request in requests]
        return summarize(reports, policy=self.policy)

    # -------------------------------------------------------------------- checks

    def _structural_checks(self, probe: MediaProbe, request: FinalMediaQAInput) -> Dict[str, Check]:
        if not probe.exists:
            return {
                "container_valid": Check(
                    "container_valid", FAIL, "artifact_missing",
                    f"{request.final_file} does not exist",
                )
            }
        if probe.size_bytes == 0:
            return {
                "container_valid": Check(
                    "container_valid", FAIL, "artifact_empty", "the file is zero bytes"
                )
            }
        if not probe.probe_ok:
            return {
                "container_valid": Check(
                    "container_valid", FAIL, "invalid_container", probe.error or "ffprobe could not open the file"
                )
            }

        checks = {"container_valid": Check("container_valid", PASS, detail=probe.format_name or "")}

        if not probe.has_video:
            checks["video_stream"] = Check(
                "video_stream", FAIL, "video_stream_missing", "the container declares no video stream"
            )
        elif probe.video.width <= 0 or probe.video.height <= 0:
            checks["video_stream"] = Check(
                "video_stream", FAIL, "dimensions_not_positive",
                f"{probe.video.width}x{probe.video.height}",
            )
        else:
            checks["video_stream"] = Check(
                "video_stream", PASS,
                detail=f"{probe.video.width}x{probe.video.height} {probe.video.codec}",
            )

        if not probe.has_audio:
            if request.expect_audio:
                checks["audio_stream"] = Check(
                    "audio_stream", FAIL, "audio_stream_missing",
                    "the content requires audio but the container declares none",
                )
            else:
                checks["audio_stream"] = Check(
                    "audio_stream", PASS, detail="no audio expected for this artefact"
                )
        else:
            checks["audio_stream"] = Check(
                "audio_stream", PASS,
                detail=f"{probe.audio.codec} {probe.audio.sample_rate}Hz {probe.audio.channels}ch",
            )

        if not probe.duration_declared or probe.duration_sec <= 0:
            checks["duration_finite"] = Check(
                "duration_finite", FAIL, "duration_invalid",
                f"the container declares duration={probe.duration_sec!r}",
            )
        else:
            checks["duration_finite"] = Check(
                "duration_finite", PASS, detail=f"{probe.duration_sec:.3f}s"
            )

        return checks

    def _decode_check(self, analysis: MediaAnalysis) -> Dict[str, Check]:
        if analysis.timed_out:
            return {
                "decode_integrity": Check(
                    "decode_integrity", FAIL, "decode_timeout",
                    f"decoding exceeded {self.policy.decode_timeout_sec:.0f}s",
                )
            }
        if not analysis.decode_ok:
            return {
                "decode_integrity": Check(
                    "decode_integrity", FAIL, "decode_error",
                    analysis.decode_errors or "ffmpeg reported decode errors",
                )
            }
        return {"decode_integrity": Check("decode_integrity", PASS, detail="full decode, no errors")}

    def _duration_check(self, probe: MediaProbe, expected: ExpectedTimeline) -> Dict[str, Check]:
        if expected.clip_count == 0 or expected.expected_duration_sec <= 0:
            return {
                "duration": Check(
                    "duration", UNMEASURABLE, "expected_duration_unknown",
                    "the render plan does not describe a timeline to compare against",
                )
            }

        tolerance = self.policy.duration_tolerance_for(expected.expected_duration_sec)
        delta = probe.duration_sec - expected.expected_duration_sec
        detail = (
            f"actual={probe.duration_sec:.3f}s expected={expected.expected_duration_sec:.3f}s "
            f"delta={delta:+.3f}s tolerance={tolerance:.3f}s"
        )

        if abs(delta) <= tolerance:
            return {"duration": Check("duration", PASS, detail=detail)}
        if abs(delta) > tolerance * self.policy.duration_severe_multiplier:
            return {"duration": Check("duration", FAIL, "duration_mismatch_severe", detail)}
        return {"duration": Check("duration", REVIEW, "duration_mismatch", detail)}

    def _dimensions_check(self, probe: MediaProbe, request: FinalMediaQAInput) -> Dict[str, Check]:
        if not probe.has_video or probe.video.height <= 0:
            return {
                "dimensions": Check(
                    "dimensions", UNMEASURABLE, "dimensions_unknown", "no usable video stream"
                )
            }

        ratio = probe.video.aspect_ratio
        target_name = str(request.video_ratio or "portrait").strip().lower()
        target = 9 / 16 if target_name != "landscape" else 16 / 9
        contract = "9:16" if target_name != "landscape" else "16:9"
        detail = (
            f"{probe.video.width}x{probe.video.height} ratio={ratio:.4f} "
            f"contract={contract} ({target:.4f}) tolerance={self.policy.aspect_ratio_tolerance}"
        )

        if abs(ratio - target) <= self.policy.aspect_ratio_tolerance:
            return {"dimensions": Check("dimensions", PASS, detail=detail)}
        return {"dimensions": Check("dimensions", FAIL, "wrong_aspect_ratio", detail)}

    def _video_content_checks(self, analysis: MediaAnalysis) -> Dict[str, Check]:
        checks: Dict[str, Check] = {}

        black_detail = (
            f"black={analysis.black_total_sec:.2f}s of {analysis.duration_sec:.2f}s "
            f"({analysis.black_ratio:.1%})"
        )
        if analysis.black_ratio >= self.policy.black_ratio_block:
            checks["video_content"] = Check("video_content", FAIL, "video_mostly_black", black_detail)
        elif analysis.black_ratio >= self.policy.black_ratio_review:
            checks["video_content"] = Check("video_content", REVIEW, "video_black_segment", black_detail)
        else:
            checks["video_content"] = Check("video_content", PASS, detail=black_detail)

        freeze = analysis.longest_freeze_sec
        freeze_detail = f"longest frozen run={freeze:.2f}s (limit {self.policy.max_freeze_sec:.2f}s)"
        if freeze > self.policy.max_freeze_sec:
            checks["video_motion"] = Check("video_motion", REVIEW, "video_frozen", freeze_detail)
        else:
            checks["video_motion"] = Check("video_motion", PASS, detail=freeze_detail)

        return checks

    def _audio_content_checks(
        self,
        probe: MediaProbe,
        analysis: MediaAnalysis,
        request: FinalMediaQAInput,
    ) -> Dict[str, Check]:
        if not probe.has_audio:
            # The missing stream is already a FAIL in the structural layer; do not
            # double-count it, and do not silently report the content as fine.
            return {
                "audio_silence": Check("audio_silence", UNMEASURABLE, None, "no audio stream"),
                "audio_level": Check("audio_level", UNMEASURABLE, None, "no audio stream"),
            }
        if not analysis.audio_measured:
            return {
                "audio_silence": Check(
                    "audio_silence", UNMEASURABLE, "audio_analysis_unavailable",
                    "the decode pass produced no audio statistics",
                ),
                "audio_level": Check(
                    "audio_level", UNMEASURABLE, "audio_analysis_unavailable",
                    "the decode pass produced no audio statistics",
                ),
            }

        checks: Dict[str, Check] = {}
        mean = analysis.mean_volume_db
        silence_detail = (
            f"longest silence={analysis.longest_silence_sec:.2f}s "
            f"total={analysis.silence_total_sec:.2f}s of {analysis.duration_sec:.2f}s "
            f"({analysis.silence_ratio:.1%}) mean={mean if mean is not None else 'n/a'}dB"
        )

        fully_silent = mean is None or mean < -80.0
        if fully_silent or analysis.silence_ratio >= self.policy.silence_ratio_block:
            code = "audio_fully_silent" if fully_silent else "audio_long_silence_severe"
            checks["audio_silence"] = Check("audio_silence", FAIL, code, silence_detail)
        elif analysis.longest_silence_sec > self.policy.max_silence_sec:
            checks["audio_silence"] = Check("audio_silence", REVIEW, "audio_long_silence", silence_detail)
        else:
            checks["audio_silence"] = Check("audio_silence", PASS, detail=silence_detail)

        peak = analysis.max_volume_db
        level_detail = (
            f"mean={mean if mean is not None else 'n/a'}dB peak={peak}dB "
            f"at_ceiling={analysis.clipped_samples} samples ({analysis.clipping_ratio:.2%})"
        )
        at_full_scale = peak is not None and peak >= self.policy.max_peak_db

        if at_full_scale and analysis.clipping_ratio >= self.policy.clipping_flat_ratio_block:
            checks["audio_level"] = Check("audio_level", FAIL, "audio_severe_clipping", level_detail)
        elif at_full_scale:
            checks["audio_level"] = Check("audio_level", REVIEW, "audio_peak_clipping", level_detail)
        elif mean is not None and mean < self.policy.min_mean_volume_db and not fully_silent:
            checks["audio_level"] = Check("audio_level", REVIEW, "audio_level_too_low", level_detail)
        else:
            checks["audio_level"] = Check("audio_level", PASS, detail=level_detail)

        return checks

    def _subtitle_check(
        self,
        subtitles: SubtitleTimeline,
        probe: MediaProbe,
        request: FinalMediaQAInput,
    ) -> Dict[str, Check]:
        findings = check_subtitle_timeline(
            subtitles,
            video_duration_sec=probe.duration_sec,
            tolerance_sec=self.policy.subtitle_tolerance_sec,
            expect_subtitles=request.expect_subtitles,
        )
        if not findings:
            detail = (
                f"{subtitles.event_count} events, "
                f"{subtitles.first_start:.2f}s..{subtitles.last_end:.2f}s"
                if subtitles.events
                else "no subtitles expected"
            )
            return {"subtitle_timing": Check("subtitle_timing", PASS, detail=detail)}

        if not subtitles.present and not request.expect_subtitles:
            return {"subtitle_timing": Check("subtitle_timing", PASS, detail="no subtitles expected")}

        # Subtitles never break the file, only its readability: review, never block.
        primary = findings[0]
        detail = "; ".join(f"{f.code}: {f.detail}" for f in findings[:4])
        if len(findings) > 4:
            detail += f" (+{len(findings) - 4} more)"
        return {"subtitle_timing": Check("subtitle_timing", REVIEW, primary.code, detail)}

    def _transition_check(self, render_plan: Dict[str, Any] | None) -> Dict[str, Check]:
        issues = transition_issues(render_plan)
        if not issues:
            return {"transitions": Check("transitions", PASS, detail="within clip bounds")}
        detail = "; ".join(f"clip {i.clip_index} {i.code}: {i.detail}" for i in issues[:4])
        return {"transitions": Check("transitions", REVIEW, issues[0].code, detail)}

    # -------------------------------------------------------------------- report

    def _report(
        self,
        *,
        request: FinalMediaQAInput,
        probe: MediaProbe,
        analysis: MediaAnalysis | None,
        subtitles: SubtitleTimeline,
        expected: ExpectedTimeline,
        checks: Dict[str, Check],
    ) -> Dict[str, Any]:
        worst = _worst_status(checks)
        status = {FAIL: BLOCKED, REVIEW: NEEDS_REVIEW}.get(worst, AUTO_READY)
        reasons = [check.code for check in checks.values() if check.status in {FAIL, REVIEW} and check.code]
        blocking = [check.code for check in checks.values() if check.status == FAIL and check.code]

        report = {
            "qa_scope": QA_SCOPE,
            "artifact_id": request.artifact_id,
            "video_index": request.video_index,
            "file_name": request.final_file.name,
            "file_path": str(request.final_file),
            "cut_ids": list(request.cut_ids),
            "status": status,
            "score": _score(checks),
            "checks": {name: check.as_dict() for name, check in sorted(checks.items())},
            "reasons": sorted(set(reasons)),
            "blocking_reasons": sorted(set(blocking)),
            "retry_classification": classify_failures(blocking),
            "probe": probe.as_dict(),
            "analysis": analysis.as_dict() if analysis else None,
            "subtitles": subtitles.as_dict(),
            "expected_timeline": expected.as_dict(),
            "cold_open": cold_open_metadata(request.render_plan),
            "policy": self.policy.as_dict(),
        }

        logger.info(
            f"Final media QA: {request.artifact_id} -> {status}",
            extra={
                "job_id": self.job_id,
                "step": "final_media_qa",
                "status": status,
                "artifact_id": request.artifact_id,
                "reasons": report["reasons"],
            },
        )
        return report


# ------------------------------------------------------------------------ helpers


def summarize(reports: List[Dict[str, Any]], *, policy: FinalMediaQAPolicy | None = None) -> Dict[str, Any]:
    """Aggregate per-artefact reports without letting a good one hide a bad one."""
    blocked = [r for r in reports if r.get("status") == BLOCKED]
    review = [r for r in reports if r.get("status") == NEEDS_REVIEW]
    ready = [r for r in reports if r.get("status") == AUTO_READY]

    if blocked:
        status = BLOCKED
    elif review:
        status = NEEDS_REVIEW
    elif ready:
        status = AUTO_READY
    else:
        status = BLOCKED

    reasons = sorted({reason for report in reports for reason in report.get("reasons", [])})
    if not reports:
        reasons = ["no_final_media_evaluated"]

    return {
        "qa_scope": QA_SCOPE,
        "status": status,
        "summary": {
            "total_artifacts": len(reports),
            "auto_ready": len(ready),
            "needs_review": len(review),
            "blocked": len(blocked),
        },
        "reasons": reasons,
        "blocking_reasons": sorted({r for report in reports for r in report.get("blocking_reasons", [])}),
        "policy": (policy or FinalMediaQAPolicy()).as_dict(),
        "artifacts": reports,
    }


def classify_failures(codes: List[str]) -> str:
    """Whether re-running the render could plausibly change the outcome.

    This only *labels* the failure. The queue's retry behaviour is PR-RUNTIME-01's, and this
    PR does not touch it — but re-rendering the same plan three times over a wrong aspect
    ratio burns an hour of GPU to produce the same wrong file.
    """
    if not codes:
        return "not_applicable"
    if any(code in DETERMINISTIC_FAILURE_CODES for code in codes):
        return "retry_will_not_help"
    if all(code in TRANSIENT_FAILURE_CODES for code in codes):
        return "retry_may_help"
    return "unknown"


def _worst_status(checks: Dict[str, Check]) -> str:
    return max(
        (check.status for check in checks.values()),
        key=lambda status: _SEVERITY_ORDER.get(status, 0),
        default=PASS,
    )


def _score(checks: Dict[str, Check]) -> int:
    """An aggregate for ranking only.

    It exists because operators want to sort a list. It is never the truth: ``status`` and
    ``checks`` are. A structural failure pins the score low so no aggregate can look healthy
    while the file is broken.
    """
    if not checks:
        return 0
    score = 100
    for check in checks.values():
        if check.status == FAIL:
            score -= 40
        elif check.status == REVIEW:
            score -= 12
        elif check.status == UNMEASURABLE:
            score -= 4
    return max(0, min(100, score))
