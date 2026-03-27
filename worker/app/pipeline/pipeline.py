import ast
import json
import re
from pathlib import Path

from app.media.audio_extractor import AudioExtractor
from app.media.downloader import VideoDownloader
from app.media.diarizer import SpeakerDiarizer
from app.media.transcriber import Transcriber
from app.media.transcript_merger import TranscriptSpeakerMerger
from app.video.cutter import VideoCutter
from app.video.final_renderer import FinalVideoRenderer
from app.video.qa import ClipQA

from app.pipeline.chunker import Chunker
from app.pipeline.auto_review import AutoReviewPolicy
from app.pipeline.candidate_builder import CandidateBuilder
from app.pipeline.delivery_package_builder import DeliveryPackageBuilder
from app.pipeline.publish_package_builder import PublishPackageBuilder
from app.pipeline.soundtrack_selector import SoundtrackSelector
from app.pipeline.subtitle_builder import SubtitleBuilder
from app.pipeline.scorer import Scorer
from app.prompts.manual_prompt_builder import ManualPromptBuilder
from app.pipeline.hook_detector import HookDetector
from app.pipeline.render_plan_builder import RenderPlanBuilder
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
            device=settings.asr_device,
            compute_type=settings.asr_compute_type,
            cpu_threads=settings.asr_cpu_threads,
            language=settings.asr_language,
            beam_size=settings.asr_beam_size,
            vad_filter=settings.asr_vad_filter,
            segment_duration_sec=settings.asr_segment_duration_sec,
            parallel_workers=settings.asr_parallel_workers,
            max_merged_segment_duration_sec=settings.asr_max_merged_segment_duration_sec,
            fallback_to_cpu_on_oom=settings.asr_fallback_to_cpu_on_oom,
        )
        self.diarizer = SpeakerDiarizer(
            enabled=settings.diarization_enabled,
            model_name=settings.diarization_model_name,
            device=settings.diarization_device,
            hf_token=settings.diarization_hf_token,
            fallback_to_cpu_on_oom=settings.diarization_fallback_to_cpu_on_oom,
        )
        self.transcript_merger = TranscriptSpeakerMerger(
            min_overlap_sec=settings.diarization_min_overlap_sec,
        )

        self.chunker = self._build_chunker()
        self.builder = self._build_candidate_builder()
        self.scorer = self._build_scorer()
        self.delivery_package_builder = DeliveryPackageBuilder()
        self.publish_package_builder = PublishPackageBuilder()
        self.soundtrack_selector = SoundtrackSelector()
        self.subtitle_builder = SubtitleBuilder(
            playback_speed=settings.render_playback_speed,
        )
        self.render_plan_builder = RenderPlanBuilder()
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
        self.final_renderer = FinalVideoRenderer(self.work_dir)
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
                max_candidates_per_window=2,
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
            self._release_accelerator_memory()
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
        finally:
            self._release_accelerator_memory()

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
        self.transcriber.release_resources()

        raw_segments = [dict(segment) for segment in segments]
        segments = self._apply_diarization(video_path, raw_segments)
        self.diarizer.release_resources()

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

        combined_candidates = candidates

        self._log("📊 Ranking candidates...")
        self._mark_step("candidate_score", "started")

        ranked = self.scorer.score(combined_candidates)
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

    def _merge_candidate_sources(self, primary: list[dict], secondary: list[dict]) -> list[dict]:
        if not secondary:
            return primary

        merged = [*primary]
        for candidate in secondary:
            if self._contains_similar_candidate(merged, candidate):
                continue
            merged.append(candidate)

        return sorted(merged, key=lambda item: (float(item["start"]), float(item["end"])))

    def _contains_similar_candidate(self, existing: list[dict], candidate: dict) -> bool:
        candidate_start = float(candidate["start"])
        candidate_end = float(candidate["end"])
        candidate_text = str(candidate.get("text") or "").strip().lower()

        for item in existing:
            start = float(item["start"])
            end = float(item["end"])
            if abs(candidate_start - start) < 4.0 and abs(candidate_end - end) < 4.0:
                return True

            existing_text = str(item.get("text") or "").strip().lower()
            if candidate_text and existing_text and candidate_text[:120] == existing_text[:120]:
                return True

        return False

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

    def _release_accelerator_memory(self) -> None:
        for component in (
            self.transcriber,
            self.diarizer,
        ):
            try:
                component.release_resources()
            except Exception:
                continue

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

        self.manual_response = self._normalize_response_schema(self.manual_response)

        if "shorts_content" not in self.manual_response:
            raise RuntimeError("Invalid response: shorts_content missing")

        self.manual_response["post"] = self._normalize_post_payload(
            self.manual_response.get("post"),
            self.manual_response.get("shorts_content", []),
        )

        response_validation = self._build_response_validation(
            self.manual_response.get("shorts_content", []),
            self.manual_response.get("post", {}),
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
        cuts = self._align_first_cut_to_global_hook(
            cuts,
            transcript_segments,
            self.manual_response.get("post", {}),
        )
        cuts = self._prune_disconnected_short_serie_cuts(cuts)
        self.manual_response["shorts_content"] = cuts

        self._log("🎬 Generating cuts...")
        self._mark_step("render_cuts", "started")

        filtered_cuts = []
        min_cut_duration = (
            settings.render_min_internal_cut_duration_sec
            if self.manual_response.get("final_videos")
            else settings.render_min_clip_duration_sec
        )

        for cut in cuts:

            start = float(cut["start"])
            end = float(cut["end"])

            if end <= start:
                continue

            duration = end - start

            if duration < min_cut_duration:
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
        render_plan = self._build_render_plan(filtered_cuts, transcript_segments, qa_report)
        subtitle_path = self._build_final_reel_subtitles(
            filtered_cuts,
            transcript_segments,
        )
        self.manual_response["_final_video_specs"] = self._build_final_video_specs(transcript_segments)
        final_clip_files = self._render_final_clips(
            video_path,
            filtered_cuts,
            cut_files,
            render_plan,
            transcript_segments,
        )
        self._log(f"🎞️ Final videos generated: {len(final_clip_files)}")
        final_reel_path = None
        publish_package = self._build_publish_package(
            filtered_cuts,
            final_clip_files,
            final_reel_path,
            subtitle_path,
            qa_report,
            automation_report,
        )
        delivery_package = self._build_delivery_package(
            filtered_cuts,
            cut_files,
            final_clip_files,
            final_reel_path,
            subtitle_path,
            qa_report,
            automation_report,
            render_plan,
        )

        self._mark_step("send_cuts", "started")
        if qa_report is not None:
            qa_report_path = self._write_json_artifact("qa_report.json", qa_report, "qa_report")

        render_plan_path = self._write_json_artifact(
            "render_plan.json",
            render_plan,
            "render_plan",
        )

        delivery_package_path = self._write_json_artifact(
            "delivery_package.json",
            delivery_package,
            "delivery_package",
        )

        publish_package_path = self._write_json_artifact(
            "publish_package.json",
            publish_package,
            "publish_package",
        )

        for index, path in enumerate(final_clip_files, start=1):
            self.artifacts.mark_local(
                f"final_clip_{index:02d}",
                settings.pipeline_stage,
                path,
                artifact_type="video",
            )
            video_payloads = publish_package.get("videos") or []
            video_post = video_payloads[index - 1].get("post", {}) if index - 1 < len(video_payloads) else {}
            caption_lines = [str(video_post.get("title") or f"Video final {index}").strip()]
            if str(video_post.get("hook") or "").strip():
                caption_lines.append(str(video_post.get("hook")).strip())
            self.telegram.send_video(
                str(path),
                caption="\n".join(line for line in caption_lines if line),
            )
        if subtitle_path is not None and subtitle_path.exists():
            self.artifacts.mark_local(
                "final_reel_subtitles",
                settings.pipeline_stage,
                subtitle_path,
                artifact_type="text",
            )
        self.telegram.send_message(
            self._build_publish_message(publish_package)
        )
        self._mark_step("send_cuts", "completed")
        self._mark_step("finalize", "completed")

        return {
            "status": "success",
            "job_id": self.job_id,
            "cut_files": cut_files,
            "final_clip_files": [str(path) for path in final_clip_files],
            "final_reel_path": str(final_reel_path) if final_reel_path is not None else None,
            "subtitle_path": str(subtitle_path) if subtitle_path is not None else None,
            "qa_report_path": str(self.work_dir / "qa_report.json") if qa_report is not None else None,
            "render_plan_path": str(render_plan_path),
            "delivery_package_path": str(delivery_package_path),
            "publish_package_path": str(publish_package_path),
            "runtime_status_path": str(self.runtime.runtime_path),
            "artifacts_manifest_path": str(self.artifacts.manifest_path),
        }

    def _normalize_response_schema(self, payload: dict) -> dict:
        normalized = dict(payload or {})
        final_videos = normalized.get("final_videos")
        if not isinstance(final_videos, list) or not final_videos:
            return normalized

        flattened_cuts: list[dict] = []
        normalized_videos: list[dict] = []

        for index, video in enumerate(final_videos, start=1):
            if not isinstance(video, dict):
                continue

            cuts = video.get("shorts_content") or []
            if not isinstance(cuts, list) or not cuts:
                continue

            post = self._normalize_post_payload(self._extract_video_post_payload(video), cuts)
            video_index = int(video.get("video_index") or index)
            normalized_videos.append(
                {
                    **video,
                    "video_index": video_index,
                    "post": post,
                    "shorts_content": cuts,
                }
            )

            for cut in cuts:
                if not isinstance(cut, dict):
                    continue
                flattened_cuts.append(
                    {
                        **cut,
                        "_post": post,
                        "_video_index": video_index,
                    }
                )

        if flattened_cuts:
            normalized["shorts_content"] = flattened_cuts
        if normalized_videos:
            normalized["final_videos"] = normalized_videos
            normalized["post"] = normalized_videos[0].get("post", {})

        return normalized

    def _extract_video_post_payload(self, video_payload: dict | None) -> dict:
        payload = dict((video_payload or {}).get("post") or {})
        source = video_payload or {}
        direct_fields = {
            "title": source.get("title"),
            "hook": source.get("hook"),
            "hook_source_cut_index": source.get("hook_source_cut_index"),
            "hook_start": source.get("hook_start"),
            "hook_end": source.get("hook_end"),
            "description": source.get("description"),
            "hashtags": source.get("hashtags"),
            "thumbnail": source.get("thumbnail"),
            "thumbnails": source.get("thumbnails"),
            "speaker_focus": source.get("speaker_focus"),
            "soundtrack_suggestion": source.get("soundtrack_suggestion"),
            "tracksound_suggestion": source.get("tracksound_suggestion"),
            "tracksound": source.get("tracksound"),
        }
        for key, value in direct_fields.items():
            if value in (None, "", []):
                continue
            payload.setdefault(key, value)
        return payload

    def _normalize_post_payload(self, post_payload: dict | None, cuts: list[dict]) -> dict:
        payload = dict(post_payload or {})
        first_cut = cuts[0] if cuts else {}

        if not str(payload.get("thumbnail") or "").strip():
            thumbnails = payload.get("thumbnails")
            if isinstance(thumbnails, list):
                for item in thumbnails:
                    value = str(item).strip()
                    if value:
                        payload["thumbnail"] = value
                        break
            elif str(thumbnails or "").strip():
                payload["thumbnail"] = str(thumbnails).strip()

        soundtrack_suggestion = (
            payload.get("soundtrack_suggestion")
            or payload.get("tracksound_suggestion")
            or payload.get("tracksound")
        )
        if soundtrack_suggestion not in (None, ""):
            payload["soundtrack_suggestion"] = str(soundtrack_suggestion).strip()

        hook_start = self._coerce_optional_float(payload.get("hook_start"))
        hook_end = self._coerce_optional_float(payload.get("hook_end"))
        if hook_start is not None:
            payload["hook_start"] = round(hook_start, 2)
        if hook_end is not None:
            payload["hook_end"] = round(hook_end, 2)

        normalized_hashtags = []
        for item in payload.get("hashtags") or first_cut.get("hashtags") or []:
            tag = str(item).strip()
            if not tag:
                continue
            normalized_hashtags.append(tag if tag.startswith("#") else f"#{tag}")

        if not str(payload.get("title") or "").strip():
            payload["title"] = first_cut.get("title")
        if not str(payload.get("hook") or "").strip():
            payload["hook"] = first_cut.get("hook")
        if not str(payload.get("description") or "").strip():
            payload["description"] = first_cut.get("description")
        if not payload.get("hashtags"):
            payload["hashtags"] = normalized_hashtags
        if not str(payload.get("thumbnail") or "").strip():
            payload["thumbnail"] = first_cut.get("thumbnail")
        if not str(payload.get("speaker_focus") or "").strip():
            payload["speaker_focus"] = first_cut.get("speaker_focus")
        return payload

    def _build_response_validation(self, cuts: list[dict], post_payload: dict | None = None) -> dict:
        warnings: list[str] = []
        post_payload = post_payload or {}
        generic_title_markers = {
            "o jogo por trás",
            "quem realmente manda",
            "o objetivo final",
            "o tamanho do poder",
        }

        global_title = str(post_payload.get("title") or "").strip().lower()
        global_hook = str(post_payload.get("hook") or "").strip()

        if global_title in generic_title_markers:
            warnings.append("post: generic_title")

        if global_hook and len(global_hook) < 18:
            warnings.append("post: short_hook")

        if global_hook and cuts:
            first_cut = cuts[0]
            if not self._text_belongs_to_cut(global_hook, first_cut):
                warnings.append("post: hook_outside_first_cut")

        hook_start = self._coerce_optional_float(post_payload.get("hook_start"))
        hook_end = self._coerce_optional_float(post_payload.get("hook_end"))
        if hook_start is not None and hook_end is not None:
            hook_duration = hook_end - hook_start
            if hook_duration <= 0:
                warnings.append("post: invalid_hook_range")
            elif hook_duration > 8.5:
                warnings.append("post: loose_hook_window")
            if cuts:
                first_cut = cuts[0]
                first_start = float(first_cut.get("safe_start", first_cut.get("start", 0.0)))
                first_end = float(first_cut.get("safe_end", first_cut.get("end", 0.0)))
                if hook_start < first_start or hook_end > first_end:
                    warnings.append("post: timed_hook_outside_first_cut")

        total_duration = 0.0

        for index, cut in enumerate(cuts, start=1):
            start = float(cut.get("start", 0.0) or 0.0)
            end = float(cut.get("end", 0.0) or 0.0)
            if end <= start:
                warnings.append(f"cut_{index}: invalid_range")
                continue
            total_duration += end - start

        if total_duration > float(settings.qa_max_clip_duration_sec):
            warnings.append("video: over_max_duration")

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

    def _build_publish_message(self, publish_package: dict) -> str:
        videos = publish_package.get("videos") or []
        lines = [f"🎯 PUBLICAÇÃO PRONTA", f"JOB_ID: {self.job_id}"]

        for video in videos:
            post = video.get("post") or {}
            hashtags = " ".join(post.get("hashtags") or [])
            lines.extend(
                [
                    "",
                    f"Vídeo {video.get('video_index')}",
                    str(post.get("title") or "Video pronto").strip(),
                ]
            )
            if str(post.get("hook") or "").strip():
                lines.append(str(post.get("hook")).strip())
            if str(post.get("description") or "").strip():
                lines.append(str(post.get("description")).strip())
            if hashtags:
                lines.append(hashtags)

        if not videos:
            title = str(publish_package.get("primary_title") or "Video pronto").strip()
            hook = str(publish_package.get("primary_hook") or "").strip()
            description = str(publish_package.get("description") or "").strip()
            hashtags = " ".join(publish_package.get("hashtags") or [])
            lines.extend(["", title])
            if hook:
                lines.append(hook)
            if description:
                lines.extend(["", description])
            if hashtags:
                lines.extend(["", hashtags])
        return "\n".join(line for line in lines if line is not None)

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
        sanitized = self._escape_inner_string_quotes(sanitized)
        return sanitized

    def _escape_inner_string_quotes(self, text: str) -> str:
        result: list[str] = []
        in_string = False
        escaped = False

        for index, char in enumerate(text):
            if escaped:
                result.append(char)
                escaped = False
                continue

            if char == "\\":
                result.append(char)
                escaped = True
                continue

            if char == '"':
                if not in_string:
                    in_string = True
                    result.append(char)
                    continue

                next_significant = self._next_significant_char(text, index + 1)
                if next_significant in {",", "}", "]", ":"} or next_significant is None:
                    in_string = False
                    result.append(char)
                    continue

                result.append('\\"')
                continue

            result.append(char)

        return "".join(result)

    def _next_significant_char(self, text: str, start: int) -> str | None:
        for char in text[start:]:
            if not char.isspace():
                return char
        return None

    def _parse_manual_response(self, text: str) -> dict:
        sanitized = self._sanitize_json_text(text)

        candidates = [sanitized]
        start = sanitized.find("{")
        end = sanitized.rfind("}")
        if start != -1 and end != -1 and end > start:
            extracted = sanitized[start : end + 1]
            extracted = re.sub(r",(\s*[}\]])", r"\1", extracted)
            candidates.append(extracted)

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        for candidate in candidates:
            repaired = self._convert_json_literals_for_python(candidate)
            try:
                parsed = ast.literal_eval(repaired)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, SyntaxError):
                continue

        raise json.JSONDecodeError("Unable to parse AI response JSON", sanitized, 0)

    def _convert_json_literals_for_python(self, text: str) -> str:
        result: list[str] = []
        in_string = False
        escaped = False
        quote_char = ""
        index = 0

        while index < len(text):
            char = text[index]

            if in_string:
                result.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote_char:
                    in_string = False
                    quote_char = ""
                index += 1
                continue

            if char in {'"', "'"}:
                in_string = True
                quote_char = char
                result.append(char)
                index += 1
                continue

            if text.startswith("true", index):
                result.append("True")
                index += 4
                continue
            if text.startswith("false", index):
                result.append("False")
                index += 5
                continue
            if text.startswith("null", index):
                result.append("None")
                index += 4
                continue

            result.append(char)
            index += 1

        return "".join(result)

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

        adjusted = self._remove_adjacent_overlap(normalized, transcript_segments)
        return self._improve_sequence_continuity(adjusted, transcript_segments)

    def _align_first_cut_to_global_hook(
        self,
        cuts: list[dict],
        transcript_segments: list[dict],
        post_payload: dict,
    ) -> list[dict]:
        if not cuts or not transcript_segments:
            return cuts

        hook_text = str(post_payload.get("hook") or "").strip()
        if not hook_text:
            return cuts

        first = dict(cuts[0])
        first_start = float(first.get("safe_start", first.get("start", 0.0)))
        first_end = float(first.get("safe_end", first.get("end", 0.0)))
        explicit_hook_start = self._coerce_optional_float(post_payload.get("hook_start"))
        explicit_hook_end = self._coerce_optional_float(post_payload.get("hook_end"))
        if explicit_hook_start is not None and explicit_hook_end is not None:
            hook_start = explicit_hook_start
            hook_end = explicit_hook_end
        else:
            if self._text_belongs_to_cut(hook_text, first):
                return cuts

            hook_segment = self._find_best_matching_segment(hook_text, transcript_segments)
            if hook_segment is None:
                return cuts

            hook_start = float(hook_segment.get("start", first_start))
            hook_end = float(hook_segment.get("end", first_end))
        if hook_start > first_end or hook_end < first_start:
            return cuts

        new_start = min(first_start, hook_start)
        max_duration = float(settings.qa_max_clip_duration_sec)
        new_end = first_end
        if (new_end - new_start) > max_duration:
            new_end = min(first_end, new_start + max_duration)

        first["start"] = round(new_start, 2)
        first["safe_start"] = round(new_start, 2)
        first["end"] = round(new_end, 2)
        first["safe_end"] = round(new_end, 2)

        updated = [first, *cuts[1:]]
        return self._remove_adjacent_overlap(updated, transcript_segments)

    def _reconcile_post_hook_to_transcript(
        self,
        post_payload: dict,
        cuts: list[dict],
        transcript_segments: list[dict],
    ) -> dict:
        if not post_payload or not cuts or not transcript_segments:
            return post_payload

        hook_text = str(post_payload.get("hook") or "").strip()
        if not hook_text:
            return post_payload

        source_cut_index = self._resolve_hook_cut_index(post_payload, cuts)
        source_cut = cuts[source_cut_index]
        explicit_hook_start = self._coerce_optional_float(post_payload.get("hook_start"))
        explicit_hook_end = self._coerce_optional_float(post_payload.get("hook_end"))
        cut_start = float(source_cut.get("safe_start", source_cut.get("start", 0.0)) or 0.0)
        cut_end = float(source_cut.get("safe_end", source_cut.get("end", 0.0)) or 0.0)

        if (
            explicit_hook_start is not None
            and explicit_hook_end is not None
            and explicit_hook_end > explicit_hook_start
            and explicit_hook_start >= cut_start
            and explicit_hook_end <= cut_end
        ):
            updated = dict(post_payload)
            tightened = self._tighten_hook_window_to_text(
                hook_text=hook_text,
                cut=source_cut,
                transcript_segments=transcript_segments,
                fallback_start=explicit_hook_start,
                fallback_end=explicit_hook_end,
            )
            updated["hook_start"] = round(float(tightened["start"]), 2)
            updated["hook_end"] = round(float(tightened["end"]), 2)
            updated["hook_source_cut_index"] = source_cut_index
            return updated

        matched_segment = self._find_best_matching_segment_in_cut(
            hook_text=hook_text,
            cut=source_cut,
            transcript_segments=transcript_segments,
        )
        if matched_segment is None:
            return post_payload

        updated = dict(post_payload)
        updated["hook"] = str(matched_segment.get("text") or hook_text).strip()
        tightened = self._tighten_hook_window_to_text(
            hook_text=hook_text,
            cut=source_cut,
            transcript_segments=transcript_segments,
            fallback_start=float(matched_segment.get("start", 0.0)),
            fallback_end=float(matched_segment.get("end", 0.0)),
        )
        updated["hook_start"] = round(float(tightened["start"]), 2)
        updated["hook_end"] = round(float(tightened["end"]), 2)
        updated["hook_source_cut_index"] = source_cut_index
        return updated

    def _tighten_hook_window_to_text(
        self,
        *,
        hook_text: str,
        cut: dict,
        transcript_segments: list[dict],
        fallback_start: float,
        fallback_end: float,
    ) -> dict:
        matched_segment = self._find_best_matching_segment_in_cut(
            hook_text=hook_text,
            cut=cut,
            transcript_segments=transcript_segments,
        )
        if matched_segment is None:
            return {"start": fallback_start, "end": fallback_end}

        tightened = self._estimate_text_window_within_segment(hook_text, matched_segment)
        if tightened is None:
            return {
                "start": float(matched_segment.get("start", fallback_start)),
                "end": float(matched_segment.get("end", fallback_end)),
            }

        start = max(float(matched_segment.get("start", fallback_start)), float(tightened["start"]))
        end = min(float(matched_segment.get("end", fallback_end)), float(tightened["end"]))
        if end <= start:
            return {"start": fallback_start, "end": fallback_end}
        return {"start": start, "end": end}

    def _estimate_text_window_within_segment(self, hook_text: str, segment: dict) -> dict | None:
        segment_text = str(segment.get("text") or "").strip()
        if not hook_text or not segment_text:
            return None

        normalized_segment, segment_map = self._normalized_text_with_map(segment_text)
        normalized_hook, _ = self._normalized_text_with_map(hook_text)
        if not normalized_segment or not normalized_hook:
            return None

        start_idx = normalized_segment.find(normalized_hook)
        if start_idx < 0:
            return None

        end_idx = start_idx + len(normalized_hook)
        segment_duration = float(segment.get("end", 0.0)) - float(segment.get("start", 0.0))
        if segment_duration <= 0:
            return None

        start_ratio = segment_map[start_idx] / max(1, len(segment_text))
        end_anchor = segment_map[min(len(segment_map) - 1, end_idx - 1)] + 1
        end_ratio = end_anchor / max(1, len(segment_text))
        segment_start = float(segment.get("start", 0.0))
        segment_end = float(segment.get("end", 0.0))

        estimated_start = segment_start + (segment_duration * start_ratio)
        estimated_end = segment_start + (segment_duration * end_ratio)
        estimated_start = max(segment_start, estimated_start)
        estimated_end = min(segment_end, estimated_end)
        if estimated_end <= estimated_start:
            return None
        return {"start": estimated_start, "end": estimated_end}

    def _normalized_text_with_map(self, text: str) -> tuple[str, list[int]]:
        normalized_chars: list[str] = []
        index_map: list[int] = []
        previous_space = True

        for index, char in enumerate(text.lower()):
            normalized_char = char if char.isalnum() else " "
            if normalized_char == " ":
                if previous_space:
                    continue
                previous_space = True
            else:
                previous_space = False

            normalized_chars.append(normalized_char)
            index_map.append(index)

        normalized = "".join(normalized_chars).strip()
        if not normalized:
            return "", []

        leading_trim = len("".join(normalized_chars)) - len("".join(normalized_chars).lstrip())
        if leading_trim > 0:
            index_map = index_map[leading_trim:]
        return normalized, index_map

    def _prune_disconnected_short_serie_cuts(self, cuts: list[dict]) -> list[dict]:
        if self.clip_mode != "short_serie" or len(cuts) < 2:
            return cuts

        ordered = sorted((dict(cut) for cut in cuts), key=lambda item: float(item["start"]))
        selected = [ordered[0]]
        max_soft_gap = min(float(settings.short_serie_max_gap_sec), 12.0)

        for candidate in ordered[1:]:
            previous = selected[-1]
            gap = float(candidate["start"]) - float(previous["end"])
            if gap > max_soft_gap and self._cut_pair_feels_disconnected(previous, candidate):
                break
            selected.append(candidate)

        return selected

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

    def _remove_adjacent_overlap(
        self,
        cuts: list[dict],
        transcript_segments: list[dict],
    ) -> list[dict]:
        if len(cuts) < 2:
            return cuts

        adjusted: list[dict] = []
        previous_end = 0.0

        for index, cut in enumerate(cuts):
            current = dict(cut)
            start = float(current.get("start", 0.0))
            end = float(current.get("end", 0.0))

            if index > 0 and start < previous_end:
                deduped_start = self._find_next_segment_start(transcript_segments, previous_end) or previous_end
                start = max(start, deduped_start)
                if end - start < settings.render_min_clip_duration_sec:
                    end = self._extend_end_to_min_duration(
                        transcript_segments,
                        start,
                        end,
                    )

            current["start"] = round(start, 2)
            current["end"] = round(end, 2)
            current["safe_start"] = round(start, 2)
            current["safe_end"] = round(end, 2)
            adjusted.append(current)
            previous_end = end

        return adjusted

    def _find_next_segment_start(
        self,
        transcript_segments: list[dict],
        timestamp: float,
    ) -> float | None:
        for segment in transcript_segments:
            segment_start = float(segment.get("start", 0.0))
            if segment_start >= timestamp:
                return segment_start
        return None

    def _improve_sequence_continuity(
        self,
        cuts: list[dict],
        transcript_segments: list[dict],
    ) -> list[dict]:
        if not cuts or not transcript_segments:
            return cuts

        adjusted = [dict(cut) for cut in cuts]
        bridge_gap_limit = float(settings.render_sequence_bridge_max_gap_sec)
        max_duration = float(settings.qa_max_clip_duration_sec)

        for index in range(len(adjusted) - 1):
            current = adjusted[index]
            nxt = adjusted[index + 1]

            if not self._cuts_share_sequence_context(current, nxt):
                continue

            current_start = float(current.get("start", 0.0))
            current_end = float(current.get("end", 0.0))
            next_start = float(nxt.get("start", 0.0))
            gap = next_start - current_end

            if gap <= 0 or gap > bridge_gap_limit:
                continue

            if not self._cut_needs_context_bridge(nxt, transcript_segments):
                continue

            bridged_end = min(next_start, current_start + max_duration)
            if bridged_end <= current_end:
                continue

            current["end"] = round(bridged_end, 2)
            current["safe_end"] = round(bridged_end, 2)

            next_start = self._backfill_cut_start_for_context(
                cut=nxt,
                previous_end=bridged_end,
                transcript_segments=transcript_segments,
            )
            if next_start is not None:
                nxt["start"] = round(next_start, 2)
                nxt["safe_start"] = round(next_start, 2)

        for index in range(1, len(adjusted)):
            previous = adjusted[index - 1]
            current = adjusted[index]
            if not self._cut_needs_context_bridge(current, transcript_segments):
                continue

            next_start = self._backfill_cut_start_for_context(
                cut=current,
                previous_end=float(previous.get("end", 0.0)),
                transcript_segments=transcript_segments,
            )
            if next_start is not None:
                current["start"] = round(next_start, 2)
                current["safe_start"] = round(next_start, 2)

        last = adjusted[-1]
        last_start = float(last.get("start", 0.0))
        last_end = float(last.get("end", 0.0))
        extended_end = self._extend_last_cut_for_closure(
            transcript_segments,
            start=last_start,
            current_end=last_end,
        )
        if extended_end > last_end:
            last["end"] = round(extended_end, 2)
            last["safe_end"] = round(extended_end, 2)

        return adjusted

    def _compact_low_signal_spans(
        self,
        cuts: list[dict],
        transcript_segments: list[dict],
    ) -> list[dict]:
        if not cuts or not transcript_segments:
            return cuts

        adjusted: list[dict] = []
        min_internal = float(settings.render_min_internal_cut_duration_sec)

        for cut in cuts:
            meaningful_ranges = self._meaningful_ranges_for_cut(cut, transcript_segments)
            if not meaningful_ranges:
                adjusted.append(cut)
                continue

            if len(meaningful_ranges) == 1 or any((end - start) < min_internal for start, end in meaningful_ranges):
                trimmed = dict(cut)
                trimmed["start"] = round(meaningful_ranges[0][0], 2)
                trimmed["safe_start"] = round(meaningful_ranges[0][0], 2)
                trimmed["end"] = round(meaningful_ranges[-1][1], 2)
                trimmed["safe_end"] = round(meaningful_ranges[-1][1], 2)
                adjusted.append(trimmed)
                continue

            for index, (start, end) in enumerate(meaningful_ranges):
                if (end - start) < min_internal:
                    continue
                fragment = dict(cut)
                fragment["start"] = round(start, 2)
                fragment["safe_start"] = round(start, 2)
                fragment["end"] = round(end, 2)
                fragment["safe_end"] = round(end, 2)
                fragment["transition_after"] = (
                    "fade" if index < len(meaningful_ranges) - 1 else str(cut.get("transition_after") or "fade")
                )
                adjusted.append(fragment)

        return adjusted or cuts

    def _meaningful_ranges_for_cut(
        self,
        cut: dict,
        transcript_segments: list[dict],
    ) -> list[tuple[float, float]]:
        cut_start = float(cut.get("safe_start", cut.get("start", 0.0)))
        cut_end = float(cut.get("safe_end", cut.get("end", 0.0)))
        if cut_end <= cut_start:
            return []

        segments = [
            segment
            for segment in transcript_segments
            if float(segment.get("end", 0.0)) > cut_start and float(segment.get("start", 0.0)) < cut_end
        ]
        if not segments:
            return []

        meaningful_segments = [
            segment
            for segment in segments
            if not self._segment_is_low_signal(segment)
        ]
        if not meaningful_segments:
            return []

        ranges: list[tuple[float, float]] = []
        current_start = max(cut_start, float(meaningful_segments[0].get("start", cut_start)) - 0.15)
        current_end = min(cut_end, float(meaningful_segments[0].get("end", cut_end)) + 0.12)
        split_gap = 1.35

        for segment in meaningful_segments[1:]:
            segment_start = max(cut_start, float(segment.get("start", cut_start)) - 0.15)
            segment_end = min(cut_end, float(segment.get("end", cut_end)) + 0.12)
            if segment_start - current_end > split_gap:
                ranges.append((current_start, current_end))
                current_start = segment_start
                current_end = segment_end
                continue

            current_end = max(current_end, segment_end)

        ranges.append((current_start, current_end))
        return ranges

    def _segment_is_low_signal(self, segment: dict | None) -> bool:
        if not segment:
            return False

        text = str(segment.get("text") or "").strip().lower()
        duration = max(0.0, float(segment.get("end", 0.0)) - float(segment.get("start", 0.0)))
        if not text:
            return duration >= 0.9

        filler_words = {
            "é",
            "eh",
            "e",
            "ah",
            "aham",
            "hum",
            "uh",
            "hã",
            "hãm",
            "né",
            "tipo",
            "tá",
            "ta",
            "bom",
        }
        laugh_markers = ("risos", "haha", "kkkk", "kkk", "rsrs")
        tokens = [token for token in re.findall(r"\w+", text) if token]
        if not tokens:
            return duration >= 0.9

        unique_tokens = set(tokens)
        if any(marker in text for marker in laugh_markers):
            return True
        if len(tokens) <= 4 and unique_tokens.issubset(filler_words):
            return True
        if len(unique_tokens) == 1 and next(iter(unique_tokens)) in filler_words:
            return True
        if duration >= 1.4 and len(tokens) <= 3 and unique_tokens.issubset(filler_words | {"sim", "não", "nao"}):
            return True

        return False

    def _cuts_share_sequence_context(self, left: dict, right: dict) -> bool:
        if self.clip_mode == "short_serie":
            return True

        left_group = str(left.get("merge_group") or "").strip()
        right_group = str(right.get("merge_group") or "").strip()
        return bool(left_group and left_group == right_group)

    def _cut_needs_context_bridge(
        self,
        cut: dict,
        transcript_segments: list[dict],
    ) -> bool:
        segment = self._find_segment_covering(
            transcript_segments,
            float(cut.get("start", 0.0)),
        )
        if segment is None:
            return False

        text = str(segment.get("text") or "").strip().lower()
        if not text:
            return False

        if re.match(r"^(e|mas|ent[aã]o|porque|por isso|s[oó] que|a[ií])\b", text):
            return True

        if text.startswith(("ele ", "ela ", "isso ", "essa ", "esse ", "aí ", "daí ")):
            return True

        return False

    def _extend_last_cut_for_closure(
        self,
        transcript_segments: list[dict],
        *,
        start: float,
        current_end: float,
    ) -> float:
        if self._has_strong_closing_near_timestamp(transcript_segments, current_end):
            return current_end

        max_duration = float(settings.qa_max_clip_duration_sec)
        max_extension = float(settings.render_final_closure_extension_max_sec)
        max_end = min(start + max_duration, current_end + max_extension)

        extension_candidate = current_end
        for segment in transcript_segments:
            segment_start = float(segment.get("start", 0.0))
            segment_end = float(segment.get("end", 0.0))
            if segment_end <= current_end or segment_start < current_end:
                continue
            if segment_end > max_end:
                break

            extension_candidate = segment_end
            if self._segment_has_strong_ending(segment):
                return segment_end

        previous_strong_end = self._find_previous_strong_ending(
            transcript_segments,
            start=start,
            current_end=current_end,
        )
        if previous_strong_end is not None:
            return previous_strong_end

        return current_end

    def _has_strong_closing_near_timestamp(
        self,
        transcript_segments: list[dict],
        timestamp: float,
    ) -> bool:
        segment = self._find_segment_covering(transcript_segments, timestamp)
        if segment is None:
            for item in transcript_segments:
                if float(item.get("end", 0.0)) >= timestamp:
                    segment = item
                    break
        return self._segment_has_strong_ending(segment) if segment is not None else False

    def _segment_has_strong_ending(self, segment: dict | None) -> bool:
        if not segment:
            return False

        text = str(segment.get("text") or "").strip().lower()
        if not text:
            return False

        if re.search(r"[.!?]\s*$", text):
            return True

        if re.search(r"\b(no final|por isso|por conta disso|a verdade|ou seja|entendeu)\b", text):
            return True

        return False

    def _backfill_cut_start_for_context(
        self,
        *,
        cut: dict,
        previous_end: float,
        transcript_segments: list[dict],
    ) -> float | None:
        current_start = float(cut.get("start", 0.0))
        if current_start <= previous_end:
            return None

        previous_segment_start = self._find_previous_segment_start(
            transcript_segments,
            current_start,
        )
        if previous_segment_start is None:
            return None

        max_backfill = float(settings.render_context_backfill_max_sec)
        backfill_start = previous_segment_start
        for segment in reversed(transcript_segments):
            segment_start = float(segment.get("start", 0.0))
            if segment_start >= current_start:
                continue
            if (current_start - segment_start) > max_backfill:
                break
            backfill_start = segment_start
            if self._segment_starts_cleanly(segment):
                break

        allowed_overlap = 0.6
        return max(backfill_start, previous_end - allowed_overlap)

    def _find_best_matching_segment(
        self,
        target_text: str,
        transcript_segments: list[dict],
    ) -> dict | None:
        target_tokens = self._tokenize_text(target_text)
        if not target_tokens:
            return None

        best_segment = None
        best_score = 0.0
        for segment in transcript_segments:
            segment_text = str(segment.get("text") or "").strip()
            if not segment_text:
                continue

            segment_tokens = self._tokenize_text(segment_text)
            if not segment_tokens:
                continue

            overlap = len(target_tokens & segment_tokens) / max(1, len(target_tokens))
            if overlap > best_score:
                best_score = overlap
                best_segment = segment

        if best_score < 0.34:
            return None
        return best_segment

    def _find_best_matching_segment_in_cut(
        self,
        *,
        hook_text: str,
        cut: dict,
        transcript_segments: list[dict],
    ) -> dict | None:
        cut_start = float(cut.get("safe_start", cut.get("start", 0.0)) or 0.0)
        cut_end = float(cut.get("safe_end", cut.get("end", 0.0)) or 0.0)
        if cut_end <= cut_start:
            return None

        candidate_segments = [
            segment
            for segment in transcript_segments
            if float(segment.get("end", 0.0)) > cut_start and float(segment.get("start", 0.0)) < cut_end
        ]
        if not candidate_segments:
            return None

        return self._find_best_matching_segment(hook_text, candidate_segments)

    def _resolve_hook_cut_index(self, post_payload: dict, cuts: list[dict]) -> int:
        raw_index = post_payload.get("hook_source_cut_index")
        if raw_index in (None, ""):
            return 0
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return 0
        if 0 <= index < len(cuts):
            return index
        if 1 <= index <= len(cuts):
            return index - 1
        return 0

    def _text_belongs_to_cut(self, text: str, cut: dict) -> bool:
        cut_text = str(cut.get("reason") or "") + " " + str(cut.get("hook") or "")
        token_overlap = self._token_overlap_ratio(text, cut_text)
        return token_overlap >= 0.4

    def _cut_pair_feels_disconnected(self, left: dict, right: dict) -> bool:
        left_text = str(left.get("reason") or "") + " " + str(left.get("hook") or "")
        right_text = str(right.get("reason") or "") + " " + str(right.get("hook") or "")
        return self._token_overlap_ratio(left_text, right_text) < 0.16

    def _token_overlap_ratio(self, left_text: str, right_text: str) -> float:
        left_tokens = self._tokenize_text(left_text)
        right_tokens = self._tokenize_text(right_text)
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))

    def _tokenize_text(self, text: str) -> set[str]:
        return {
            token
            for token in "".join(char.lower() if char.isalnum() else " " for char in text).split()
            if len(token) > 2
        }

    def _coerce_optional_float(self, value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _find_previous_segment_start(
        self,
        transcript_segments: list[dict],
        timestamp: float,
    ) -> float | None:
        previous_start = None
        for segment in transcript_segments:
            segment_start = float(segment.get("start", 0.0))
            if segment_start >= timestamp:
                break
            previous_start = segment_start
        return previous_start

    def _segment_starts_cleanly(self, segment: dict | None) -> bool:
        if not segment:
            return False

        text = str(segment.get("text") or "").strip()
        if not text:
            return False

        lowered = text.lower()
        if re.match(r"^(e|mas|ent[aã]o|porque|por isso|s[oó] que|a[ií]|da[ií]|ele|ela|isso|essa|esse)\b", lowered):
            return False

        return True

    def _find_previous_strong_ending(
        self,
        transcript_segments: list[dict],
        *,
        start: float,
        current_end: float,
    ) -> float | None:
        min_duration_end = start + float(settings.render_min_clip_duration_sec)
        candidate = None

        for segment in transcript_segments:
            segment_end = float(segment.get("end", 0.0))
            if segment_end > current_end:
                break
            if segment_end < min_duration_end:
                continue
            if self._segment_has_strong_ending(segment):
                candidate = segment_end

        return candidate

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
        final_clip_files: list[Path],
        final_reel_path: Path | None,
        subtitle_path: Path | None,
        qa_report: dict | None,
        automation_report: dict | None,
        render_plan: dict | None,
    ) -> dict:
        self._mark_step("delivery_package", "started")
        package = self.delivery_package_builder.build(
            job_id=self.job_id,
            clip_mode=self.clip_mode,
            video_ratio=self.video_ratio,
            cuts=filtered_cuts,
            cut_files=cut_files,
            final_clip_files=final_clip_files,
            final_reel_path=final_reel_path,
            subtitle_path=subtitle_path,
            post_payload=self.manual_response.get("post"),
            long_video_script=self.manual_response.get("long_video_script"),
            qa_report=qa_report,
            automation_report=automation_report,
            render_plan=render_plan,
            artifacts_manifest=self.artifacts.read(),
            response_validation=self.manual_response.get("_response_validation"),
            final_video_specs=self.manual_response.get("_final_video_specs"),
        )
        self._mark_step(
            "delivery_package",
            "completed",
            clip_count=package.get("clip_count", 0),
            delivery_status=package.get("delivery_status"),
        )
        return package

    def _build_publish_package(
        self,
        filtered_cuts: list[dict],
        final_clip_files: list[Path],
        final_reel_path: Path | None,
        subtitle_path: Path | None,
        qa_report: dict | None,
        automation_report: dict | None,
    ) -> dict:
        self._mark_step("publish_package", "started")
        package = self.publish_package_builder.build(
            job_id=self.job_id,
            clip_mode=self.clip_mode,
            video_ratio=self.video_ratio,
            cuts=filtered_cuts,
            post_payload=self.manual_response.get("post"),
            final_clip_files=final_clip_files,
            final_reel_path=final_reel_path,
            subtitle_path=subtitle_path,
            qa_report=qa_report,
            automation_report=automation_report,
            final_video_specs=self.manual_response.get("_final_video_specs"),
        )
        self._mark_step(
            "publish_package",
            "completed",
            publish_status=package.get("publish_status"),
        )
        return package

    def _build_final_reel_subtitles(
        self,
        filtered_cuts: list[dict],
        transcript_segments: list[dict],
    ) -> Path | None:
        self._mark_step("final_reel_subtitles", "started")
        output_path = self.work_dir / "final_reel.ass"
        render_plan = self._build_render_plan(filtered_cuts, transcript_segments, None)
        subtitle_path = self.subtitle_builder.build_final_reel_srt(
            cuts=filtered_cuts,
            transcript_segments=transcript_segments,
            output_path=output_path,
            lead_in_sec=self._cold_open_lead_in_seconds(render_plan),
            cold_open=((render_plan.get("clips") or [{}])[0].get("cold_open") if (render_plan.get("clips") or []) else None),
        )
        if subtitle_path is None:
            self._mark_step("final_reel_subtitles", "skipped", reason="no_subtitle_entries")
            return None

        self._mark_step("final_reel_subtitles", "completed", output_path=str(subtitle_path))
        return subtitle_path

    def _build_render_plan(
        self,
        filtered_cuts: list[dict],
        transcript_segments: list[dict],
        qa_report: dict | None,
    ) -> dict:
        self._mark_step("render_plan", "started")
        soundtrack = self.soundtrack_selector.select(
            cuts=filtered_cuts,
            post_payload=self.manual_response.get("post"),
        )
        plan = self.render_plan_builder.build(
            job_id=self.job_id,
            clip_mode=self.clip_mode,
            video_ratio=self.video_ratio,
            cuts=filtered_cuts,
            post_payload=self.manual_response.get("post"),
            transcript_segments=transcript_segments,
            soundtrack=soundtrack,
            qa_report=qa_report,
        )
        self._mark_step("render_plan", "completed", clip_count=len(plan.get("clips", [])))
        return plan

    def _cold_open_lead_in_seconds(self, render_plan: dict | None) -> float:
        clips = list((render_plan or {}).get("clips") or [])
        if not clips:
            return 0.0

        first_clip = clips[0] if isinstance(clips[0], dict) else {}
        cold_open = first_clip.get("cold_open") or {}
        if not cold_open.get("enabled"):
            return 0.0

        try:
            lead_in = max(0.0, float(cold_open.get("duration_sec") or 0.0))
        except (TypeError, ValueError):
            return 0.0
        playback_speed = max(0.5, float(settings.render_playback_speed or 1.0))
        return lead_in / playback_speed

    def _render_final_reel(
        self,
        cut_files: list[Path],
        render_plan: dict,
        subtitle_path: Path | None = None,
    ) -> Path | None:
        self._mark_step("final_reel", "started")
        try:
            output = self.final_renderer.render(
                cut_files=cut_files,
                render_plan=render_plan,
                subtitle_path=subtitle_path,
            )
        except Exception as exc:
            self._mark_step("final_reel", "failed", error=str(exc))
            return None

        if output is None:
            self._mark_step("final_reel", "skipped", reason="no_cut_files")
            return None

        self._mark_step("final_reel", "completed", output_path=str(output))
        return output

    def _render_final_clips(
        self,
        video_path: Path,
        filtered_cuts: list[dict],
        cut_files: list[Path],
        render_plan: dict,
        transcript_segments: list[dict],
    ) -> list[Path]:
        self._mark_step("final_clips", "started")
        final_clips_dir = self.work_dir / "final_clips"
        final_clips_dir.mkdir(parents=True, exist_ok=True)
        final_video_specs = list(self.manual_response.get("_final_video_specs") or [])
        if not final_video_specs:
            final_video_specs = self._build_final_video_specs(transcript_segments)
        if final_video_specs:
            outputs: list[Path] = []
            for index, spec in enumerate(final_video_specs, start=1):
                local_cut_files = self.cutter.cut(video_path, spec["cuts"])
                soundtrack = self.soundtrack_selector.select(
                    cuts=spec["cuts"],
                    post_payload=spec["post"],
                )
                local_render_plan = self.render_plan_builder.build(
                    job_id=self.job_id,
                    clip_mode=self.clip_mode,
                    video_ratio=self.video_ratio,
                    cuts=spec["cuts"],
                    post_payload=spec["post"],
                    transcript_segments=transcript_segments,
                    soundtrack=soundtrack,
                    qa_report=None,
                )
                subtitle_output = final_clips_dir / f"final_clip_{index:02d}.ass"
                clip_subtitle_path = self.subtitle_builder.build_final_reel_srt(
                    cuts=spec["cuts"],
                    transcript_segments=transcript_segments,
                    output_path=subtitle_output,
                    lead_in_sec=self._cold_open_lead_in_seconds(local_render_plan),
                    cold_open=((local_render_plan.get("clips") or [{}])[0].get("cold_open") if (local_render_plan.get("clips") or []) else None),
                )
                rendered_path = self.final_renderer.render(
                    cut_files=local_cut_files,
                    render_plan=local_render_plan,
                    subtitle_path=clip_subtitle_path,
                )
                clip_output = final_clips_dir / f"final_clip_{index:02d}.mp4"
                if rendered_path is not None and rendered_path.exists() and rendered_path != clip_output:
                    rendered_path.replace(clip_output)
                outputs.append(clip_output if clip_output.exists() else rendered_path)

            self._mark_step("final_clips", "completed", clip_count=len(outputs))
            return [path for path in outputs if path is not None]

        clips_plan = {
            int(item.get("clip_index", 0)): item
            for item in render_plan.get("clips", [])
            if item.get("clip_index")
        }
        soundtrack = render_plan.get("soundtrack") or {}
        outputs: list[Path] = []

        for index, (cut, cut_file) in enumerate(zip(filtered_cuts, cut_files), start=1):
            subtitle_output = final_clips_dir / f"final_clip_{index:02d}.ass"
            clip_subtitle_path = self.subtitle_builder.build_clip_srt(
                cut=cut,
                transcript_segments=transcript_segments,
                output_path=subtitle_output,
            )
            clip_output = final_clips_dir / f"final_clip_{index:02d}.mp4"
            rendered_path = self.final_renderer.render_clip(
                input_path=cut_file,
                clip_plan=clips_plan.get(index, {}),
                subtitle_path=clip_subtitle_path,
                soundtrack=soundtrack,
                output_path=clip_output,
            )
            outputs.append(rendered_path)

        self._mark_step("final_clips", "completed", clip_count=len(outputs))
        return outputs

    def _build_final_video_specs(self, transcript_segments: list[dict]) -> list[dict]:
        specs: list[dict] = []
        for index, video in enumerate(self.manual_response.get("final_videos") or [], start=1):
            if not isinstance(video, dict):
                continue

            cuts = list(video.get("shorts_content") or [])
            if not cuts:
                continue

            post = self._normalize_post_payload(self._extract_video_post_payload(video), cuts)
            normalized_cuts = self._normalize_cuts_to_transcript(cuts, transcript_segments)
            post = self._reconcile_post_hook_to_transcript(
                post,
                normalized_cuts,
                transcript_segments,
            )
            normalized_cuts = self._align_first_cut_to_global_hook(
                normalized_cuts,
                transcript_segments,
                post,
            )
            normalized_cuts = self._compact_low_signal_spans(
                normalized_cuts,
                transcript_segments,
            )
            normalized_cuts = self._align_first_cut_to_global_hook(
                normalized_cuts,
                transcript_segments,
                post,
            )
            if self.clip_mode == "short_serie":
                normalized_cuts = self._prune_disconnected_short_serie_cuts(normalized_cuts)

            filtered = []
            for cut in normalized_cuts:
                start = float(cut.get("start", 0.0))
                end = float(cut.get("end", 0.0))
                if end <= start:
                    continue
                if (end - start) < settings.render_min_internal_cut_duration_sec:
                    continue
                filtered.append(cut)

            if not filtered:
                continue

            filtered = self._split_single_cut_for_short_serie(filtered, transcript_segments)
            filtered = self._strengthen_final_video_cuts(filtered, transcript_segments, post)
            filtered = self._cap_final_video_total_duration(filtered, transcript_segments)
            post = self._reconcile_post_hook_to_transcript(post, filtered, transcript_segments)
            filtered = self._align_first_cut_to_global_hook(filtered, transcript_segments, post)
            post = self._strengthen_post_hook(post, filtered, transcript_segments)
            filtered = self._align_first_cut_to_global_hook(filtered, transcript_segments, post)
            filtered = self._assign_default_transitions(filtered)

            specs.append(
                {
                    "video_index": int(video.get("video_index") or index),
                    "post": post,
                    "cuts": filtered,
                }
            )

        return self._dedupe_final_video_specs(specs, transcript_segments)

    def _dedupe_final_video_specs(
        self,
        specs: list[dict],
        transcript_segments: list[dict],
    ) -> list[dict]:
        if len(specs) < 2:
            return specs

        min_internal = float(settings.render_min_internal_cut_duration_sec)
        overlap_guard_sec = 0.2
        occupied_until = -1.0
        deduped: list[dict] = []

        for spec in specs:
            post = dict(spec.get("post") or {})
            adjusted_cuts: list[dict] = []

            for cut_index, raw_cut in enumerate(spec.get("cuts") or []):
                cut = dict(raw_cut)
                start = float(cut.get("safe_start", cut.get("start", 0.0)))
                end = float(cut.get("safe_end", cut.get("end", 0.0)))
                if end <= start:
                    continue

                if occupied_until >= 0.0 and end <= occupied_until + overlap_guard_sec:
                    continue

                if occupied_until >= 0.0 and start < occupied_until + overlap_guard_sec:
                    proposed_start = occupied_until + overlap_guard_sec
                    hook_start = self._coerce_optional_float(post.get("hook_start"))
                    if cut_index == 0 and hook_start is not None and start <= hook_start <= end:
                        proposed_start = min(proposed_start, hook_start)
                    start = proposed_start

                if (end - start) < min_internal:
                    continue

                cut["start"] = round(start, 2)
                cut["safe_start"] = round(start, 2)
                cut["end"] = round(end, 2)
                cut["safe_end"] = round(end, 2)
                adjusted_cuts.append(cut)

            if not adjusted_cuts:
                continue

            post = self._reconcile_post_hook_to_transcript(post, adjusted_cuts, transcript_segments)
            adjusted_cuts = self._align_first_cut_to_global_hook(adjusted_cuts, transcript_segments, post)
            adjusted_cuts = self._strengthen_final_video_cuts(adjusted_cuts, transcript_segments, post)
            adjusted_cuts = self._cap_final_video_total_duration(adjusted_cuts, transcript_segments)
            post = self._reconcile_post_hook_to_transcript(post, adjusted_cuts, transcript_segments)
            post = self._strengthen_post_hook(post, adjusted_cuts, transcript_segments)
            adjusted_cuts = self._align_first_cut_to_global_hook(adjusted_cuts, transcript_segments, post)

            deduped.append(
                {
                    **spec,
                    "post": post,
                    "cuts": adjusted_cuts,
                }
            )
            occupied_until = max(
                occupied_until,
                max(float(cut.get("safe_end", cut.get("end", 0.0))) for cut in adjusted_cuts),
            )

        return deduped if len(deduped) == len(specs) else specs

    def _split_single_cut_for_short_serie(
        self,
        cuts: list[dict],
        transcript_segments: list[dict],
    ) -> list[dict]:
        if self.clip_mode != "short_serie" or len(cuts) != 1:
            return cuts

        cut = dict(cuts[0])
        start = float(cut.get("safe_start", cut.get("start", 0.0)))
        end = float(cut.get("safe_end", cut.get("end", 0.0)))
        duration = end - start
        min_internal = float(settings.render_min_internal_cut_duration_sec)

        if duration < 62.0:
            return cuts

        midpoint = start + (duration / 2.0)
        candidate_boundaries: list[float] = []

        for segment in transcript_segments:
            segment_start = float(segment.get("start", 0.0))
            segment_end = float(segment.get("end", 0.0))
            if segment_start <= start or segment_end >= end:
                continue
            if (segment_end - start) < min_internal or (end - segment_start) < min_internal:
                continue
            if self._segment_has_strong_ending(segment):
                candidate_boundaries.append(segment_end)
            elif self._segment_starts_cleanly(segment):
                candidate_boundaries.append(segment_start)

        if not candidate_boundaries:
            return cuts

        split_point = min(candidate_boundaries, key=lambda boundary: abs(boundary - midpoint))
        if (split_point - start) < min_internal or (end - split_point) < min_internal:
            return cuts

        first = dict(cut)
        first["end"] = round(split_point, 2)
        first["safe_end"] = round(split_point, 2)
        first["narrative_role"] = str(first.get("narrative_role") or "hook")
        first["continuity_note"] = "primeira parte do mesmo contexto, preparando continuação natural"
        first["transition_after"] = "fade"

        second = dict(cut)
        second["start"] = round(split_point, 2)
        second["safe_start"] = round(split_point, 2)
        second["narrative_role"] = "development"
        second["continuity_note"] = "segunda parte do mesmo contexto, aprofundando e fechando o assunto"
        second["transition_after"] = str(second.get("transition_after") or "fade")

        return [first, second]

    def _strengthen_final_video_cuts(
        self,
        cuts: list[dict],
        transcript_segments: list[dict],
        post: dict,
    ) -> list[dict]:
        if not cuts:
            return cuts

        adjusted = [dict(cut) for cut in cuts]
        total_duration = sum(
            max(
                0.0,
                float(cut.get("safe_end", cut.get("end", 0.0))) - float(cut.get("safe_start", cut.get("start", 0.0))),
            )
            for cut in adjusted
        )

        target_min_total = float(settings.render_min_final_video_duration_sec)
        max_total = float(settings.qa_max_clip_duration_sec)

        last = adjusted[-1]
        last_start = float(last.get("safe_start", last.get("start", 0.0)))
        last_end = float(last.get("safe_end", last.get("end", 0.0)))
        strengthened_end = self._extend_last_cut_for_closure(
            transcript_segments,
            start=last_start,
            current_end=last_end,
        )
        if strengthened_end > last_end:
            last["end"] = round(strengthened_end, 2)
            last["safe_end"] = round(strengthened_end, 2)
            total_duration += strengthened_end - last_end

        if total_duration >= target_min_total:
            return adjusted

        continuation = self._build_followup_cut(
            last_cut=adjusted[-1],
            transcript_segments=transcript_segments,
            remaining_budget=max_total - total_duration,
        )
        if continuation is not None:
            adjusted.append(continuation)

        return adjusted

    def _cap_final_video_total_duration(
        self,
        cuts: list[dict],
        transcript_segments: list[dict],
    ) -> list[dict]:
        if not cuts:
            return cuts

        max_total = float(settings.qa_max_clip_duration_sec)
        adjusted = [dict(cut) for cut in cuts]
        total_duration = sum(
            max(
                0.0,
                float(cut.get("safe_end", cut.get("end", 0.0))) - float(cut.get("safe_start", cut.get("start", 0.0))),
            )
            for cut in adjusted
        )
        if total_duration <= max_total:
            return adjusted

        min_internal = float(settings.render_min_internal_cut_duration_sec)
        overflow = total_duration - max_total

        for index in range(len(adjusted) - 1, -1, -1):
            if overflow <= 0:
                break

            cut = adjusted[index]
            cut_start = float(cut.get("safe_start", cut.get("start", 0.0)))
            cut_end = float(cut.get("safe_end", cut.get("end", 0.0)))
            current_duration = max(0.0, cut_end - cut_start)
            removable = max(0.0, current_duration - min_internal)
            if removable <= 0.0:
                continue

            trim_amount = min(removable, overflow)
            target_end = max(cut_start + min_internal, cut_end - trim_amount)
            strong_ending = self._find_previous_strong_ending(
                transcript_segments,
                start=cut_start,
                current_end=min(cut_end, target_end + 2.0),
            )
            if strong_ending is not None and strong_ending >= cut_start + min_internal:
                target_end = min(target_end, strong_ending)

            new_end = round(min(cut_end, target_end), 2)
            effective_trim = max(0.0, cut_end - new_end)
            if effective_trim <= 0.0:
                continue

            cut["end"] = new_end
            cut["safe_end"] = new_end
            overflow -= effective_trim

        return adjusted

    def _build_followup_cut(
        self,
        *,
        last_cut: dict,
        transcript_segments: list[dict],
        remaining_budget: float,
    ) -> dict | None:
        if remaining_budget < float(settings.render_min_internal_cut_duration_sec):
            return None

        last_end = float(last_cut.get("safe_end", last_cut.get("end", 0.0)))
        next_start = self._find_next_segment_start(transcript_segments, last_end + 0.01)
        if next_start is None:
            return None

        start = next_start
        max_end = start + remaining_budget
        end = None

        for segment in transcript_segments:
            segment_start = float(segment.get("start", 0.0))
            segment_end = float(segment.get("end", 0.0))
            if segment_start < start:
                continue
            if segment_end > max_end:
                break
            end = segment_end
            if (
                segment_end - start >= float(settings.render_min_internal_cut_duration_sec)
                and self._segment_has_strong_ending(segment)
            ):
                break

        if end is None:
            return None

        duration = end - start
        if duration < float(settings.render_min_internal_cut_duration_sec):
            return None

        return {
            **last_cut,
            "start": round(start, 2),
            "end": round(end, 2),
            "safe_start": round(start, 2),
            "safe_end": round(end, 2),
            "reason": str(last_cut.get("reason") or "").strip(),
            "continuity_note": "continuação automática para fechar melhor o raciocínio e aumentar a retenção",
            "transition_after": "fade",
        }

    def _strengthen_post_hook(
        self,
        post: dict,
        cuts: list[dict],
        transcript_segments: list[dict],
    ) -> dict:
        if not cuts:
            return post

        first_cut = cuts[0]
        current_hook = str(post.get("hook") or "").strip()
        explicit_hook_start = self._coerce_optional_float(post.get("hook_start"))
        explicit_hook_end = self._coerce_optional_float(post.get("hook_end"))
        if current_hook and explicit_hook_start is not None and explicit_hook_end is not None:
            return post

        candidate = self._derive_hook_from_cut(first_cut, transcript_segments)
        if candidate:
            updated = dict(post)
            updated["hook"] = candidate["text"]
            updated["hook_source_cut_index"] = 0
            updated["hook_start"] = round(candidate["start"], 2)
            updated["hook_end"] = round(candidate["end"], 2)
            return updated

        if current_hook and self._hook_feels_strong(current_hook):
            return post

        return post

    def _derive_hook_from_cut(self, cut: dict, transcript_segments: list[dict]) -> dict | None:
        start = float(cut.get("safe_start", cut.get("start", 0.0)))
        end = float(cut.get("safe_end", cut.get("end", 0.0)))
        segments = [
            segment
            for segment in transcript_segments
            if float(segment.get("end", 0.0)) > start and float(segment.get("start", 0.0)) < min(end, start + 16.0)
        ]
        best_segment = None
        best_score = -1.0

        for segment in segments:
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            score = 0.0
            if "?" in text:
                score += 2.0
            if any(token in text.lower() for token in ("ninguém", "quem", "por quê", "deep state", "blackrock")):
                score += 1.5
            if self._segment_has_strong_ending(segment):
                score += 1.0
            if len(text.split()) >= 6:
                score += 0.8
            if score > best_score:
                best_score = score
                best_segment = segment

        if best_segment is None:
            return None

        return {
            "text": str(best_segment.get("text") or "").strip(),
            "start": float(best_segment.get("start", start)),
            "end": float(best_segment.get("end", end)),
        }

    def _hook_feels_strong(self, hook: str) -> bool:
        lowered = hook.lower()
        if len(hook.strip()) < 20:
            return False
        if "?" in hook:
            return True
        return any(token in lowered for token in ("ninguém", "deep state", "blackrock", "verdade", "quem"))

    def _assign_default_transitions(self, cuts: list[dict]) -> list[dict]:
        if len(cuts) < 2:
            return cuts

        adjusted = [dict(cut) for cut in cuts]
        for index, cut in enumerate(adjusted[:-1]):
            transition = str(cut.get("transition_after") or "").strip().lower()
            if transition in {"", "none", "hard_cut"}:
                adjusted[index]["transition_after"] = "fade"
        return adjusted

    def _augment_final_video_specs(
        self,
        specs: list[dict],
        transcript_segments: list[dict],
    ) -> list[dict]:
        if len(specs) >= 3:
            return specs[:3]

        candidates = self._load_prepare_candidates()
        if not candidates:
            return specs

        occupied_ranges = []
        for spec in specs:
            for cut in spec.get("cuts", []):
                occupied_ranges.append(
                    (
                        float(cut.get("safe_start", cut.get("start", 0.0))),
                        float(cut.get("safe_end", cut.get("end", 0.0))),
                    )
                )

        next_index = len(specs) + 1
        for candidate in candidates:
            if len(specs) >= 3:
                break

            start = float(candidate.get("start", 0.0))
            end = float(candidate.get("end", 0.0))
            if end <= start:
                continue
            if any(not (end <= existing_start or start >= existing_end) for existing_start, existing_end in occupied_ranges):
                continue

            cut = {
                "start": round(start, 2),
                "end": round(end, 2),
                "safe_start": round(start, 2),
                "safe_end": round(end, 2),
                "reason": str(candidate.get("text") or "").strip(),
                "narrative_role": str(candidate.get("narrative_role") or "development"),
                "merge_group": f"auto_video_{next_index}",
                "continuity_note": "vídeo complementar gerado automaticamente a partir dos melhores candidatos do pipeline",
                "speaker_focus": self._primary_speaker_from_candidate(candidate),
                "transition_after": "fade",
            }
            strengthened = self._strengthen_final_video_cuts([cut], transcript_segments, {})
            post = self._normalize_post_payload({}, strengthened)
            post = self._strengthen_post_hook(post, strengthened, transcript_segments)
            specs.append(
                {
                    "video_index": next_index,
                    "post": post,
                    "cuts": self._assign_default_transitions(strengthened),
                }
            )
            occupied_ranges.extend(
                (
                    float(item.get("safe_start", item.get("start", 0.0))),
                    float(item.get("safe_end", item.get("end", 0.0))),
                )
                for item in strengthened
            )
            next_index += 1

        return specs

    def _load_prepare_candidates(self) -> list[dict]:
        local_path = self.work_dir / "candidates.json"
        if not local_path.exists():
            try:
                self.storage.download(
                    f"jobs/{self.job_id}/candidates.json",
                    str(local_path),
                )
            except Exception:
                return []

        try:
            with open(local_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return []

        return data if isinstance(data, list) else []

    def _primary_speaker_from_candidate(self, candidate: dict) -> str | None:
        speakers = candidate.get("speakers") or []
        if not isinstance(speakers, list) or not speakers:
            return None
        speaker = str(speakers[0]).strip()
        return speaker or None

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
