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
        final_clip_files: List[Path],
        final_reel_path: Path | None,
        subtitle_path: Path | None,
        qa_report: Dict | None,
        automation_report: Dict | None,
        final_video_specs: List[Dict] | None = None,
    ) -> Dict:
        primary_clip = cuts[0] if cuts else {}
        post_payload = post_payload or {}
        ordered_hashtags = self._collect_hashtags(cuts, post_payload)
        primary_title = str(post_payload.get("title") or primary_clip.get("title") or "").strip()
        primary_hook = str(post_payload.get("hook") or primary_clip.get("hook") or "").strip()
        description = str(post_payload.get("description") or primary_clip.get("description") or "").strip()
        thumbnail_text = str(post_payload.get("thumbnail") or primary_clip.get("thumbnail") or "").strip()
        speaker_focus = str(post_payload.get("speaker_focus") or primary_clip.get("speaker_focus") or "").strip()
        videos = self._build_video_payloads(cuts, final_clip_files, post_payload, final_video_specs or [])

        return {
            "job_id": job_id,
            "clip_mode": clip_mode,
            "video_ratio": video_ratio,
            "publish_status": self._publish_status(qa_report, final_reel_path, final_clip_files),
            "primary_hook": primary_hook,
            "primary_title": primary_title,
            "description": description,
            "thumbnail_text": thumbnail_text,
            "speaker_focus": speaker_focus or None,
            "caption_text": self._build_caption_text(primary_title, primary_hook, description, ordered_hashtags),
            "hashtags": ordered_hashtags,
            "telegram_caption": self._build_telegram_caption(primary_title, primary_hook, description, ordered_hashtags),
            "videos": videos,
            "final_clips": self._final_clips_payload(final_clip_files),
            "final_reel": self._final_reel_payload(final_reel_path),
            "subtitles": self._subtitle_payload(subtitle_path),
            "automation": automation_report or {},
        }

    def _publish_status(
        self,
        qa_report: Dict | None,
        final_reel_path: Path | None,
        final_clip_files: List[Path],
    ) -> str:
        has_final_reel = final_reel_path is not None and final_reel_path.exists()
        has_final_clips = any(path.exists() for path in final_clip_files)
        if not has_final_reel and not has_final_clips:
            return "missing_final_assets"

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

    def _final_clips_payload(self, final_clip_files: List[Path]) -> List[Dict]:
        payload: List[Dict] = []
        for index, path in enumerate(final_clip_files, start=1):
            payload.append(
                {
                    "clip_index": index,
                    "status": "generated" if path.exists() else "missing",
                    "file_name": path.name,
                    "local_path": str(path),
                }
            )
        return payload

    def _build_video_payloads(
        self,
        cuts: List[Dict],
        final_clip_files: List[Path],
        fallback_post: Dict,
        final_video_specs: List[Dict],
    ) -> List[Dict]:
        if final_video_specs:
            videos: List[Dict] = []
            for index, spec in enumerate(final_video_specs, start=1):
                post = spec.get("post") or fallback_post or {}
                spec_cuts = list(spec.get("cuts") or [])
                hashtags = self._collect_hashtags(spec_cuts, post)
                final_clip = final_clip_files[index - 1] if index - 1 < len(final_clip_files) else None
                videos.append(
                    {
                        "video_index": int(spec.get("video_index") or index),
                        "post": {
                            "title": str(post.get("title") or "").strip(),
                            "hook": str(post.get("hook") or "").strip(),
                            "hook_source_cut_index": int(post.get("hook_source_cut_index") or 0),
                            "description": str(post.get("description") or "").strip(),
                            "hashtags": hashtags,
                            "thumbnail": str(post.get("thumbnail") or "").strip(),
                            "soundtrack_suggestion": str(post.get("soundtrack_suggestion") or "").strip() or None,
                            "speaker_focus": str(post.get("speaker_focus") or "").strip() or None,
                        },
                        "final_clip": {
                            "status": "generated" if final_clip and final_clip.exists() else "missing",
                            "file_name": final_clip.name if final_clip else None,
                            "local_path": str(final_clip) if final_clip else None,
                        },
                    }
                )
            return videos

        videos: List[Dict] = []
        for index, cut in enumerate(cuts, start=1):
            post = cut.get("_post") or fallback_post or {}
            hashtags = self._collect_hashtags([cut], post)
            final_clip = final_clip_files[index - 1] if index - 1 < len(final_clip_files) else None
            videos.append(
                {
                    "video_index": index,
                    "post": {
                        "title": str(post.get("title") or cut.get("title") or "").strip(),
                        "hook": str(post.get("hook") or cut.get("hook") or "").strip(),
                        "hook_source_cut_index": int(post.get("hook_source_cut_index") or 0),
                        "description": str(post.get("description") or cut.get("description") or "").strip(),
                        "hashtags": hashtags,
                        "thumbnail": str(post.get("thumbnail") or cut.get("thumbnail") or "").strip(),
                        "soundtrack_suggestion": str(post.get("soundtrack_suggestion") or "").strip() or None,
                        "speaker_focus": str(post.get("speaker_focus") or cut.get("speaker_focus") or "").strip() or None,
                    },
                    "final_clip": {
                        "status": "generated" if final_clip and final_clip.exists() else "missing",
                        "file_name": final_clip.name if final_clip else None,
                        "local_path": str(final_clip) if final_clip else None,
                    },
                }
            )
        return videos
