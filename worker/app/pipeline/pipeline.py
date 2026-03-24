import json
import re
from pathlib import Path

from app.media.audio_extractor import AudioExtractor
from app.media.downloader import VideoDownloader
from app.media.diarizer import SpeakerDiarizer
from app.media.transcriber import Transcriber
from app.media.transcript_merger import TranscriptSpeakerMerger
from app.video.cutter import VideoCutter
from app.video.qa import ClipQA

from app.pipeline.chunker import Chunker
from app.pipeline.auto_review import AutoReviewPolicy
from app.pipeline.candidate_builder import CandidateBuilder
from app.pipeline.delivery_package_builder import DeliveryPackageBuilder
from app.pipeline.scorer import Scorer
from app.prompts.manual_prompt_builder import ManualPromptBuilder
from app.pipeline.hook_detector import HookDetector
from app.pipeline.audio_peak_detector import AudioPeakDetector
from app.pipeline.story_shift_detector import StoryShiftDetector

from app.integrations.telegram_sender import TelegramSender
from app.observability import ArtifactTracker, RuntimeTracker, get_logger
from app.settings import settings

from app.storage.minio_client import MinioStorage


class Pipeline:

    def __init__(
        self,
        video_url: str,
        job_id: str,
        manual_response: dict | None = None,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
        build_ia: bool = False,
    ):

        self.video_url = video_url
        self.job_id = job_id
        self.manual_response = manual_response

        self.clip_mode = clip_mode
        self.video_ratio = video_ratio
        self.build_ia = build_ia

        self.work_dir = Path(settings.work_dir) / job_id
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.failed = False

        self.storage = MinioStorage()
        self.runtime = RuntimeTracker(self.work_dir, job_id)
        self.artifacts = ArtifactTracker(self.work_dir, job_id)
        self.logger = get_logger(__name__)

        self.downloader = VideoDownloader(self.work_dir)
        self.audio_extractor = AudioExtractor(self.work_dir / "audio")

        self.transcriber = Transcriber(
            model_size=settings.asr_model_size,
            compute_type=settings.asr_compute_type,
            language=settings.asr_language,
            beam_size=settings.asr_beam_size,
            vad_filter=settings.asr_vad_filter,
            segment_duration_sec=settings.asr_segment_duration_sec,
            parallel_workers=settings.asr_parallel_workers,
            max_merged_segment_duration_sec=settings.asr_max_merged_segment_duration_sec,
        )
        self.diarizer = SpeakerDiarizer(
            enabled=settings.diarization_enabled,
            model_name=settings.diarization_model_name,
            hf_token=settings.diarization_hf_token,
        )
        self.transcript_merger = TranscriptSpeakerMerger(
            min_overlap_sec=settings.diarization_min_overlap_sec,
        )

        self.chunker = self._build_chunker()
        self.builder = self._build_candidate_builder()
        self.scorer = self._build_scorer()
        self.delivery_package_builder = DeliveryPackageBuilder()
        self.auto_review_policy = AutoReviewPolicy(
            enabled=settings.auto_review_enabled,
            ready_score_threshold=settings.auto_review_ready_score_threshold,
            blocked_score_threshold=settings.auto_review_blocked_score_threshold,
            max_review_clips=settings.auto_review_max_review_clips,
        )
        self.hook_detector = HookDetector()
        self.audio_peak_detector = AudioPeakDetector()
        self.story_shift_detector = StoryShiftDetector()

        self.cutter = VideoCutter(
            self.work_dir,
            min_clip_duration_sec=settings.render_min_clip_duration_sec,
        )
        self.clip_qa = ClipQA(
            min_duration_sec=settings.qa_min_clip_duration_sec,
            max_duration_sec=settings.qa_max_clip_duration_sec,
            max_speakers_per_clip=settings.qa_max_speakers_per_clip,
        )

        self.telegram = TelegramSender()
        self.prompt_builder = ManualPromptBuilder()

    def _build_chunker(self) -> Chunker:
        if self.clip_mode == "short" and self.video_ratio == "portrait":
            return Chunker(min_duration=24, target_duration=40, max_duration=60, overlap=5)

        if self.clip_mode == "short_serie" and self.video_ratio == "portrait":
            return Chunker(min_duration=26, target_duration=46, max_duration=68, overlap=5)

        if self.clip_mode == "short":
            return Chunker(min_duration=26, target_duration=48, max_duration=72, overlap=5)

        if self.clip_mode == "short_serie":
            return Chunker(min_duration=28, target_duration=50, max_duration=75, overlap=5)

        return Chunker()

    def _build_candidate_builder(self) -> CandidateBuilder:
        if self.clip_mode == "short" and self.video_ratio == "portrait":
            return CandidateBuilder(
                max_candidate_duration_sec=60,
                preferred_duration_sec=42,
                min_candidate_duration_sec=24,
                max_candidates_per_window=3,
            )

        if self.clip_mode == "short_serie" and self.video_ratio == "portrait":
            return CandidateBuilder(
                max_candidate_duration_sec=68,
                preferred_duration_sec=48,
                min_candidate_duration_sec=26,
                max_candidates_per_window=3,
            )

        if self.clip_mode == "short":
            return CandidateBuilder(
                max_candidate_duration_sec=72,
                preferred_duration_sec=50,
                min_candidate_duration_sec=26,
            )

        if self.clip_mode == "short_serie":
            return CandidateBuilder(
                max_candidate_duration_sec=75,
                preferred_duration_sec=52,
                min_candidate_duration_sec=28,
            )

        return CandidateBuilder(
            max_candidate_duration_sec=settings.candidate_max_duration_sec,
        )

    def _build_scorer(self) -> Scorer:
        if self.clip_mode == "short" and self.video_ratio == "portrait":
            return Scorer(max_candidates=8, max_candidates_per_window=1, min_start_gap=18)

        if self.clip_mode == "short_serie" and self.video_ratio == "portrait":
            return Scorer(
                max_candidates=8,
                max_candidates_per_window=1,
                min_start_gap=16,
                prefer_thematic_continuity=True,
                thematic_similarity_threshold=0.16,
            )

        return Scorer()

    # ==================================================
    # Logging helper
    # ==================================================

    def _log(self, message: str):
        self.logger.info(
            message,
            extra={
                "job_id": self.job_id,
                "pipeline_stage": settings.pipeline_stage,
            },
        )

        try:
            self.telegram.send_message(
                f"""
🧠 VOXMIND PIPELINE

JOB_ID: {self.job_id}

{message}
"""
            )
        except Exception:
            self.logger.exception(
                "Telegram notification failed",
                extra={
                    "job_id": self.job_id,
                    "pipeline_stage": settings.pipeline_stage,
                    "step": "telegram_notify",
                    "status": "failed",
                },
            )

    def _mark_step(self, step: str, status: str, **details):
        self.runtime.mark(settings.pipeline_stage, step, status, **details)
        self.logger.info(
            f"{step}:{status}",
            extra={
                "job_id": self.job_id,
                "pipeline_stage": settings.pipeline_stage,
                "step": step,
                "status": status,
            },
        )

    def _write_json_artifact(self, filename: str, payload: object, artifact_name: str) -> Path:
        path = self.work_dir / filename
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)

        self.artifacts.mark_local(
            artifact_name,
            settings.pipeline_stage,
            path,
            artifact_type="json",
        )
        return path

    def _write_text_artifact(self, filename: str, content: str, artifact_name: str) -> Path:
        path = self.work_dir / filename
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

        self.artifacts.mark_local(
            artifact_name,
            settings.pipeline_stage,
            path,
            artifact_type="text",
        )
        return path

    # ==================================================
    # Main runner
    # ==================================================

    def run(self):

        try:
            self._mark_step("pipeline", "started")

            if settings.pipeline_stage == "prepare":
                return self._prepare_stage()

            if settings.pipeline_stage == "finalize":
                return self._finalize_stage()

            raise RuntimeError("Invalid PIPELINE_STAGE")

        except Exception as e:
            self.failed = True
            self._mark_step("pipeline", "failed", error=str(e))

            try:
                self.telegram.send_message(
                    f"""
❌ VOXMIND ERROR

JOB_ID: {self.job_id}

ERROR:
{str(e)}
"""
                )
            except Exception:
                self.logger.exception(
                    "Failed to send Telegram error notification",
                    extra={
                        "job_id": self.job_id,
                        "pipeline_stage": settings.pipeline_stage,
                        "step": "telegram_error_notify",
                        "status": "failed",
                    },
                )

            return {
                "status": "error",
                "job_id": self.job_id,
                "error": str(e),
                "runtime_status_path": str(self.runtime.runtime_path),
                "artifacts_manifest_path": str(self.artifacts.manifest_path),
            }

    # ==================================================
    # STAGE 1 - PREPARE
    # ==================================================

    def _prepare_stage(self):
        self._mark_step("prepare", "started")

        self._log("⬇️ Downloading video...")
        self._mark_step("download_video", "started")

        video_path = self.downloader.download(self.video_url)
        self._mark_step("download_video", "completed", video_path=str(video_path))

        self._log("💾 Uploading video to storage...")
        self._mark_step("upload_video", "started")

        self.storage.upload(
            str(video_path),
            f"jobs/{self.job_id}/video.mp4"
        )
        self._mark_step("upload_video", "completed")

        self._log("🧠 Transcribing video in segments...")
        self._mark_step("transcribe", "started")

        segments = self.transcriber.transcribe(video_path)

        if not segments:
            raise RuntimeError("Transcription returned no segments")
        self._mark_step("transcribe", "completed", segment_count=len(segments))

        raw_segments = [dict(segment) for segment in segments]
        segments = self._apply_diarization(video_path, raw_segments)

        self._log("✂️ Generating chunks...")
        self._mark_step("chunk", "started")

        chunks = self.chunker.chunk(segments)
        self._mark_step("chunk", "completed", chunk_count=len(chunks))

        self._log("🔎 Detecting hooks...")
        self._mark_step("hook_detection", "started")

        chunks = self.hook_detector.analyze(chunks)
        self._mark_step("hook_detection", "completed")

        self._log("🎧 Detecting audio peaks...")
        self._mark_step("audio_peak_detection", "started")

        chunks = self.audio_peak_detector.analyze(video_path, chunks)
        self._mark_step("audio_peak_detection", "completed")

        self._log("📖 Detecting narrative shifts...")
        self._mark_step("story_shift_detection", "started")

        chunks = self.story_shift_detector.analyze(chunks)
        self._mark_step("story_shift_detection", "completed")

        self._log("🔥 Extracting candidates...")
        self._mark_step("candidate_build", "started")

        candidates = self.builder.build(chunks)
        self._mark_step("candidate_build", "completed", candidate_count=len(candidates))

        self._log("📊 Ranking candidates...")
        self._mark_step("candidate_score", "started")

        ranked = self.scorer.score(candidates)
        self._mark_step("candidate_score", "completed", ranked_count=len(ranked))

        self._log("📝 Building LLM prompt...")
        self._mark_step("prompt_build", "started")

        prompt = self.prompt_builder.build(
            segments,
            ranked,
            self.job_id,
            clip_mode=self.clip_mode,
            video_ratio=self.video_ratio,
        )
        self._mark_step("prompt_build", "completed", prompt_chars=len(prompt))

        transcript_path = self._write_json_artifact(
            "transcript.json",
            raw_segments,
            "transcript",
        )
        transcript_with_speakers_path = self._write_json_artifact(
            "transcript_with_speakers.json",
            segments,
            "transcript_with_speakers",
        )
        candidates_path = self._write_json_artifact(
            "candidates.json",
            ranked,
            "candidates",
        )
        prompt_path = self._write_text_artifact(
            "prompt.txt",
            prompt,
            "prompt",
        )

        self._log("📤 Sending prompt to Telegram...")
        self._mark_step("send_prompt", "started")

        self.telegram.send_document(
            str(prompt_path),
            caption=f"""
🧠 VOXMIND — PROMPT GERADO

JOB_ID: {self.job_id}

1️⃣ Copie o conteúdo do arquivo PROMPT

2️⃣ Cole no ChatGPT / Claude / Gemini

3️⃣ Gere o JSON

4️⃣ Salve como:

response.json

5️⃣ Envie o arquivo aqui para continuar o pipeline.
""",
        )
        self._mark_step("send_prompt", "completed")

        self.telegram.send_message(
            f"""
📊 PIPELINE PRONTO

JOB_ID: {self.job_id}

Envie o arquivo **response.json** retornado pela IA
para continuar o processamento.
"""
        )
        self._mark_step("prepare", "completed")

        return {
            "status": "awaiting_manual_llm",
            "job_id": self.job_id,
            "transcript_path": str(transcript_path),
            "transcript_with_speakers_path": str(transcript_with_speakers_path),
            "candidates_path": str(candidates_path),
            "prompt_path": str(prompt_path),
            "runtime_status_path": str(self.runtime.runtime_path),
            "artifacts_manifest_path": str(self.artifacts.manifest_path),
        }

    def _apply_diarization(self, video_path: Path, segments: list[dict]) -> list[dict]:
        self._mark_step("diarization", "started")
        diagnostics = self.diarizer.diagnostics()

        if not settings.diarization_enabled:
            merged = self.transcript_merger.merge(segments, [])
            unknown_segment_count = sum(
                1 for segment in merged if segment.get("speaker", "UNKNOWN") == "UNKNOWN"
            )
            diagnostics = {
                **diagnostics,
                "speaker_turn_count": 0,
                "segment_count": len(merged),
                "unknown_segment_count": unknown_segment_count,
            }
            self._write_json_artifact(
                "diarization_diagnostics.json",
                diagnostics,
                "diarization_diagnostics",
            )
            self._mark_step("diarization", "skipped", reason="disabled")
            return merged

        if not self.diarizer.is_available:
            merged = self.transcript_merger.merge(segments, [])
            diagnostics = self.diarizer.diagnostics()
            unknown_segment_count = sum(
                1 for segment in merged if segment.get("speaker", "UNKNOWN") == "UNKNOWN"
            )
            diagnostics = {
                **diagnostics,
                "speaker_turn_count": 0,
                "segment_count": len(merged),
                "unknown_segment_count": unknown_segment_count,
            }
            self._write_json_artifact(
                "diarization_diagnostics.json",
                diagnostics,
                "diarization_diagnostics",
            )
            self._mark_step(
                "diarization",
                "skipped",
                reason=self.diarizer.availability_reason,
            )
            return merged

        self._log("🗣️ Detecting speakers...")

        audio_path = self.audio_extractor.extract_wav_16k_mono(video_path)
        self.artifacts.mark_local(
            "source_audio",
            settings.pipeline_stage,
            audio_path,
            artifact_type="audio",
        )

        speaker_turns = self.diarizer.diarize(audio_path)
        merged = self.transcript_merger.merge(segments, speaker_turns)
        unknown_segment_count = sum(
            1 for segment in merged if segment.get("speaker", "UNKNOWN") == "UNKNOWN"
        )
        diagnostics = {
            **self.diarizer.diagnostics(),
            "speaker_turn_count": len(speaker_turns),
            "segment_count": len(merged),
            "unknown_segment_count": unknown_segment_count,
        }
        diarization_path = self._write_json_artifact(
            "speaker_turns.json",
            speaker_turns,
            "speaker_turns",
        )
        self._write_json_artifact(
            "diarization_diagnostics.json",
            diagnostics,
            "diarization_diagnostics",
        )

        self.artifacts.mark_local(
            "speaker_turns",
            settings.pipeline_stage,
            diarization_path,
            artifact_type="json",
        )
        self._mark_step(
            "diarization",
            "completed",
            speaker_turn_count=len(speaker_turns),
            unknown_segment_count=unknown_segment_count,
            availability_reason=self.diarizer.availability_reason,
        )
        return merged

    # ==================================================
    # STAGE 2 - FINALIZE
    # ==================================================

    def _finalize_stage(self):
        self._mark_step("finalize", "started")

        if not self.manual_response:
            raise RuntimeError("Manual response missing")

        self._log("🔎 Validating AI response...")
        self._mark_step("validate_ai_response", "started")

        try:

            if isinstance(self.manual_response, str):
                self.manual_response = self._parse_manual_response(self.manual_response)

            text = json.dumps(self.manual_response, ensure_ascii=False)
            self.manual_response = self._parse_manual_response(text)

        except Exception:
            raise RuntimeError("Invalid JSON received from AI")

        if "shorts_content" not in self.manual_response:
            raise RuntimeError("Invalid response: shorts_content missing")

        response_validation = self._build_response_validation(
            self.manual_response.get("shorts_content", [])
        )
        self.manual_response["_response_validation"] = response_validation
        self._mark_step("validate_ai_response", "completed")

        self._log("⬇️ Downloading video from storage...")
        self._mark_step("download_video", "started")

        video_path = self.work_dir / "video.mp4"

        self.storage.download(
            f"jobs/{self.job_id}/video.mp4",
            str(video_path),
        )

        if not video_path.exists():
            raise RuntimeError("Video not found in storage")
        self._mark_step("download_video", "completed", video_path=str(video_path))

        transcript_segments = self._load_finalize_transcript()
        cuts = self.manual_response.get("shorts_content", [])

        if not cuts:
            raise RuntimeError("shorts_content is empty")

        cuts = self._normalize_cuts_to_transcript(cuts, transcript_segments)
        self.manual_response["shorts_content"] = cuts

        self._log("🎬 Generating cuts...")
        self._mark_step("render_cuts", "started")

        filtered_cuts = []

        for cut in cuts:

            start = float(cut["start"])
            end = float(cut["end"])

            if end <= start:
                continue

            duration = end - start

            if duration < settings.render_min_clip_duration_sec:
                continue

            filtered_cuts.append(cut)

        self._log(f"Valid cuts after filtering: {len(filtered_cuts)}")

        if not filtered_cuts:
            raise RuntimeError("No valid cuts after filtering")

        cut_files = self.cutter.cut(video_path, filtered_cuts)
        self._mark_step("render_cuts", "completed", cut_count=len(cut_files))

        for index, path in enumerate(cut_files, start=1):
            self.artifacts.mark_local(
                f"cut_{index:02d}",
                settings.pipeline_stage,
                path,
                artifact_type="video",
            )

        self._log(f"📦 {len(cut_files)} cuts generated")

        qa_report = self._run_clip_qa(filtered_cuts, cut_files, transcript_segments)
        automation_report = self._run_auto_review(qa_report, filtered_cuts)
        if qa_report is not None:
            qa_report["automation"] = automation_report
            qa_report["response_validation"] = self.manual_response.get("_response_validation", {})
        delivery_package = self._build_delivery_package(
            filtered_cuts,
            cut_files,
            qa_report,
            automation_report,
        )

        self._mark_step("send_cuts", "started")
        for path in cut_files:
            self.telegram.send_video(path)

        if qa_report is not None:
            qa_report_path = self._write_json_artifact(
                "qa_report.json",
                qa_report,
                "qa_report",
            )
            self.telegram.send_document(
                str(qa_report_path),
                caption=f"QA report for JOB_ID: {self.job_id}",
            )

        delivery_package_path = self._write_json_artifact(
            "delivery_package.json",
            delivery_package,
            "delivery_package",
        )
        self.telegram.send_document(
            str(delivery_package_path),
            caption=f"Delivery package for JOB_ID: {self.job_id}",
        )
        self._mark_step("send_cuts", "completed")
        self._mark_step("finalize", "completed")

        return {
            "status": "success",
            "job_id": self.job_id,
            "cut_files": cut_files,
            "qa_report_path": str(self.work_dir / "qa_report.json") if qa_report is not None else None,
            "delivery_package_path": str(delivery_package_path),
            "runtime_status_path": str(self.runtime.runtime_path),
            "artifacts_manifest_path": str(self.artifacts.manifest_path),
        }

    def _sanitize_json_text(self, text: str) -> str:
        replacements = {
            "“": '"',
            "”": '"',
            "„": '"',
            "‟": '"',
            "’": "'",
            "‘": "'",
            "´": "'",
            "\ufeff": "",
        }

        sanitized = text
        for source, target in replacements.items():
            sanitized = sanitized.replace(source, target)

        sanitized = sanitized.strip()
        if sanitized.startswith("```"):
            sanitized = re.sub(r"^```(?:json)?\s*", "", sanitized, flags=re.IGNORECASE)
            sanitized = re.sub(r"\s*```$", "", sanitized)

        sanitized = re.sub(r",(\s*[}\]])", r"\1", sanitized)
        return sanitized

    def _parse_manual_response(self, text: str) -> dict:
        sanitized = self._sanitize_json_text(text)

        try:
            return json.loads(sanitized)
        except json.JSONDecodeError:
            start = sanitized.find("{")
            end = sanitized.rfind("}")
            if start != -1 and end != -1 and end > start:
                extracted = sanitized[start : end + 1]
                extracted = re.sub(r",(\s*[}\]])", r"\1", extracted)
                return json.loads(extracted)
            raise

    def _build_response_validation(self, cuts: list[dict]) -> dict:
        warnings: list[str] = []
        generic_title_markers = {
            "o jogo por trás",
            "quem realmente manda",
            "o objetivo final",
            "o tamanho do poder",
        }

        for index, cut in enumerate(cuts, start=1):
            title = str(cut.get("title") or "").strip().lower()
            hook = str(cut.get("hook") or "").strip()

            if title in generic_title_markers:
                warnings.append(f"cut_{index}: generic_title")

            if hook and len(hook) < 18:
                warnings.append(f"cut_{index}: short_hook")

        continuity_warnings = self._short_serie_continuity_gaps(cuts)
        warnings.extend(
            f"continuity_gap:{item['gap_sec']}"
            for item in continuity_warnings
        )

        if continuity_warnings:
            self._mark_step(
                "validate_ai_response",
                "warning",
                continuity_gaps=continuity_warnings,
            )

        return {
            "warnings": warnings,
            "continuity_gaps": continuity_warnings,
        }

    def _short_serie_continuity_gaps(self, cuts: list[dict]) -> list[dict]:
        if self.clip_mode != "short_serie" or len(cuts) < 2:
            return []

        ordered = sorted(cuts, key=lambda item: float(item["start"]))
        continuity_warnings = []

        for previous, current in zip(ordered, ordered[1:]):
            previous_group = previous.get("merge_group")
            current_group = current.get("merge_group")

            if previous_group and current_group and previous_group != current_group:
                continue

            gap = float(current["start"]) - float(previous["end"])
            if gap > settings.short_serie_max_gap_sec:
                continuity_warnings.append(
                    {
                        "previous_end": float(previous["end"]),
                        "current_start": float(current["start"]),
                        "gap_sec": round(gap, 2),
                    }
                )

        return continuity_warnings

    def _load_finalize_transcript(self) -> list[dict]:
        transcript_path = self.work_dir / "transcript_with_speakers.json"
        fallback_path = self.work_dir / "transcript.json"

        try:
            self.storage.download(
                f"jobs/{self.job_id}/transcript_with_speakers.json",
                str(transcript_path),
            )
        except Exception:
            transcript_path = fallback_path
            try:
                self.storage.download(
                    f"jobs/{self.job_id}/transcript.json",
                    str(fallback_path),
                )
            except Exception:
                return []

        if not transcript_path.exists():
            return []

        try:
            with open(transcript_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return []

        if isinstance(data, list):
            return data

        return []

    def _normalize_cuts_to_transcript(
        self,
        cuts: list[dict],
        transcript_segments: list[dict],
    ) -> list[dict]:
        if not cuts or not transcript_segments:
            return cuts

        normalized: list[dict] = []
        tolerance = settings.render_boundary_snap_tolerance_sec

        for cut in cuts:
            try:
                start = float(cut["start"])
                end = float(cut["end"])
            except Exception:
                normalized.append(cut)
                continue

            if end <= start:
                normalized.append(cut)
                continue

            start_segment = self._find_segment_covering(transcript_segments, start)
            end_segment = self._find_segment_covering(transcript_segments, end)

            snapped_start = start
            snapped_end = end

            if start_segment is not None:
                segment_start = float(start_segment.get("start", start))
                if (start - segment_start) <= tolerance:
                    snapped_start = segment_start

            if end_segment is not None:
                segment_end = float(end_segment.get("end", end))
                if (segment_end - end) <= tolerance:
                    snapped_end = segment_end

            if snapped_end - snapped_start < settings.render_min_clip_duration_sec:
                snapped_end = self._extend_end_to_min_duration(
                    transcript_segments,
                    snapped_start,
                    snapped_end,
                )

            normalized_cut = {
                **cut,
                "start": round(snapped_start, 2),
                "end": round(snapped_end, 2),
                "safe_start": round(snapped_start, 2),
                "safe_end": round(snapped_end, 2),
            }
            normalized.append(normalized_cut)

        return normalized

    def _find_segment_covering(self, transcript_segments: list[dict], timestamp: float) -> dict | None:
        for segment in transcript_segments:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", 0.0))
            if start <= timestamp <= end:
                return segment
        return None

    def _extend_end_to_min_duration(
        self,
        transcript_segments: list[dict],
        start: float,
        current_end: float,
    ) -> float:
        min_end = start + settings.render_min_clip_duration_sec
        extended_end = current_end

        for segment in transcript_segments:
            segment_end = float(segment.get("end", 0.0))
            if segment_end >= min_end:
                extended_end = segment_end
                break

        return extended_end

    def _run_clip_qa(
        self,
        filtered_cuts: list[dict],
        cut_files: list[Path],
        transcript_segments: list[dict],
    ) -> dict | None:
        if not settings.qa_enabled:
            self._mark_step("qa", "skipped", reason="disabled")
            return None

        self._mark_step("qa", "started")
        report = self.clip_qa.evaluate(
            requested_cuts=filtered_cuts,
            rendered_files=cut_files,
            transcript_segments=transcript_segments,
        )
        self._mark_step("qa", "completed", decision=report.get("decision"))
        return report

    def _build_delivery_package(
        self,
        filtered_cuts: list[dict],
        cut_files: list[Path],
        qa_report: dict | None,
        automation_report: dict | None,
    ) -> dict:
        self._mark_step("delivery_package", "started")
        package = self.delivery_package_builder.build(
            job_id=self.job_id,
            clip_mode=self.clip_mode,
            video_ratio=self.video_ratio,
            cuts=filtered_cuts,
            cut_files=cut_files,
            long_video_script=self.manual_response.get("long_video_script"),
            qa_report=qa_report,
            automation_report=automation_report,
            artifacts_manifest=self.artifacts.read(),
            response_validation=self.manual_response.get("_response_validation"),
        )
        self._mark_step(
            "delivery_package",
            "completed",
            clip_count=package.get("clip_count", 0),
            delivery_status=package.get("delivery_status"),
        )
        return package

    def _run_auto_review(
        self,
        qa_report: dict | None,
        filtered_cuts: list[dict],
    ) -> dict | None:
        if qa_report is None:
            self._mark_step("auto_review", "skipped", reason="qa_unavailable")
            return None

        self._mark_step("auto_review", "started")
        report = self.auto_review_policy.evaluate(
            qa_report=qa_report,
            cuts=filtered_cuts,
        )
        self._mark_step(
            "auto_review",
            "completed",
            review_status=report.get("status"),
            readiness_score=report.get("readiness_score"),
        )
        return report
