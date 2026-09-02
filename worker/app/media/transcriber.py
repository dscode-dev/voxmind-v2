import hashlib
import json
import re
import gc
from pathlib import Path
from typing import Any, List, Dict

from faster_whisper import WhisperModel

from app.media.asr_windows import AsrWindow, build_windows, overlap_overhead
from app.media.seam_reconciler import SeamPolicy, normalize_for_match, reconcile_windows
from app.observability import get_logger
from app.runtime.subprocess_runner import run_ffmpeg, run_ffprobe

logger = get_logger(__name__)

#: Bumped whenever windowing, seam reconciliation or normalization changes, so a checkpoint
#: or transcript cache produced by an older algorithm is never mistaken for a current one.
ASR_PIPELINE_VERSION = 2


class Transcriber:

    def __init__(
        self,
        model_size: str,
        device: str,
        compute_type: str,
        cpu_threads: int,
        language: str,
        beam_size: int,
        vad_filter: bool,
        segment_duration_sec: int = 600,
        parallel_workers: int = 2,
        max_merged_segment_duration_sec: int = 18,
        fallback_to_cpu_on_oom: bool = True,
        fallback_model_sizes: list[str] | None = None,
        preloaded_models_dir: str | None = None,
        job_id: str | None = None,
        window_overlap_sec: float = 0.0,
        word_timestamps: bool = False,
        seam_policy: SeamPolicy | None = None,
        strip_fillers: bool = False,
    ):
        self.job_id = job_id
        self.model_size = model_size
        self._requested_model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.parallel_workers = parallel_workers
        self.model = None
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.segment_duration_sec = segment_duration_sec
        self.window_overlap_sec = float(window_overlap_sec)
        self.word_timestamps = bool(word_timestamps)
        self.seam_policy = seam_policy or SeamPolicy()
        self.strip_fillers = bool(strip_fillers)
        self.max_merged_segment_duration_sec = max_merged_segment_duration_sec
        self.fallback_to_cpu_on_oom = fallback_to_cpu_on_oom
        self.fallback_model_sizes = [item for item in (fallback_model_sizes or []) if item]
        self.preloaded_models_dir = Path(preloaded_models_dir).expanduser() if preloaded_models_dir else None
        self.last_transcription_info: Dict = {
            "requested_language": language,
            "detected_language": None,
            "language_probability": None,
            "device": device,
            "compute_type": compute_type,
            "model_size": model_size,
        }
        #: Per-window language observations, aggregated at the end of a run.
        self._language_observations: List[Dict[str, Any]] = []
        #: Windowing/seam statistics for the logs and the evaluation harness.
        self.last_run_stats: Dict[str, Any] = {}

    def transcribe(self, video_path: Path) -> List[Dict]:
        try:
            self._ensure_model()
            return self._transcribe_with_current_model(video_path)
        except Exception as exc:
            if not self._should_fallback_to_cpu(exc):
                raise

            self._fallback_to_cpu()
            self._ensure_model()
            return self._transcribe_with_current_model(video_path)

    def _transcribe_with_current_model(self, video_path: Path) -> List[Dict]:

        audio_dir = video_path.parent / "audio_chunks"
        transcript_dir = video_path.parent / "transcripts"

        audio_dir.mkdir(parents=True, exist_ok=True)
        transcript_dir.mkdir(parents=True, exist_ok=True)

        duration = self._probe_duration(video_path)
        windows = build_windows(duration, self.segment_duration_sec, self.window_overlap_sec)
        config_hash = self.config_hash()

        self._language_observations = []
        window_segments: List[tuple] = []

        for window in windows:
            chunk_file = self._extract_window_audio(video_path, audio_dir, window)
            part_file = transcript_dir / f"part_{window.index:03d}.json"

            cached = self._load_checkpoint(part_file, window, config_hash)
            if cached is not None:
                segments = cached["segments"]
                for observation in cached.get("language_observations") or []:
                    self._language_observations.append(observation)
            else:
                segments = self._transcribe_window(chunk_file, window)
                self._write_checkpoint(part_file, window, config_hash, segments)

            window_segments.append((window, segments))

        reconciled, seam_stats = reconcile_windows(window_segments, self.seam_policy)
        merged = self._merge_small_gaps(reconciled)

        self.last_transcription_info = self._aggregate_language_metadata()
        self.last_run_stats = {
            **overlap_overhead(windows, duration),
            **seam_stats.as_dict(),
            "segments_final": len(merged),
            "asr_pipeline_version": ASR_PIPELINE_VERSION,
        }
        logger.info(
            "ASR windows reconciled",
            extra={
                "job_id": self.job_id,
                "step": "transcribe",
                "status": "reconciled",
                "windows": self.last_run_stats.get("window_count"),
                "seams": self.last_run_stats.get("seams"),
                "duplicates_removed": self.last_run_stats.get("duplicates_removed"),
                "overhead_ratio": self.last_run_stats.get("overhead_ratio"),
            },
        )

        return merged

    # ------------------------------------------------------------------ windows

    def _extract_window_audio(self, video_path: Path, audio_dir: Path, window: AsrWindow) -> Path:
        chunk_file = audio_dir / f"chunk_{window.index:03d}.wav"
        if chunk_file.exists():
            return chunk_file

        command = [
            "ffmpeg",
            "-y",
            "-ss",
            str(window.start),
            "-i",
            str(video_path),
            "-t",
            str(window.duration),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(chunk_file),
        ]
        run_ffmpeg(command, step="extract_audio_chunk", job_id=self.job_id)
        return chunk_file

    def _transcribe_window(self, chunk_file: Path, window: AsrWindow) -> List[Dict]:
        """Transcribe one window and return segments in ABSOLUTE video time."""
        requested_language = self._requested_language_for_model()
        segments, info = self.model.transcribe(
            str(chunk_file),
            language=requested_language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            word_timestamps=self.word_timestamps,
        )

        window_segments: List[Dict] = []
        for segment in segments:
            raw_text = (segment.text or "").strip()
            text = self._clean_text(raw_text)
            if not text:
                continue

            record: Dict[str, Any] = {
                # Window-relative -> absolute. The only place this conversion happens.
                "start": window.to_absolute(float(segment.start)),
                "end": window.to_absolute(float(segment.end)),
                "text": text,
                "window_index": window.index,
            }
            if self.strip_fillers:
                # The aggressive form sits alongside, never instead of, what was said.
                record["normalized_text"] = self._strip_fillers(text)

            words = getattr(segment, "words", None)
            if self.word_timestamps and words:
                record["words"] = [
                    {
                        "start": window.to_absolute(float(word.start)),
                        "end": window.to_absolute(float(word.end)),
                        "word": word.word,
                    }
                    for word in words
                    if word.start is not None and word.end is not None
                ]

            window_segments.append(record)

        self._language_observations.append(self._language_observation(info, window))
        return window_segments

    # -------------------------------------------------------------- checkpoints

    def config_hash(self) -> str:
        """Identity of the algorithm that produced a checkpoint or cache entry."""
        payload = {
            "asr_pipeline_version": ASR_PIPELINE_VERSION,
            "model_size": self.model_size,
            "language": self.language,
            "beam_size": self.beam_size,
            "vad_filter": self.vad_filter,
            "window_sec": self.segment_duration_sec,
            "overlap_sec": self.window_overlap_sec,
            "word_timestamps": self.word_timestamps,
            "strip_fillers": self.strip_fillers,
            "seam_policy": {
                "min_temporal_iou": self.seam_policy.min_temporal_iou,
                "min_text_similarity": self.seam_policy.min_text_similarity,
                "strong_text_similarity": self.seam_policy.strong_text_similarity,
            },
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]

    def _load_checkpoint(self, part_file: Path, window: AsrWindow, config_hash: str):
        """Reuse a window checkpoint only when this exact algorithm produced it.

        Checkpoints written before this PR are bare JSON lists with no window metadata. They
        came from a different (non-overlapping) layout, so their offsets do not match the
        current windows. They are recognised and discarded rather than silently reused.
        """
        if not part_file.exists():
            return None

        try:
            payload = json.loads(part_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            logger.warning(
                "Unreadable ASR checkpoint; re-transcribing this window",
                extra={"job_id": self.job_id, "step": "transcribe",
                       "status": "checkpoint_invalid", "window_index": window.index},
            )
            return None

        if isinstance(payload, list):
            logger.warning(
                "Legacy ASR checkpoint without window metadata; re-transcribing this window",
                extra={"job_id": self.job_id, "step": "transcribe",
                       "status": "checkpoint_legacy", "window_index": window.index},
            )
            return None

        if not isinstance(payload, dict):
            return None

        if payload.get("config_hash") != config_hash:
            logger.info(
                "ASR checkpoint came from a different configuration; re-transcribing",
                extra={"job_id": self.job_id, "step": "transcribe",
                       "status": "checkpoint_stale", "window_index": window.index},
            )
            return None

        stored_window = payload.get("window") or {}
        try:
            same_range = (
                abs(float(stored_window.get("start")) - window.start) <= 0.001
                and abs(float(stored_window.get("end")) - window.end) <= 0.001
            )
        except (TypeError, ValueError):
            same_range = False

        if not same_range:
            logger.info(
                "ASR checkpoint covers a different window range; re-transcribing",
                extra={"job_id": self.job_id, "step": "transcribe",
                       "status": "checkpoint_mismatch", "window_index": window.index},
            )
            return None

        return payload

    def _write_checkpoint(
        self,
        part_file: Path,
        window: AsrWindow,
        config_hash: str,
        segments: List[Dict],
    ) -> None:
        payload = {
            "asr_pipeline_version": ASR_PIPELINE_VERSION,
            "config_hash": config_hash,
            "window": window.as_dict(),
            "segments": segments,
            "language_observations": self._language_observations[-1:],
        }
        part_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ---------------------------------------------------------------- language

    def _language_observation(self, info: object, window: AsrWindow) -> Dict[str, Any]:
        detected = getattr(info, "language", None)
        probability = getattr(info, "language_probability", None)
        return {
            "window_index": window.index,
            "duration_sec": round(window.duration, 3),
            "language": str(detected).strip().lower() if detected else None,
            "probability": float(probability) if probability is not None else None,
        }

    def _aggregate_language_metadata(self) -> Dict[str, Any]:
        """Consolidate per-window detections into one decision.

        ``last_transcription_info`` used to be overwritten on every window, so a job's
        language was whatever the LAST window happened to detect — one misdetected closing
        window relabelled the whole video.

        Windows are weighted by audio duration times detection confidence: a long, confident
        window outvotes a short, uncertain one. Confidence defaults to 1.0 when the model
        reports none, which degrades the rule to duration-weighted voting.
        """
        requested = self._requested_language_for_model() or "auto"
        base: Dict[str, Any] = {
            "requested_language": requested,
            "detected_language": None,
            "language_probability": None,
            "device": self.device,
            "compute_type": self.compute_type,
            "model_size": self.model_size,
            "language_observations": list(self._language_observations),
        }

        weights: Dict[str, float] = {}
        confidence_sum: Dict[str, float] = {}
        counts: Dict[str, int] = {}

        for observation in self._language_observations:
            language = observation.get("language")
            if not language:
                continue
            probability = observation.get("probability")
            confidence = 1.0 if probability is None else float(probability)
            weight = max(0.0, float(observation.get("duration_sec") or 0.0)) * confidence
            weights[language] = weights.get(language, 0.0) + weight
            confidence_sum[language] = confidence_sum.get(language, 0.0) + confidence
            counts[language] = counts.get(language, 0) + 1

        if not weights:
            return base

        winner = max(weights, key=lambda lang: (weights[lang], counts[lang]))
        base["detected_language"] = winner
        base["language_probability"] = round(confidence_sum[winner] / counts[winner], 4)
        base["language_agreement"] = round(weights[winner] / sum(weights.values()), 4)
        return base

    def _ensure_model(self) -> None:
        if self.model is not None:
            return

        if self._cuda_requested_but_unavailable():
            self._fallback_to_cpu()

        last_exc: Exception | None = None
        for candidate_model, model_ref in self._candidate_model_refs():
            try:
                self.model = WhisperModel(
                    model_ref,
                    device=self.device,
                    compute_type=self.compute_type,
                    cpu_threads=self.cpu_threads,
                    num_workers=self.parallel_workers,
                )
                self.model_size = candidate_model
                self.last_transcription_info["device"] = self.device
                self.last_transcription_info["compute_type"] = self.compute_type
                self.last_transcription_info["model_size"] = candidate_model
                return
            except Exception as exc:
                last_exc = exc
                if not self._is_model_resolution_error(exc):
                    raise
                continue

        if last_exc is not None:
            raise RuntimeError(
                "Unable to load any ASR model from local cache or Hub. "
                f"Requested={self._requested_model_size}; tried={','.join(self._candidate_model_sizes())}; "
                f"last_error={last_exc}"
            ) from last_exc

    def _requested_language_for_model(self) -> str | None:
        language = str(self.language or "").strip().lower()
        if language in {"", "auto", "source"}:
            return None
        return language

    def _update_transcription_info(self, info: object, requested_language: str | None) -> None:
        detected_language = getattr(info, "language", None)
        language_probability = getattr(info, "language_probability", None)
        self.last_transcription_info = {
            "requested_language": requested_language or "auto",
            "detected_language": str(detected_language).strip().lower() if detected_language else None,
            "language_probability": float(language_probability) if language_probability is not None else None,
            "device": self.device,
            "compute_type": self.compute_type,
            "model_size": self.model_size,
        }

    def _candidate_model_sizes(self) -> list[str]:
        ordered: list[str] = []
        for item in [self._requested_model_size, *self.fallback_model_sizes]:
            name = str(item or "").strip()
            if not name or name in ordered:
                continue
            ordered.append(name)
        return ordered

    def _candidate_model_refs(self) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        for model_size in self._candidate_model_sizes():
            local_dir = self._preloaded_model_dir(model_size)
            if local_dir is not None:
                refs.append((model_size, str(local_dir)))
            refs.append((model_size, model_size))
        return refs

    def _preloaded_model_dir(self, model_size: str) -> Path | None:
        if self.preloaded_models_dir is None:
            return None
        candidate = self.preloaded_models_dir / model_size
        if not candidate.exists() or not candidate.is_dir():
            return None
        return candidate

    def _is_model_resolution_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "snapshot folder" in message
            or "trying to locate the files on the hub" in message
            or "internet connection" in message
            or "cannot find the appropriate snapshot" in message
            or "model.bin" in message
            or "does not exist locally" in message
            or "huggingface" in message
            or "hub" in message and "snapshot" in message
        )

    def _should_fallback_to_cpu(self, exc: Exception) -> bool:
        if not self.fallback_to_cpu_on_oom:
            return False
        if str(self.device).strip().lower() != "cuda":
            return False
        message = str(exc).lower()
        return (
            ("cuda" in message and "out of memory" in message)
            or "no cuda-capable device is detected" in message
            or "cuda-capable device" in message
            or "no cuda devices are available" in message
            or "cuda driver version is insufficient" in message
            or "found no nvidia driver" in message
        )

    def _cuda_requested_but_unavailable(self) -> bool:
        if str(self.device).strip().lower() != "cuda":
            return False
        try:
            import torch

            return not bool(torch.cuda.is_available())
        except Exception:
            return True

    def _fallback_to_cpu(self) -> None:
        self.model = None
        self.device = "cpu"
        self.compute_type = "int8"
        self.parallel_workers = max(1, min(self.parallel_workers, 2))
        self._clear_cuda_memory()

    def _clear_cuda_memory(self) -> None:
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def release_resources(self) -> None:
        self.model = None
        self._clear_cuda_memory()

    def _probe_duration(self, video_path: Path) -> float:

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

        result = run_ffprobe(command, step="probe_duration", job_id=self.job_id)

        return float((result.stdout or b"").decode("utf-8", errors="replace").strip())

    def _merge_small_gaps(
        self,
        segments: List[Dict],
        max_gap: float = 0.35,
    ) -> List[Dict]:

        if not segments:
            return []

        merged = [segments[0]]

        for current in segments[1:]:

            previous = merged[-1]

            gap = current["start"] - previous["end"]
            merged_duration = current["end"] - previous["start"]

            # A NEGATIVE gap means the two segments overlap in time. Concatenating their
            # text would duplicate the overlapping words, which is exactly the artifact
            # seam reconciliation exists to remove. Only genuinely adjacent segments merge.
            if -0.05 <= gap <= max_gap and merged_duration <= self.max_merged_segment_duration_sec:

                previous["end"] = current["end"]

                previous["text"] = f'{previous["text"]} {current["text"]}'.strip()

            else:

                merged.append(current)

        return merged

    def _clean_text(self, text: str) -> str:
        """Whitespace and punctuation tidying only. Nothing spoken is removed.

        This used to also delete filler words (eh/ah/hum/uh) and collapse repeated words —
        without adjusting timestamps, so the stored text no longer lined up with the audio,
        and every downstream estimator that maps character positions to time inherited the
        drift. ClipFlow also cuts real speech: hesitation and repetition carry editorial
        meaning and are not noise to be tidied away.

        The aggressive form still exists for matching and dedup, via `_strip_fillers` and
        `seam_reconciler.normalize_for_match`, but it never replaces the transcript.
        """
        if not text:
            return ""

        cleaned = re.sub(r"\s+", " ", text).strip()
        # Collapse runs of identical punctuation ("!!!" -> "!"); leaves words untouched.
        cleaned = re.sub(r"([,.;:?!])\1+", r"\1", cleaned)
        return cleaned.strip(" ,")

    def _strip_fillers(self, text: str) -> str:
        """The aggressive form: filler words and immediate repetitions removed.

        Opt-in (ASR_STRIP_FILLERS) and stored as `normalized_text` alongside the real text,
        never in place of it. Timestamps still refer to what was actually said.
        """
        if not text:
            return ""

        stripped = re.sub(r"\b(\w+)( \1\b)+", r"\1", text, flags=re.IGNORECASE)
        stripped = re.sub(r"\b(eh|ah|hum|hã|uh)\b", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+", " ", stripped).strip(" ,")
        return stripped
