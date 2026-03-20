import subprocess
from pathlib import Path
from typing import Dict, List


class ClipQA:
    GENERIC_TITLE_MARKERS = {
        "quem manda de verdade?",
        "por que eles mandam?",
        "o jogo por trás",
        "quem realmente manda",
        "o objetivo final",
        "o tamanho do poder",
    }

    GENERIC_THUMBNAIL_MARKERS = {
        "quem manda?",
        "dinheiro = poder",
        "quem é ele?",
    }

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
        elif self._is_weak_hook(str(requested_cut.get("hook", ""))):
            warnings.append("weak_hook")
        if not requested_cut.get("title"):
            warnings.append("missing_title")
        elif self._is_generic_title(str(requested_cut.get("title", ""))):
            warnings.append("generic_title")
        if not requested_cut.get("description"):
            warnings.append("missing_description")
        if self._has_sparse_hashtags(requested_cut):
            warnings.append("sparse_hashtags")
        if self._is_generic_thumbnail(str(requested_cut.get("thumbnail", ""))):
            warnings.append("generic_thumbnail")

        speakers = self._speakers_in_range(
            transcript_segments,
            requested_start,
            requested_end,
        )
        if transcript_segments and (not speakers or speakers == ["UNKNOWN"]):
            warnings.append("speaker_labels_unavailable")
        if len(speakers) > self.max_speakers_per_clip:
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
            elif warning == "generic_title":
                score -= 8
            elif warning == "speaker_labels_unavailable":
                score -= 6
            elif warning == "weak_hook":
                score -= 5
            elif warning == "generic_thumbnail":
                score -= 4
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

    def _is_generic_title(self, title: str) -> bool:
        normalized = title.strip().lower()
        return normalized in self.GENERIC_TITLE_MARKERS

    def _is_generic_thumbnail(self, thumbnail: str) -> bool:
        normalized = thumbnail.strip().lower().replace("texto", "").strip(" '\"")
        return any(marker in normalized for marker in self.GENERIC_THUMBNAIL_MARKERS)

    def _has_sparse_hashtags(self, requested_cut: Dict) -> bool:
        hashtags = requested_cut.get("hashtags") or []
        if not isinstance(hashtags, list):
            return True
        return len([tag for tag in hashtags if str(tag).strip()]) < 3

    def _is_weak_hook(self, hook: str) -> bool:
        text = hook.strip()
        if len(text) < 24:
            return True
        normalized = text.lower()
        weak_starts = (
            "porque ",
            "então ",
            "aí ",
            "e o ",
            "mas ",
        )
        return normalized.startswith(weak_starts) and len(text) < 48
