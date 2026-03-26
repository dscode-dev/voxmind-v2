from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Dict, List


SENTENCE_CLOSURE_PATTERN = re.compile(r"[.!?…][\"')\]]?$")
BAD_START_PATTERN = re.compile(r"^(e|mas|ent[aã]o|porque)\b", re.IGNORECASE)
HOOK_PATTERN = re.compile(r"\?|ningu[eé]m|nunca|verdade|absurdo|segredo|por que", re.IGNORECASE)
BRIDGE_PATTERN = re.compile(r"\bporque\b|\bpor isso\b|\bmas\b|\bent[aã]o\b", re.IGNORECASE)


class ClipsAICandidateProvider:

    def __init__(
        self,
        *,
        enabled: bool,
        device: str,
        max_candidates: int,
        min_duration_sec: float,
        max_duration_sec: float,
        window_size_sec: int = 180,
    ):
        self.enabled = enabled
        self.device = device
        self.max_candidates = max_candidates
        self.min_duration_sec = min_duration_sec
        self.max_duration_sec = max_duration_sec
        self.window_size_sec = window_size_sec

    def build(self, transcript_segments: List[Dict]) -> tuple[List[Dict], Dict]:
        diagnostics: Dict[str, Any] = {
            "enabled": self.enabled,
            "requested_device": self.device,
            "available": False,
            "resolved_device": "cpu",
            "candidate_count": 0,
            "reason": "disabled",
        }
        if not self.enabled:
            return [], diagnostics

        if not transcript_segments:
            diagnostics["reason"] = "empty_transcript"
            return [], diagnostics

        try:
            classes = self._resolve_clipsai_classes()
            transcription = self._build_transcription(classes, transcript_segments)
            clipfinder = self._instantiate_clipfinder(classes["ClipFinder"])
            clips = clipfinder.find_clips(transcription=transcription)
            candidates = self._normalize_clips(clips, transcript_segments)
            diagnostics.update(
                {
                    "available": True,
                    "resolved_device": self._resolve_device_label(clipfinder),
                    "candidate_count": len(candidates),
                    "reason": "completed",
                }
            )
            return candidates, diagnostics
        except Exception as exc:
            diagnostics["reason"] = f"{exc.__class__.__name__}:{exc}"
            return [], diagnostics

    def _resolve_clipsai_classes(self) -> Dict[str, Any]:
        root = import_module("clipsai")
        return {
            "ClipFinder": self._resolve_symbol(root, "ClipFinder", ["clipsai.clip.clipfinder"]),
            "Transcription": self._resolve_symbol(root, "Transcription", ["clipsai.transcribe.transcription"]),
            "Sentence": self._resolve_symbol(root, "Sentence", ["clipsai.transcribe.transcription"]),
            "Word": self._resolve_symbol(root, "Word", ["clipsai.transcribe.transcription"]),
            "Character": self._resolve_symbol(root, "Character", ["clipsai.transcribe.transcription"]),
        }

    def _resolve_symbol(self, root: Any, name: str, module_paths: List[str]) -> Any:
        symbol = getattr(root, name, None)
        if symbol is not None:
            return symbol

        for module_path in module_paths:
            try:
                module = import_module(module_path)
            except Exception:
                continue
            symbol = getattr(module, name, None)
            if symbol is not None:
                return symbol

        raise ImportError(f"clipsai symbol not found: {name}")

    def _instantiate_clipfinder(self, clipfinder_cls: Any) -> Any:
        signature = inspect.signature(clipfinder_cls)
        kwargs: Dict[str, Any] = {}
        for parameter_name in signature.parameters.keys():
            lowered = parameter_name.lower()
            if lowered in {"device", "compute_device", "torch_device"}:
                kwargs[parameter_name] = self.device
        return clipfinder_cls(**kwargs)

    def _build_transcription(self, classes: Dict[str, Any], transcript_segments: List[Dict]) -> Any:
        words: List[Any] = []
        sentences: List[Any] = []
        characters: List[Any] = []
        full_text_parts: List[str] = []
        current_char = 0

        for sentence_index, segment in enumerate(transcript_segments):
            text = str(segment.get("text") or "").strip()
            if not text:
                continue

            if full_text_parts:
                full_text_parts.append(" ")
                current_char += 1

            sentence_start_char = current_char
            full_text_parts.append(text)
            sentence_start = float(segment.get("start", 0.0))
            sentence_end = float(segment.get("end", sentence_start))
            sentence_end_char = sentence_start_char + len(text) - 1
            if sentence_end_char < sentence_start_char:
                continue

            sentences.append(
                self._instantiate_object(
                    classes["Sentence"],
                    {
                        "start_time": sentence_start,
                        "end_time": sentence_end,
                        "start_char": sentence_start_char,
                        "end_char": sentence_end_char,
                        "text": text,
                    },
                )
            )

            current_char = sentence_end_char + 1
            segment_words = text.split()
            word_timings = self._approximate_word_timings(sentence_start, sentence_end, segment_words)
            local_char = sentence_start_char

            for word_index, (word_text, word_start, word_end) in enumerate(word_timings, start=len(words)):
                word_start_char = local_char
                word_end_char = word_start_char + len(word_text) - 1
                words.append(
                    self._instantiate_object(
                        classes["Word"],
                        {
                            "start_time": word_start,
                            "end_time": word_end,
                            "start_char": word_start_char,
                            "end_char": word_end_char,
                            "text": word_text,
                        },
                    )
                )
                for character_offset, character in enumerate(word_text):
                    characters.append(
                        self._instantiate_object(
                            classes["Character"],
                            {
                                "start_time": word_start,
                                "end_time": word_end,
                                "word_index": word_index,
                                "sentence_index": sentence_index,
                                "text": character,
                            },
                        )
                    )
                    character_offset += 0
                local_char = word_end_char + 2

        full_text = "".join(full_text_parts).strip()
        if not full_text:
            raise ValueError("empty_clipsai_transcription")

        transcription_payload = {
            "text": full_text,
            "language": "pt",
            "created_time": datetime.now(timezone.utc),
            "start_time": float(transcript_segments[0].get("start", 0.0)),
            "end_time": float(transcript_segments[-1].get("end", 0.0)),
            "source_software": "voxmind-faster-whisper",
            "characters": characters,
            "words": words,
            "sentences": sentences,
        }
        return self._instantiate_object(classes["Transcription"], transcription_payload)

    def _instantiate_object(self, cls: Any, payload: Dict[str, Any]) -> Any:
        if cls is None:
            return payload

        try:
            return cls(**payload)
        except Exception:
            try:
                signature = inspect.signature(cls)
                ordered_kwargs = {
                    name: payload[name]
                    for name in signature.parameters.keys()
                    if name in payload
                }
                return cls(**ordered_kwargs)
            except Exception:
                instance = cls.__new__(cls)
                for key, value in payload.items():
                    try:
                        setattr(instance, key, value)
                    except Exception:
                        continue
                return instance

    def _approximate_word_timings(
        self,
        start_time: float,
        end_time: float,
        words: List[str],
    ) -> List[tuple[str, float, float]]:
        if not words:
            return []

        duration = max(end_time - start_time, 0.01)
        step = duration / max(len(words), 1)
        timings: List[tuple[str, float, float]] = []
        for index, word in enumerate(words):
            word_start = start_time + (step * index)
            word_end = min(end_time, word_start + step)
            timings.append((word, round(word_start, 3), round(word_end, 3)))
        return timings

    def _normalize_clips(self, clips: List[Any], transcript_segments: List[Dict]) -> List[Dict]:
        candidates: List[Dict] = []
        for index, clip in enumerate(clips[: self.max_candidates]):
            start = self._get_clip_value(clip, "start_time", 0.0)
            end = self._get_clip_value(clip, "end_time", start)
            duration = max(end - start, 0.0)
            if duration < self.min_duration_sec or duration > self.max_duration_sec:
                continue

            overlapped_segments = [
                segment
                for segment in transcript_segments
                if float(segment.get("end", 0.0)) >= start and float(segment.get("start", 0.0)) <= end
            ]
            text = self._extract_clip_text(clip, overlapped_segments)
            if not text.strip():
                continue

            speakers = sorted(
                {
                    str(segment.get("speaker", "UNKNOWN"))
                    for segment in overlapped_segments
                    if str(segment.get("speaker", "")).strip()
                }
            )
            has_clean_start = not BAD_START_PATTERN.search(text.strip())
            has_strong_ending = bool(SENTENCE_CLOSURE_PATTERN.search(text.strip()))
            narrative_completeness = 1.4 if has_clean_start else 0.0
            narrative_completeness += 2.2 if has_strong_ending else 0.0
            if BRIDGE_PATTERN.search(text):
                narrative_completeness += 0.9

            hook_score = 2.0 if HOOK_PATTERN.search(text) else 0.0
            density_bonus = min(len(text.split()) / max(duration, 1.0), 4.0) * 0.45
            clipsai_bonus = 3.5
            total_score = hook_score + narrative_completeness + density_bonus + clipsai_bonus
            window_index = int(start // self.window_size_sec)
            narrative_role = "hook" if hook_score > 0 else "development"
            if has_strong_ending:
                narrative_role = "payoff"

            candidates.append(
                {
                    "candidate_id": f"clipsai_{index:04d}",
                    "source": "clipsai",
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "duration": round(duration, 2),
                    "text": text.strip(),
                    "hook_score": hook_score,
                    "narrative_score": narrative_completeness + clipsai_bonus,
                    "emotional_score": 0.0,
                    "audio_score": 0.0,
                    "boundary_score": (1.5 if has_clean_start else 0.0) + (1.8 if has_strong_ending else 0.0),
                    "speaker_score": 1.5 if len(speakers) <= 1 else 0.5,
                    "narrative_completeness_score": narrative_completeness,
                    "dialogue_penalty": 0.0,
                    "title_signal_score": 0.9 if hook_score > 0 else 0.0,
                    "retention_score": 1.8 if hook_score > 0 else 0.6,
                    "duration_penalty": 0.0,
                    "density_bonus": round(density_bonus, 3),
                    "raw_score": round(total_score, 3),
                    "total_score": round(total_score, 3),
                    "window_index": window_index,
                    "speaker_count": len(speakers),
                    "speakers": speakers,
                    "segment_count": len(overlapped_segments),
                    "start_segment_index": None,
                    "end_segment_index": None,
                    "narrative_role": narrative_role,
                    "editorial_signals": {
                        "clean_start": has_clean_start,
                        "strong_ending": has_strong_ending,
                        "retention_score": 1.8 if hook_score > 0 else 0.6,
                        "title_signal_score": 0.9 if hook_score > 0 else 0.0,
                        "narrative_completeness_score": narrative_completeness,
                        "dialogue_penalty": 0.0,
                        "source": "clipsai",
                    },
                }
            )

        return candidates

    def _extract_clip_text(self, clip: Any, overlapped_segments: List[Dict]) -> str:
        return " ".join(str(segment.get("text", "")).strip() for segment in overlapped_segments).strip()

    def _get_clip_value(self, clip: Any, field_name: str, default: Any = None) -> Any:
        if isinstance(clip, dict):
            return clip.get(field_name, default)
        return getattr(clip, field_name, default)

    def _resolve_device_label(self, clipfinder: Any) -> str:
        for field_name in ("device", "compute_device", "torch_device"):
            value = getattr(clipfinder, field_name, None)
            if value:
                return str(value)
        return self.device
