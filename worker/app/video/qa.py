"""Clip QA.

Two contract fixes over the previous version:

1. **Explicit inputs.** QA used to read ``title``/``hook``/``description``/``hashtags`` off
   each *cut* dict. The response schema puts that metadata on the ``post`` object of a final
   video, never on a cut, so every clip was charged ``missing_hook`` + ``missing_title`` +
   ``missing_description`` + ``sparse_hashtags`` (−13) for metadata that was present all
   along. ``post_metadata`` is now an explicit parameter.

2. **Diarization honesty.** ``speaker_labels_unavailable`` used to be indistinguishable from
   "this clip has one speaker". ``diarization_status`` is now explicit, and when it is
   degraded the speaker checks report *unmeasurable* rather than passing or failing.

Scope note (source-cut vs final-output QA): this evaluates the requested cut ranges against
the rendered artefact it is handed. The final renderer additionally applies a playback-speed
change, transitions, a cold open and subtitle burn-in, so a full final-output QA would need
to probe the assembled file. That gap is documented rather than papered over — see
``final_output_qa_gap`` in the report.
"""
from pathlib import Path
from typing import Dict, List

from app.runtime.subprocess_runner import run_ffprobe


DIARIZATION_AVAILABLE = "available"
DIARIZATION_DEGRADED = "degraded"


class ClipQA:
    def __init__(
        self,
        min_duration_sec: int = 25,
        max_duration_sec: int = 90,
        max_speakers_per_clip: int = 3,
    ):
        self.min_duration_sec = min_duration_sec
        self.max_duration_sec = max_duration_sec
        self.max_speakers_per_clip = max_speakers_per_clip

    def evaluate(
        self,
        requested_cuts: List[Dict],
        rendered_files: List[Path],
        transcript_segments: List[Dict] | None = None,
        post_metadata: Dict | None = None,
        diarization_status: str = DIARIZATION_AVAILABLE,
    ) -> Dict:
        transcript_segments = transcript_segments or []
        post_metadata = post_metadata or {}
        clip_reports: List[Dict] = []
        summary = {
            "total_clips": 0,
            "approved_clips": 0,
            "needs_review_clips": 0,
            "blocked_clips": 0,
            "average_score": 0,
        }
        total_score = 0

        for index, rendered_file in enumerate(rendered_files):
            cut = requested_cuts[index] if index < len(requested_cuts) else {}
            report = self._evaluate_clip(
                clip_index=index + 1,
                requested_cut=cut,
                rendered_file=rendered_file,
                transcript_segments=transcript_segments,
                post_metadata=post_metadata,
                diarization_status=diarization_status,
            )
            clip_reports.append(report)
            summary["total_clips"] += 1
            summary[f"{report['decision']}_clips"] += 1
            total_score += report["score"]

        overall_decision = "approved"
        if summary["blocked_clips"] > 0:
            overall_decision = "blocked"
        elif summary["needs_review_clips"] > 0:
            overall_decision = "needs_review"

        if summary["total_clips"] > 0:
            summary["average_score"] = round(total_score / summary["total_clips"])

        return {
            "decision": overall_decision,
            "summary": summary,
            "diarization_status": diarization_status,
            "qa_scope": "source_cut",
            "clips": clip_reports,
        }

    def _evaluate_clip(
        self,
        clip_index: int,
        requested_cut: Dict,
        rendered_file: Path,
        transcript_segments: List[Dict],
        post_metadata: Dict,
        diarization_status: str,
    ) -> Dict:
        issues: List[Dict] = []
        warnings: List[str] = []

        requested_start = float(requested_cut.get("safe_start", requested_cut.get("start", 0.0)) or 0.0)
        requested_end = float(requested_cut.get("safe_end", requested_cut.get("end", 0.0)) or 0.0)
        requested_duration = max(0.0, requested_end - requested_start)
        rendered_duration = self._probe_duration(rendered_file)

        if requested_duration < self.min_duration_sec:
            issues.append({"severity": "blocked", "code": "duration_too_short"})
        elif requested_duration > self.max_duration_sec:
            issues.append({"severity": "review", "code": "duration_too_long"})

        if rendered_duration <= 0:
            issues.append({"severity": "blocked", "code": "render_invalid_duration"})
        elif abs(rendered_duration - requested_duration) > 2.5:
            issues.append({"severity": "review", "code": "render_duration_mismatch"})

        # Post metadata belongs to the final video, not to an individual cut.
        if not str(post_metadata.get("hook") or "").strip():
            warnings.append("missing_hook")
        elif self._is_weak_hook(str(post_metadata.get("hook", ""))):
            warnings.append("weak_hook")
        if not str(post_metadata.get("title") or "").strip():
            warnings.append("missing_title")
        if not str(post_metadata.get("description") or "").strip():
            warnings.append("missing_description")
        if self._has_sparse_hashtags(post_metadata):
            warnings.append("sparse_hashtags")

        speakers = self._speakers_in_range(transcript_segments, requested_start, requested_end)
        speaker_measurable = diarization_status == DIARIZATION_AVAILABLE and bool(
            set(speakers) - {"UNKNOWN"}
        )

        if not speaker_measurable:
            # Absence of diarization is reported, never scored as a pass.
            warnings.append("speaker_continuity_unmeasurable")
        elif len(speakers) > self.max_speakers_per_clip:
            issues.append({"severity": "review", "code": "too_many_speakers"})

        if transcript_segments and requested_start > 0 and requested_end > requested_start:
            if self._starts_inside_segment(transcript_segments, requested_start):
                warnings.append("starts_mid_segment")
            if self._ends_inside_segment(transcript_segments, requested_end):
                warnings.append("ends_mid_segment")

        decision = self._decision_from_issues(issues)
        score = self._score_clip(issues, warnings)
        return {
            "clip_index": clip_index,
            "cut_id": requested_cut.get("cut_id"),
            "file_name": rendered_file.name,
            "decision": decision,
            "score": score,
            "requested": {
                "start": requested_start,
                "end": requested_end,
                "duration": requested_duration,
            },
            "rendered_duration": rendered_duration,
            "speakers": speakers,
            "speaker_measurable": speaker_measurable,
            "issues": issues,
            "warnings": warnings,
        }

    def _decision_from_issues(self, issues: List[Dict]) -> str:
        severities = {issue["severity"] for issue in issues}
        if "blocked" in severities:
            return "blocked"
        if "review" in severities:
            return "needs_review"
        return "approved"

    def _score_clip(self, issues: List[Dict], warnings: List[str]) -> int:
        score = 100

        for issue in issues:
            code = str(issue.get("code", ""))
            severity = str(issue.get("severity", "review"))
            if severity == "blocked":
                if code == "render_invalid_duration":
                    score -= 50
                elif code == "duration_too_short":
                    score -= 40
                else:
                    score -= 35
            else:
                if code == "render_duration_mismatch":
                    score -= 15
                elif code == "too_many_speakers":
                    score -= 12
                elif code == "duration_too_long":
                    score -= 8
                else:
                    score -= 10

        for warning in warnings:
            if warning in {"starts_mid_segment", "ends_mid_segment"}:
                score -= 8
            elif warning == "speaker_continuity_unmeasurable":
                score -= 6
            elif warning == "weak_hook":
                score -= 5
            elif warning == "sparse_hashtags":
                score -= 3
            elif warning == "missing_hook":
                score -= 4
            elif warning in {"missing_title", "missing_description"}:
                score -= 3
            else:
                score -= 2

        return max(0, min(100, score))

    def _probe_duration(self, video_path: Path) -> float:
        if not video_path.exists():
            return 0.0

        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]

        try:
            result = run_ffprobe(command, step="qa_probe_duration")
        except Exception:
            # QA is advisory: an unreadable clip scores 0 rather than failing the job.
            return 0.0

        try:
            return float((result.stdout or b"").decode("utf-8", errors="replace").strip())
        except Exception:
            return 0.0

    def _speakers_in_range(
        self,
        transcript_segments: List[Dict],
        start: float,
        end: float,
    ) -> List[str]:
        speakers = {
            str(segment.get("speaker", "UNKNOWN"))
            for segment in transcript_segments
            if float(segment.get("end", 0.0)) >= start and float(segment.get("start", 0.0)) <= end
        }
        return sorted(speakers)

    def _starts_inside_segment(self, transcript_segments: List[Dict], timestamp: float) -> bool:
        return any(
            float(s.get("start", 0.0)) < timestamp < float(s.get("end", 0.0))
            for s in transcript_segments
        )

    def _ends_inside_segment(self, transcript_segments: List[Dict], timestamp: float) -> bool:
        return any(
            float(s.get("start", 0.0)) < timestamp < float(s.get("end", 0.0))
            for s in transcript_segments
        )

    def _has_sparse_hashtags(self, post_metadata: Dict) -> bool:
        hashtags = post_metadata.get("hashtags") or []
        if not isinstance(hashtags, list):
            return True
        return len([tag for tag in hashtags if str(tag).strip()]) < 3

    def _is_weak_hook(self, hook: str) -> bool:
        """Structural only.

        The previous version additionally matched a hardcoded list of titles and thumbnail
        texts from an old geopolitics job ("o jogo por trás", "dinheiro = poder", ...). Those
        penalised a specific past topic and said nothing about a football clip, so they are
        gone. Whether a hook is *interesting* is an editorial judgement that belongs to the
        model; what remains here is whether it is structurally usable.
        """
        text = hook.strip()
        if len(text) < 24:
            return True
        normalized = text.lower()
        weak_starts = ("porque ", "então ", "aí ", "e o ", "mas ")
        return normalized.startswith(weak_starts) and len(text) < 48
