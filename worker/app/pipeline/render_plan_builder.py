import re
from typing import Dict, List

from app.pipeline.presets import resolve_clip_preset


DEFAULT_TRANSITION = "hard_cut"
DEFAULT_CAPTION_STYLE = "clean_subtitles"
DEFAULT_COLD_OPEN_DURATION_SEC = 2.0
DEFAULT_COLD_OPEN_MIN_DURATION_SEC = 3.1
DEFAULT_COLD_OPEN_MAX_DURATION_SEC = 7.2
DEFAULT_COLD_OPEN_LEAD_IN_SEC = 0.02
DEFAULT_COLD_OPEN_TAIL_SEC = 1.0


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
        preset = resolve_clip_preset(clip_mode, video_ratio)
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
            transition_after = self._normalized_transition(
                cut.get("transition_after"),
                transition_profile=preset.transition_profile,
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
                    "transition_after": transition_after,
                    "transition_duration_ms": self._transition_duration_ms(
                        transition_after,
                        transition_profile=preset.transition_profile,
                    ),
                    "caption_style": cut.get("caption_style") or preset.caption_style,
                    "playback_speed": preset.render_playback_speed,
                    "visual_filter_profile": preset.visual_filter_profile,
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
            "clip_mode": preset.clip_mode,
            "video_ratio": preset.video_ratio,
            "preset_id": preset.preset_id,
            "render_intent": preset.render_intent,
            "global_style": self._global_style(preset.video_ratio, preset=preset),
            "playback_speed": preset.render_playback_speed,
            "visual_filter_profile": preset.visual_filter_profile,
            "transition_profile": preset.transition_profile,
            "soundtrack": soundtrack or {},
            "clips": clips,
        }

    def _global_style(self, video_ratio: str, *, preset) -> Dict:
        if video_ratio == "portrait":
            return {
                "aspect_ratio": "9:16",
                "caption_position": "lower_third_center",
                "hook_text_treatment": "bold_punchy",
                "default_transition": DEFAULT_TRANSITION,
                "caption_style": preset.caption_style,
            }

        return {
            "aspect_ratio": "16:9",
            "caption_position": "bottom_center" if not preset.is_long_form else "safe_lower_third",
            "hook_text_treatment": "clean",
            "default_transition": "fade" if preset.is_long_form else DEFAULT_TRANSITION,
            "caption_style": preset.caption_style,
        }

    def _transition_duration_ms(self, transition_after: str | None, *, transition_profile: str) -> int:
        transition = str(transition_after or "").strip().lower()
        if transition_profile == "long_editorial":
            mapping = {
                "none": 0,
                "hard_cut": 0,
                "punch_in": 0,
                "whoosh": 0,
                "fade": 180,
            }
            return mapping.get(transition, 0)

        mapping = {
            "none": 0,
            "hard_cut": 0,
            "punch_in": 180,
            "whoosh": 240,
            "fade": 320,
        }
        return mapping.get(transition, 0)

    def _normalized_transition(self, transition_after: str | None, *, transition_profile: str) -> str:
        transition = str(transition_after or DEFAULT_TRANSITION).strip().lower()
        allowed = {"none", "hard_cut", "punch_in", "whoosh", "fade"}
        if transition in allowed:
            if transition_profile == "long_editorial":
                if transition in {"punch_in", "whoosh"}:
                    return "fade"
                return transition
            if transition in {"none", "hard_cut"}:
                return "fade"
            return transition
        return "fade" if transition_profile == "long_editorial" else DEFAULT_TRANSITION

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

        preferred_cut_index = self._preferred_hook_cut_index(post_payload, cuts)
        explicit_hook_start = self._coerce_optional_float(post_payload.get("hook_start"))
        explicit_hook_end = self._coerce_optional_float(post_payload.get("hook_end"))
        if explicit_hook_start is not None and explicit_hook_end is not None:
            explicit_preview = self._build_explicit_hook_preview_window(
                hook_text=hook_text,
                cuts=cuts,
                clip_index=preferred_cut_index or 1,
                hook_start=explicit_hook_start,
                hook_end=explicit_hook_end,
            )
            if explicit_preview is not None:
                return {
                    "enabled": True,
                    "source_clip_index": explicit_preview["source_clip_index"],
                    "duration_sec": round(explicit_preview["duration_sec"], 2),
                    "relative_start_sec": round(explicit_preview["relative_start_sec"], 2),
                    "source_text": explicit_preview["source_text"],
                    "transition_after": "fade",
                    "transition_duration_ms": 90,
                }

        best_segment = self._find_best_hook_segment(
            hook_text=hook_text,
            cuts=cuts,
            transcript_segments=transcript_segments,
            preferred_cut_index=preferred_cut_index,
        )
        if best_segment is None:
            return {"enabled": False}

        source_clip_index = int(best_segment.get("_clip_index", 1) or 1)
        source_cut = cuts[source_clip_index - 1] if 0 < source_clip_index <= len(cuts) else cut
        clip_start = float(source_cut.get("safe_start", source_cut.get("start", 0.0)) or 0.0)
        clip_end = float(source_cut.get("safe_end", source_cut.get("end", 0.0)) or 0.0)
        if clip_end <= clip_start:
            return {"enabled": False}

        preview = self._build_hook_preview_window(
            best_segment=best_segment,
            transcript_segments=transcript_segments,
            clip_start=clip_start,
            clip_end=clip_end,
            source_clip_index=source_clip_index,
        )
        if preview is None:
            return {"enabled": False}

        return {
            "enabled": True,
            "source_clip_index": source_clip_index,
            "duration_sec": round(preview["duration_sec"], 2),
            "relative_start_sec": round(preview["relative_start_sec"], 2),
            "source_text": preview["source_text"],
            "transition_after": "fade",
            "transition_duration_ms": 90,
        }

    def _find_best_hook_segment(
        self,
        *,
        hook_text: str,
        cuts: List[Dict],
        transcript_segments: List[Dict],
        preferred_cut_index: int | None = None,
    ) -> Dict | None:
        hook_tokens = set(self._tokenize(hook_text))
        if not hook_tokens:
            return None

        best_segment = None
        best_score = 0.0

        for cut_index, cut in enumerate(cuts, start=1):
            if preferred_cut_index is not None and cut_index != preferred_cut_index:
                continue
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

    def _preferred_hook_cut_index(self, post_payload: Dict, cuts: List[Dict]) -> int | None:
        raw_index = post_payload.get("hook_source_cut_index")
        if raw_index in (None, ""):
            return 1

        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return 1

        if 0 <= index < len(cuts):
            return index + 1
        if 1 <= index <= len(cuts):
            return index
        return 1

    def _build_explicit_hook_preview_window(
        self,
        *,
        hook_text: str,
        cuts: List[Dict],
        clip_index: int,
        hook_start: float,
        hook_end: float,
    ) -> Dict | None:
        if clip_index < 1 or clip_index > len(cuts):
            return None

        source_cut = cuts[clip_index - 1]
        clip_start = float(source_cut.get("safe_start", source_cut.get("start", 0.0)) or 0.0)
        clip_end = float(source_cut.get("safe_end", source_cut.get("end", 0.0)) or 0.0)
        if clip_end <= clip_start:
            return None
        if hook_start > clip_end or hook_end < clip_start:
            return None

        # When the LLM gives us an explicit hook window, trust its beginning and
        # avoid adding extra setup before the phrase.
        absolute_start = max(clip_start, hook_start)
        absolute_end = min(
            clip_end,
            max(
                hook_end + 0.18,
                absolute_start + 1.1,
            ),
        )
        duration_sec = max(0.0, absolute_end - absolute_start)
        if duration_sec < 0.8:
            return None

        return {
            "source_clip_index": clip_index,
            "relative_start_sec": max(0.0, absolute_start - clip_start),
            "duration_sec": min(duration_sec, DEFAULT_COLD_OPEN_MAX_DURATION_SEC),
            "source_text": hook_text,
        }

    def _build_hook_preview_window(
        self,
        *,
        best_segment: Dict,
        transcript_segments: List[Dict],
        clip_start: float,
        clip_end: float,
        source_clip_index: int,
    ) -> Dict | None:
        if clip_end <= clip_start:
            return None

        source_segments = [
            segment
            for segment in transcript_segments
            if int(segment.get("_clip_index", source_clip_index) or source_clip_index) == source_clip_index
            and float(segment.get("end", 0.0)) >= clip_start
            and float(segment.get("start", 0.0)) <= clip_end
        ]
        source_segments.sort(key=lambda item: float(item.get("start", 0.0)))
        if not source_segments:
            return None

        best_start = float(best_segment.get("start", clip_start))
        best_end = float(best_segment.get("end", clip_end))
        best_index = next(
            (
                index
                for index, segment in enumerate(source_segments)
                if float(segment.get("start", 0.0)) == best_start
                and float(segment.get("end", 0.0)) == best_end
            ),
            0,
        )
        start_segment = source_segments[best_index]
        absolute_start = max(clip_start, float(start_segment.get("start", clip_start)) - DEFAULT_COLD_OPEN_LEAD_IN_SEC)
        absolute_end = min(
            clip_end,
            max(
                float(start_segment.get("end", clip_start)) + DEFAULT_COLD_OPEN_TAIL_SEC,
                absolute_start + DEFAULT_COLD_OPEN_MIN_DURATION_SEC,
            ),
        )
        preview_text_parts = [str(start_segment.get("text") or "").strip()]

        for segment in source_segments[best_index + 1 :]:
            current_duration = absolute_end - absolute_start
            if current_duration >= DEFAULT_COLD_OPEN_MAX_DURATION_SEC:
                break

            segment_end = float(segment.get("end", absolute_end))
            preview_text_parts.append(str(segment.get("text") or "").strip())
            absolute_end = min(
                clip_end,
                max(segment_end + DEFAULT_COLD_OPEN_TAIL_SEC, absolute_end),
            )
            current_duration = absolute_end - absolute_start

            joined_text = " ".join(part for part in preview_text_parts if part).strip()
            if current_duration >= DEFAULT_COLD_OPEN_MIN_DURATION_SEC and self._looks_like_sentence_closure(joined_text):
                break

        duration_sec = max(0.0, absolute_end - absolute_start)
        if duration_sec < 1.8:
            return None

        return {
            "relative_start_sec": max(0.0, absolute_start - clip_start),
            "duration_sec": min(duration_sec, DEFAULT_COLD_OPEN_MAX_DURATION_SEC),
            "source_text": " ".join(part for part in preview_text_parts if part).strip(),
        }

    def _looks_like_sentence_closure(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        return bool(re.search(r"[.!?…][\"')\\]]?$", stripped))

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r"\w+", text.lower())

    def _coerce_optional_float(self, value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
