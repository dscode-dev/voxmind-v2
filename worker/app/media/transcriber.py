import json
import math
import re
import subprocess
import gc
from pathlib import Path
from typing import List, Dict, Tuple

from faster_whisper import WhisperModel


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
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.parallel_workers = parallel_workers
        self.model = None
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.segment_duration_sec = segment_duration_sec
        self.max_merged_segment_duration_sec = max_merged_segment_duration_sec
        self.fallback_to_cpu_on_oom = fallback_to_cpu_on_oom

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

        windows = self._build_windows(duration)

        chunk_files = self._extract_audio_chunks(video_path, audio_dir, windows)

        all_segments: List[Dict] = []

        for index, (chunk_file, start_offset) in enumerate(chunk_files):

            part_file = transcript_dir / f"part_{index:03d}.json"

            # ====================================
            # CHECKPOINT
            # ====================================

            if part_file.exists():

                with open(part_file, "r", encoding="utf-8") as f:
                    chunk_segments = json.load(f)

                all_segments.extend(chunk_segments)

                continue

            # ====================================
            # TRANSCRIBE
            # ====================================

            segments, _info = self.model.transcribe(
                str(chunk_file),
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=self.vad_filter,
                word_timestamps=True,
            )

            chunk_segments: List[Dict] = []

            for segment in segments:

                text = self._normalize_text((segment.text or "").strip())

                if not text:
                    continue

                words = []
                for word in list(getattr(segment, "words", []) or []):
                    word_text = self._normalize_text((getattr(word, "word", "") or "").strip())
                    word_start = getattr(word, "start", None)
                    word_end = getattr(word, "end", None)
                    if not word_text or word_start is None or word_end is None:
                        continue
                    words.append(
                        {
                            "start": float(word_start) + start_offset,
                            "end": float(word_end) + start_offset,
                            "text": word_text,
                        }
                    )

                chunk_segments.append(
                    {
                        "start": float(segment.start) + start_offset,
                        "end": float(segment.end) + start_offset,
                        "text": text,
                        "words": words,
                    }
                )

            # ====================================
            # SAVE CHECKPOINT
            # ====================================

            with open(part_file, "w", encoding="utf-8") as f:
                json.dump(chunk_segments, f, indent=2, ensure_ascii=False)

            all_segments.extend(chunk_segments)

        all_segments.sort(key=lambda s: s["start"])

        return self._merge_small_gaps(all_segments)

    def _ensure_model(self) -> None:
        if self.model is not None:
            return

        self.model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=self.cpu_threads,
            num_workers=self.parallel_workers,
        )

    def _should_fallback_to_cpu(self, exc: Exception) -> bool:
        if not self.fallback_to_cpu_on_oom:
            return False
        if str(self.device).strip().lower() != "cuda":
            return False
        message = str(exc).lower()
        return "cuda" in message and "out of memory" in message

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

        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )

        return float(result.stdout.strip())

    def _build_windows(self, duration: float) -> List[Tuple[float, float]]:

        windows: List[Tuple[float, float]] = []

        total_chunks = math.ceil(duration / self.segment_duration_sec)

        for i in range(total_chunks):

            start = i * self.segment_duration_sec

            remaining = max(0.0, duration - start)

            chunk_duration = min(self.segment_duration_sec, remaining)

            if chunk_duration > 0:

                windows.append((start, chunk_duration))

        return windows

    def _extract_audio_chunks(
        self,
        video_path: Path,
        audio_dir: Path,
        windows: List[Tuple[float, float]],
    ) -> List[Tuple[Path, float]]:

        results: List[Tuple[Path, float]] = []

        for index, (start_offset, chunk_duration) in enumerate(windows):

            chunk_file = audio_dir / f"chunk_{index:03d}.wav"

            command = [
                "ffmpeg",
                "-y",
                "-ss",
                str(start_offset),
                "-i",
                str(video_path),
                "-t",
                str(chunk_duration),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(chunk_file),
            ]

            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            results.append((chunk_file, start_offset))

        return results

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

            if gap <= max_gap and merged_duration <= self.max_merged_segment_duration_sec:

                previous["end"] = current["end"]

                previous["text"] = f'{previous["text"]} {current["text"]}'.strip()

            else:

                merged.append(current)

        return merged

    def _normalize_text(self, text: str) -> str:
        if not text:
            return ""

        normalized = re.sub(r"\s+", " ", text).strip()
        normalized = re.sub(r"\b(\w+)( \1\b)+", r"\1", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"([,.;:?!]){2,}", r"\1", normalized)
        normalized = re.sub(r"\b(eh|ah|hum|hã|uh)\b", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s+", " ", normalized).strip(" ,")
        return normalized
