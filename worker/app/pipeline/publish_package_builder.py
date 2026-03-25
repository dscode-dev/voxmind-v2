from pathlib import Path
from typing import Dict, List


class PublishPackageBuilder:

    def build(
        self,
        *,
        job_id: str,
        clip_mode: str,
        video_ratio: str,
        cuts: List[Dict],
        final_reel_path: Path | None,
        subtitle_path: Path | None,
        qa_report: Dict | None,
        automation_report: Dict | None,
    ) -> Dict:
        primary_clip = cuts[0] if cuts else {}
        ordered_hashtags = self._collect_hashtags(cuts)

        return {
            "job_id": job_id,
            "clip_mode": clip_mode,
            "video_ratio": video_ratio,
            "publish_status": self._publish_status(qa_report, final_reel_path),
            "primary_hook": primary_clip.get("hook"),
            "primary_title": primary_clip.get("title"),
            "thumbnail_text": primary_clip.get("thumbnail"),
            "speaker_focus": primary_clip.get("speaker_focus"),
            "caption_text": self._build_caption_text(primary_clip, ordered_hashtags),
            "hashtags": ordered_hashtags,
            "telegram_caption": self._build_telegram_caption(primary_clip, ordered_hashtags),
            "final_reel": self._final_reel_payload(final_reel_path),
            "subtitles": self._subtitle_payload(subtitle_path),
            "automation": automation_report or {},
        }

    def _publish_status(self, qa_report: Dict | None, final_reel_path: Path | None) -> str:
        if final_reel_path is None or not final_reel_path.exists():
            return "missing_final_reel"

        if not qa_report:
            return "ready"

        decision = str(qa_report.get("decision") or "approved")
        if decision == "blocked":
            return "blocked"
        if decision == "needs_review":
            return "needs_review"
        return "ready"

    def _collect_hashtags(self, cuts: List[Dict]) -> List[str]:
        seen: set[str] = set()
        ordered: List[str] = []
        for cut in cuts:
            for item in cut.get("hashtags") or []:
                tag = str(item).strip()
                if not tag:
                    continue
                normalized = tag if tag.startswith("#") else f"#{tag}"
                lowered = normalized.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                ordered.append(normalized)
        return ordered

    def _build_caption_text(self, primary_clip: Dict, hashtags: List[str]) -> str:
        parts: List[str] = []
        title = str(primary_clip.get("title") or "").strip()
        description = str(primary_clip.get("description") or "").strip()
        hook = str(primary_clip.get("hook") or "").strip()

        if title:
            parts.append(title)
        if hook and hook.lower() != title.lower():
            parts.append(hook)
        if description:
            parts.append(description)

        if hashtags:
            parts.append(" ".join(hashtags[:6]))

        return "\n\n".join(part for part in parts if part)

    def _build_telegram_caption(self, primary_clip: Dict, hashtags: List[str]) -> str:
        title = str(primary_clip.get("title") or "Corte pronto").strip()
        hook = str(primary_clip.get("hook") or "").strip()
        speaker_focus = str(primary_clip.get("speaker_focus") or "").strip()

        lines = [title]
        if hook:
            lines.append(hook)
        if speaker_focus:
            lines.append(f"Speaker focus: {speaker_focus}")
        if hashtags:
            lines.append(" ".join(hashtags[:5]))

        return "\n".join(line for line in lines if line)

    def _final_reel_payload(self, final_reel_path: Path | None) -> Dict:
        if final_reel_path is None:
            return {
                "status": "not_generated",
                "file_name": None,
                "local_path": None,
            }

        return {
            "status": "generated" if final_reel_path.exists() else "missing",
            "file_name": final_reel_path.name,
            "local_path": str(final_reel_path),
        }

    def _subtitle_payload(self, subtitle_path: Path | None) -> Dict:
        if subtitle_path is None:
            return {
                "status": "not_generated",
                "file_name": None,
                "local_path": None,
                "format": "srt",
            }

        return {
            "status": "generated" if subtitle_path.exists() else "missing",
            "file_name": subtitle_path.name,
            "local_path": str(subtitle_path),
            "format": "srt",
        }
