import json
import shutil
from pathlib import Path

from app.media.downloader import VideoDownloader
from app.media.audio_extractor import AudioExtractor
from app.media.transcriber import Transcriber
from app.video.cutter import VideoCutter

from app.pipeline.chunker import Chunker
from app.pipeline.candidate_builder import CandidateBuilder
from app.pipeline.scorer import Scorer
from app.pipeline.manual_prompt_builder import ManualPromptBuilder

from app.integrations.telegram_sender import TelegramSender
from app.settings import settings


class Pipeline:

    def __init__(
        self, video_url: str, job_id: str, manual_response: dict | None = None
    ):

        self.video_url = video_url
        self.job_id = job_id
        self.manual_response = manual_response

        self.work_dir = Path(f"/tmp/voxmind/{job_id}")
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.downloader = VideoDownloader(self.work_dir)
        self.extractor = AudioExtractor(self.work_dir)
        self.transcriber = Transcriber(
            model_size=settings.asr_model_size,
            compute_type=settings.asr_compute_type,
            language=settings.asr_language,
            beam_size=settings.asr_beam_size,
            vad_filter=settings.asr_vad_filter,
        )

        self.chunker = Chunker()
        self.builder = CandidateBuilder()
        self.scorer = Scorer()

        self.cutter = VideoCutter(self.work_dir)

        self.telegram = TelegramSender()
        self.prompt_builder = ManualPromptBuilder()

    # ==================================================
    # Internal helper
    # ==================================================

    def _log(self, message: str):

        self.telegram.send_message(
            f"""
🧠 VOXMIND PIPELINE

JOB_ID: {self.job_id}

{message}
"""
        )

    # ==================================================
    # Main execution
    # ==================================================

    def run(self):

        try:

            if settings.pipeline_stage == "prepare":
                return self._prepare_stage()

            if settings.pipeline_stage == "finalize":
                return self._finalize_stage()

            raise RuntimeError("Invalid PIPELINE_STAGE")

        except Exception as e:

            self.telegram.send_message(
                f"""
❌ VOXMIND ERROR

JOB_ID: {self.job_id}

ERROR:
{str(e)}
"""
            )

            return {"status": "error", "job_id": self.job_id, "error": str(e)}

        finally:

            if self.work_dir.exists():
                shutil.rmtree(self.work_dir, ignore_errors=True)

    # ==================================================
    # STAGE 1 - PREPARE
    # ==================================================

    def _prepare_stage(self):

        self.telegram.send_message(
            f"""
🧠 VOXMIND JOB STARTED

JOB_ID: {self.job_id}
VIDEO: {self.video_url}
"""
        )

        # -----------------------------------
        # Download
        # -----------------------------------

        self._log("⬇️ Downloading video...")

        video_path = self.downloader.download(self.video_url)

        # -----------------------------------
        # Extract audio
        # -----------------------------------

        self._log("🎧 Extracting audio...")

        audio_path = self.extractor.extract(video_path)

        # -----------------------------------
        # Transcription
        # -----------------------------------

        self._log("🧠 Transcribing audio...")

        segments = self.transcriber.transcribe(audio_path)

        # -----------------------------------
        # Chunk generation
        # -----------------------------------

        self._log("✂️ Generating chunks...")

        chunks = self.chunker.chunk(segments)

        # -----------------------------------
        # Candidate detection
        # -----------------------------------

        self._log("🔥 Extracting viral candidates...")

        candidates = self.builder.build(chunks)

        # -----------------------------------
        # Ranking
        # -----------------------------------

        self._log("📊 Ranking candidates...")

        ranked_candidates = self.scorer.score(candidates)

        # -----------------------------------
        # Build prompt
        # -----------------------------------

        self._log("🧠 Preparing AI prompt...")

        prompt = self.prompt_builder.build(segments, ranked_candidates, self.job_id)

        transcript_path = self.work_dir / "transcript.json"
        candidates_path = self.work_dir / "candidates.json"
        prompt_path = self.work_dir / "prompt.txt"

        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)

        with open(candidates_path, "w", encoding="utf-8") as f:
            json.dump(ranked_candidates, f, ensure_ascii=False, indent=2)

        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)

        # -----------------------------------
        # Send instructions
        # -----------------------------------

        self.telegram.send_message(
            f"""
📊 PIPELINE READY

JOB_ID: {self.job_id}

NEXT STEP

1) Copie o PROMPT enviado abaixo
2) Cole em ChatGPT / Claude / Gemini
3) Envie a resposta aqui usando:

/finalize {self.job_id}
{{JSON}}
"""
        )

        # -----------------------------------
        # Send files
        # -----------------------------------

        self.telegram.send_document(
            str(transcript_path), caption=f"📄 Transcript — JOB_ID: {self.job_id}"
        )

        self.telegram.send_document(
            str(candidates_path), caption=f"📄 Candidates — JOB_ID: {self.job_id}"
        )

        self.telegram.send_document(
            str(prompt_path), caption=f"📌 Prompt — JOB_ID: {self.job_id}"
        )

        return {
            "status": "awaiting_manual_llm",
            "job_id": self.job_id,
            "transcript_path": str(transcript_path),
            "candidates_path": str(candidates_path),
            "prompt_path": str(prompt_path),
        }

    # ==================================================
    # STAGE 2 - FINALIZE
    # ==================================================

    def _finalize_stage(self):

        if not self.manual_response:
            raise RuntimeError("Manual LLM response not provided")

        self._log("🎬 Generating final cuts...")

        video_path = self.downloader.download(self.video_url)

        cuts = self.manual_response.get("shorts_content", [])

        cut_files = self.cutter.cut(video_path, cuts)

        first_item = self.manual_response.get("shorts_content", [{}])[0]

        title = first_item.get("title", "")
        description = first_item.get("description", "")
        hashtags = " ".join(first_item.get("hashtags", []))

        caption = f"{title}\n\n{description}\n\n{hashtags}"

        for path in cut_files:

            self.telegram.send_video(path, caption=caption)

        self.telegram.send_message(
            f"""
✅ VOXMIND JOB COMPLETED

JOB_ID: {self.job_id}

Cortes enviados.
"""
        )

        return {"status": "success", "job_id": self.job_id, "cut_files": cut_files}
