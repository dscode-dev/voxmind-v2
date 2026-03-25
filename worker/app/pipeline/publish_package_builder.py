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
        post_payload: Dict | None,
        final_reel_path: Path | None,
        subtitle_path: Path | None,
        qa_report: Dict | None,
        automation_report: Dict | None,
    ) -> Dict:
        primary_clip = cuts[0] if cuts else {}
        post_payload = post_payload or {}
        ordered_hashtags = self._collect_hashtags(cuts, post_payload)
        primary_title = str(post_payload.get("title") or primary_clip.get("title") or "").strip()
        primary_hook = str(post_payload.get("hook") or primary_clip.get("hook") or "").strip()
        description = str(post_payload.get("description") or primary_clip.get("description") or "").strip()
        thumbnail_text = str(post_payload.get("thumbnail") or primary_clip.get("thumbnail") or "").strip()
        speaker_focus = str(post_payload.get("speaker_focus") or primary_clip.get("speaker_focus") or "").strip()

        return {
            "job_id": job_id,
            "clip_mode": clip_mode,
            "video_ratio": video_ratio,
            "publish_status": self._publish_status(qa_report, final_reel_path),
            "primary_hook": primary_hook,
            "primary_title": primary_title,
            "description": description,
            "thumbnail_text": thumbnail_text,
            "speaker_focus": speaker_focus or None,
            "caption_text": self._build_caption_text(primary_title, primary_hook, description, ordered_hashtags),
            "hashtags": ordered_hashtags,
            "telegram_caption": self._build_telegram_caption(primary_title, primary_hook, description, ordered_hashtags),
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

    def _collect_hashtags(self, cuts: List[Dict], post_payload: Dict) -> List[str]:
        seen: set[str] = set()
        ordered: List[str] = []
        sources = [post_payload.get("hashtags") or []]
        sources.extend(cut.get("hashtags") or [] for cut in cuts)
        for source in sources:
            for item in source:
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

    def _build_caption_text(self, title: str, hook: str, description: str, hashtags: List[str]) -> str:
        parts: List[str] = []

        if title:
            parts.append(title)
        if hook and hook.lower() != title.lower():
            parts.append(hook)
        if description:
            parts.append(description)

        if hashtags:
            parts.append(" ".join(hashtags[:6]))

        return "\n\n".join(part for part in parts if part)

    def _build_telegram_caption(self, title: str, hook: str, description: str, hashtags: List[str]) -> str:
        lines = [title or "Video pronto"]
        if hook:
            lines.append(hook)
        if description:
            lines.append(description)
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
