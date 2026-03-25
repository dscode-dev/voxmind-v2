from typing import Dict, List


DEFAULT_TRANSITION = "hard_cut"
DEFAULT_CAPTION_STYLE = "clean_subtitles"


class RenderPlanBuilder:

    def build(
        self,
        *,
        job_id: str,
        clip_mode: str,
        video_ratio: str,
        cuts: List[Dict],
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
                    "on_screen_text": cut.get("on_screen_text") or "",
                    "emphasis_words": cut.get("emphasis_words", []),
                    "text_timing": self._text_timing(cut),
                    "hook": cut.get("hook"),
                    "title": cut.get("title"),
                    "review_hints": self._review_hints(qa_clip),
                }
            )

        return {
            "job_id": job_id,
            "clip_mode": clip_mode,
            "video_ratio": video_ratio,
            "render_intent": "social_ready_short_form",
            "global_style": self._global_style(video_ratio),
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
