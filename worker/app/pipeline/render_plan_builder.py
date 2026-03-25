import re
from typing import Dict, List


DEFAULT_TRANSITION = "hard_cut"
DEFAULT_CAPTION_STYLE = "clean_subtitles"
DEFAULT_COLD_OPEN_DURATION_SEC = 2.0


class RenderPlanBuilder:

    def build(
        self,
        *,
        job_id: str,
        clip_mode: str,
        video_ratio: str,
        cuts: List[Dict],
        post_payload: Dict | None = None,
        transcript_segments: List[Dict] | None = None,
        soundtrack: Dict | None = None,
        qa_report: Dict | None = None,
    ) -> Dict:
        qa_by_index = {
            int(clip.get("clip_index", 0)): clip
            for clip in (qa_report or {}).get("clips", [])
            if clip.get("clip_index")
        }

        clips: List[Dict] = []

        for index, cut in enumerate(cuts, start=1):
            qa_clip = qa_by_index.get(index, {})
            start = float(cut.get("start", 0.0))
            end = float(cut.get("end", 0.0))
            cold_open = self._cold_open_for_clip(
                clip_index=index,
                cut=cut,
                cuts=cuts,
                post_payload=post_payload or {},
                transcript_segments=transcript_segments or [],
            )
            clips.append(
                {
                    "clip_index": index,
                    "source_start": start,
                    "source_end": end,
                    "safe_start": float(cut.get("safe_start", start)),
                    "safe_end": float(cut.get("safe_end", end)),
                    "duration": max(0.0, end - start),
                    "speaker_focus": cut.get("speaker_focus"),
                    "transition_after": self._normalized_transition(cut.get("transition_after")),
                    "transition_duration_ms": self._transition_duration_ms(cut.get("transition_after")),
                    "caption_style": cut.get("caption_style") or DEFAULT_CAPTION_STYLE,
                    "overlay_enabled": False,
                    "on_screen_text": cut.get("on_screen_text") or "",
                    "emphasis_words": cut.get("emphasis_words", []),
                    "text_timing": self._text_timing(cut),
                    "hook": cut.get("hook"),
                    "title": cut.get("title"),
                    "cold_open": cold_open,
                    "review_hints": self._review_hints(qa_clip),
                }
            )

        return {
            "job_id": job_id,
            "clip_mode": clip_mode,
            "video_ratio": video_ratio,
            "render_intent": "social_ready_short_form",
            "global_style": self._global_style(video_ratio),
            "soundtrack": soundtrack or {},
            "clips": clips,
        }

    def _global_style(self, video_ratio: str) -> Dict:
        if video_ratio == "portrait":
            return {
                "aspect_ratio": "9:16",
                "caption_position": "lower_third_center",
                "hook_text_treatment": "bold_punchy",
                "default_transition": DEFAULT_TRANSITION,
            }

        return {
            "aspect_ratio": "16:9",
            "caption_position": "bottom_center",
            "hook_text_treatment": "clean",
            "default_transition": DEFAULT_TRANSITION,
        }

    def _transition_duration_ms(self, transition_after: str | None) -> int:
        mapping = {
            "none": 0,
            "hard_cut": 0,
            "punch_in": 120,
            "whoosh": 180,
            "fade": 220,
        }
        return mapping.get(self._normalized_transition(transition_after), 0)

    def _normalized_transition(self, transition_after: str | None) -> str:
        transition = str(transition_after or DEFAULT_TRANSITION).strip().lower()
        allowed = {"none", "hard_cut", "punch_in", "whoosh", "fade"}
        if transition in allowed:
            return transition
        return DEFAULT_TRANSITION

    def _text_timing(self, cut: Dict) -> Dict:
        start = float(cut.get("safe_start", cut.get("start", 0.0)))
        end = float(cut.get("safe_end", cut.get("end", 0.0)))
        duration = max(0.0, end - start)
        entry = 0.2 if duration >= 6 else 0.0
        exit_time = max(entry, duration - 0.4) if duration > 0 else 0.0
        return {
            "entry_sec": round(entry, 2),
            "exit_sec": round(exit_time, 2),
        }

    def _review_hints(self, qa_clip: Dict) -> List[str]:
        warnings = qa_clip.get("warnings", []) if isinstance(qa_clip, dict) else []
        hints: List[str] = []

        if "starts_mid_segment" in warnings:
            hints.append("check_opening_boundary")
        if "ends_mid_segment" in warnings:
            hints.append("check_closing_boundary")
        if "generic_thumbnail" in warnings:
            hints.append("refresh_thumbnail_copy")

        return hints

    def _cold_open_for_clip(
        self,
        *,
        clip_index: int,
        cut: Dict,
        cuts: List[Dict],
        post_payload: Dict,
        transcript_segments: List[Dict],
    ) -> Dict:
        if clip_index != 1:
            return {"enabled": False}

        hook_text = str(post_payload.get("hook") or cut.get("hook") or "").strip()
        if not hook_text:
            return {"enabled": False}

        best_segment = self._find_best_hook_segment(
            hook_text=hook_text,
            cuts=cuts,
            transcript_segments=transcript_segments,
        )
        if best_segment is None:
            return {"enabled": False}

        source_clip_index = int(best_segment.get("_clip_index", 1) or 1)
        source_cut = cuts[source_clip_index - 1] if 0 < source_clip_index <= len(cuts) else cut
        clip_start = float(source_cut.get("safe_start", source_cut.get("start", 0.0)) or 0.0)
        clip_end = float(source_cut.get("safe_end", source_cut.get("end", 0.0)) or 0.0)
        if clip_end <= clip_start:
            return {"enabled": False}

        segment_start = float(best_segment.get("start", clip_start))
        segment_end = float(best_segment.get("end", clip_end))
        preview_start = max(0.0, segment_start - clip_start)
        preview_duration = min(
            DEFAULT_COLD_OPEN_DURATION_SEC,
            max(0.8, segment_end - segment_start),
            max(0.8, clip_end - clip_start - preview_start),
        )

        return {
            "enabled": True,
            "source_clip_index": source_clip_index,
            "duration_sec": round(preview_duration, 2),
            "relative_start_sec": round(preview_start, 2),
            "source_text": str(best_segment.get("text") or "").strip(),
            "transition_after": "fade",
            "transition_duration_ms": 180,
        }

    def _find_best_hook_segment(
        self,
        *,
        hook_text: str,
        cuts: List[Dict],
        transcript_segments: List[Dict],
    ) -> Dict | None:
        hook_tokens = set(self._tokenize(hook_text))
        if not hook_tokens:
            return None

        best_segment = None
        best_score = 0.0

        for cut_index, cut in enumerate(cuts, start=1):
            clip_start = float(cut.get("safe_start", cut.get("start", 0.0)) or 0.0)
            clip_end = float(cut.get("safe_end", cut.get("end", 0.0)) or 0.0)
            if clip_end <= clip_start:
                continue

            for segment in transcript_segments:
                segment_start = float(segment.get("start", 0.0))
                segment_end = float(segment.get("end", 0.0))
                if segment_end < clip_start or segment_start > clip_end:
                    continue

                segment_tokens = set(self._tokenize(str(segment.get("text") or "")))
                if not segment_tokens:
                    continue

                overlap = hook_tokens & segment_tokens
                if not overlap:
                    continue

                score = len(overlap) / max(1, len(hook_tokens))
                if score > best_score:
                    best_score = score
                    best_segment = {
                        **segment,
                        "_clip_index": cut_index,
                    }

        return best_segment

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r"\w+", text.lower())
