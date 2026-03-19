import subprocess
from pathlib import Path
from typing import Dict, List


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
    ) -> Dict:
        transcript_segments = transcript_segments or []
        clip_reports: List[Dict] = []
        summary = {
            "total_clips": 0,
            "approved_clips": 0,
            "needs_review_clips": 0,
            "blocked_clips": 0,
        }

        for index, rendered_file in enumerate(rendered_files):
            cut = requested_cuts[index] if index < len(requested_cuts) else {}
            report = self._evaluate_clip(
                clip_index=index + 1,
                requested_cut=cut,
                rendered_file=rendered_file,
                transcript_segments=transcript_segments,
            )
            clip_reports.append(report)
            summary["total_clips"] += 1
            summary[f"{report['decision']}_clips"] += 1

        overall_decision = "approved"
        if summary["blocked_clips"] > 0:
            overall_decision = "blocked"
        elif summary["needs_review_clips"] > 0:
            overall_decision = "needs_review"

        return {
            "decision": overall_decision,
            "summary": summary,
            "clips": clip_reports,
        }

    def _evaluate_clip(
        self,
        clip_index: int,
        requested_cut: Dict,
        rendered_file: Path,
        transcript_segments: List[Dict],
    ) -> Dict:
        issues: List[Dict] = []
        warnings: List[str] = []

        requested_start = float(requested_cut.get("start", 0.0))
        requested_end = float(requested_cut.get("end", 0.0))
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

        if not requested_cut.get("hook"):
            warnings.append("missing_hook")
        if not requested_cut.get("title"):
            warnings.append("missing_title")
        if not requested_cut.get("description"):
            warnings.append("missing_description")

        speakers = self._speakers_in_range(
            transcript_segments,
            requested_start,
            requested_end,
        )
        if len(speakers) > self.max_speakers_per_clip:
            issues.append({"severity": "review", "code": "too_many_speakers"})

        if transcript_segments and requested_start > 0 and requested_end > requested_start:
            if self._starts_inside_segment(transcript_segments, requested_start):
                warnings.append("starts_mid_segment")
            if self._ends_inside_segment(transcript_segments, requested_end):
                warnings.append("ends_mid_segment")

        decision = self._decision_from_issues(issues)
        return {
            "clip_index": clip_index,
            "file_name": rendered_file.name,
            "decision": decision,
            "requested": {
                "start": requested_start,
                "end": requested_end,
                "duration": requested_duration,
            },
            "rendered_duration": rendered_duration,
            "speakers": speakers,
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
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception:
            return 0.0

        try:
            return float(result.stdout.strip())
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
        for segment in transcript_segments:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", 0.0))
            if start < timestamp < end:
                return True
        return False

    def _ends_inside_segment(self, transcript_segments: List[Dict], timestamp: float) -> bool:
        for segment in transcript_segments:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", 0.0))
            if start < timestamp < end:
                return True
        return False
