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
from app.pipeline.clipsai_candidate_provider import ClipsAICandidateProvider
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
        )
        self.diarizer = SpeakerDiarizer(
            enabled=settings.diarization_enabled,
            model_name=settings.diarization_model_name,
            device=settings.diarization_device,
            hf_token=settings.diarization_hf_token,
        )
        self.transcript_merger = TranscriptSpeakerMerger(
            min_overlap_sec=settings.diarization_min_overlap_sec,
        )

        self.chunker = self._build_chunker()
        self.builder = self._build_candidate_builder()
        self.clipsai_candidate_provider = ClipsAICandidateProvider(
            enabled=settings.clipsai_enabled,
            device=settings.clipsai_device,
            max_candidates=settings.clipsai_max_candidates,
            min_duration_sec=settings.clipsai_min_candidate_duration_sec,
            max_duration_sec=settings.clipsai_max_candidate_duration_sec,
        )
        self.scorer = self._build_scorer()
        self.delivery_package_builder = DeliveryPackageBuilder()
        self.publish_package_builder = PublishPackageBuilder()
        self.soundtrack_selector = SoundtrackSelector()
        self.subtitle_builder = SubtitleBuilder()
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

        self._log("🧩 Segmenting transcript with ClipsAI...")
        self._mark_step("clipsai_candidate_build", "started")

        clipsai_candidates, clipsai_diagnostics = self.clipsai_candidate_provider.build(segments)
        if settings.clipsai_enabled and not clipsai_diagnostics.get("available"):
            reason = clipsai_diagnostics.get("reason", "unknown")
            self._mark_step("clipsai_candidate_build", "failed", reason=reason)
            raise RuntimeError(f"ClipsAI candidate generation failed: {reason}")

        self._mark_step(
            "clipsai_candidate_build",
            "completed",
            candidate_count=len(clipsai_candidates),
            device=clipsai_diagnostics.get("resolved_device"),
        )

        combined_candidates = self._merge_candidate_sources(candidates, clipsai_candidates)

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
        clipsai_candidates_path = self._write_json_artifact(
            "clipsai_candidates.json",
            clipsai_candidates,
            "clipsai_candidates",
        )
        clipsai_diagnostics_path = self._write_json_artifact(
            "clipsai_diagnostics.json",
            clipsai_diagnostics,
            "clipsai_diagnostics",
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
            "clipsai_candidates_path": str(clipsai_candidates_path),
            "clipsai_diagnostics_path": str(clipsai_diagnostics_path),
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
        render_plan = self._build_render_plan(filtered_cuts, transcript_segments, qa_report)
        subtitle_path = self._build_final_reel_subtitles(
            filtered_cuts,
            transcript_segments,
        )
        final_reel_path = self._render_final_reel(cut_files, render_plan, subtitle_path)
        publish_package = self._build_publish_package(
            filtered_cuts,
            final_reel_path,
            subtitle_path,
            qa_report,
            automation_report,
        )
        delivery_package = self._build_delivery_package(
            filtered_cuts,
            cut_files,
            final_reel_path,
            subtitle_path,
            qa_report,
            automation_report,
            render_plan,
        )

        self._mark_step("send_cuts", "started")
        for path in cut_files:
            self.telegram.send_video(path, caption=f"Corte {Path(path).name} - JOB_ID: {self.job_id}")

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

        if final_reel_path is not None and final_reel_path.exists():
            self.artifacts.mark_local(
                "final_reel",
                settings.pipeline_stage,
                final_reel_path,
                artifact_type="video",
            )
            self.telegram.send_video(
                str(final_reel_path),
                caption=str(publish_package.get("telegram_caption") or f"Final reel for JOB_ID: {self.job_id}"),
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
            "final_reel_path": str(final_reel_path) if final_reel_path is not None else None,
            "subtitle_path": str(subtitle_path) if subtitle_path is not None else None,
            "qa_report_path": str(self.work_dir / "qa_report.json") if qa_report is not None else None,
            "render_plan_path": str(render_plan_path),
            "delivery_package_path": str(delivery_package_path),
            "publish_package_path": str(publish_package_path),
            "runtime_status_path": str(self.runtime.runtime_path),
            "artifacts_manifest_path": str(self.artifacts.manifest_path),
        }

    def _normalize_post_payload(self, post_payload: dict | None, cuts: list[dict]) -> dict:
        payload = dict(post_payload or {})
        first_cut = cuts[0] if cuts else {}

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

        for index, cut in enumerate(cuts, start=1):
            start = float(cut.get("start", 0.0) or 0.0)
            end = float(cut.get("end", 0.0) or 0.0)
            if end <= start:
                warnings.append(f"cut_{index}: invalid_range")

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
        title = str(publish_package.get("primary_title") or "Video pronto").strip()
        hook = str(publish_package.get("primary_hook") or "").strip()
        description = str(publish_package.get("description") or "").strip()
        hashtags = " ".join(publish_package.get("hashtags") or [])
        lines = [f"🎯 PUBLICAÇÃO PRONTA", f"JOB_ID: {self.job_id}", "", title]
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
            final_reel_path=final_reel_path,
            subtitle_path=subtitle_path,
            post_payload=self.manual_response.get("post"),
            long_video_script=self.manual_response.get("long_video_script"),
            qa_report=qa_report,
            automation_report=automation_report,
            render_plan=render_plan,
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

    def _build_publish_package(
        self,
        filtered_cuts: list[dict],
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
            final_reel_path=final_reel_path,
            subtitle_path=subtitle_path,
            qa_report=qa_report,
            automation_report=automation_report,
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
        output_path = self.work_dir / "final_reel.srt"
        subtitle_path = self.subtitle_builder.build_final_reel_srt(
            cuts=filtered_cuts,
            transcript_segments=transcript_segments,
            output_path=output_path,
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
            long_video_script=self.manual_response.get("long_video_script"),
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
