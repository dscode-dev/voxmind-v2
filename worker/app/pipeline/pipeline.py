import json
import shutil
from pathlib import Path

from app.media.downloader import VideoDownloader
from app.media.transcriber import Transcriber
from app.video.cutter import VideoCutter

from app.pipeline.chunker import Chunker
from app.pipeline.candidate_builder import CandidateBuilder
from app.pipeline.scorer import Scorer
from app.pipeline.manual_prompt_builder import ManualPromptBuilder

from app.integrations.telegram_sender import TelegramSender
from app.settings import settings

from app.storage.minio_client import MinioStorage


class Pipeline:

    def __init__(
        self,
        video_url: str,
        job_id: str,
        manual_response: dict | None = None,
    ):

        self.video_url = video_url
        self.job_id = job_id
        self.manual_response = manual_response

        self.work_dir = Path(f"/tmp/voxmind/{job_id}")
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.storage = MinioStorage()

        self.downloader = VideoDownloader(self.work_dir)

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
    # Logging helper
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
    # Main runner
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

            return {
                "status": "error",
                "job_id": self.job_id,
                "error": str(e),
            }

        finally:

            if self.work_dir.exists():
                shutil.rmtree(self.work_dir, ignore_errors=True)

    # ==================================================
    # STAGE 1 - PREPARE
    # ==================================================

    def _prepare_stage(self):

        self._log("⬇️ Downloading video...")

        video_path = self.downloader.download(self.video_url)

        self._log("💾 Uploading video to storage...")

        self.storage.upload(
            str(video_path),
            f"jobs/{self.job_id}/video.mp4"
        )

        self._log("🧠 Transcribing video...")

        segments = self.transcriber.transcribe(video_path)

        if not segments:
            raise RuntimeError("Transcription returned no segments")

        self._log("✂️ Generating chunks...")

        chunks = self.chunker.chunk(segments)

        self._log("🔥 Extracting candidates...")

        candidates = self.builder.build(chunks)

        self._log("📊 Ranking candidates...")

        ranked = self.scorer.score(candidates)

        self._log("📝 Building LLM prompt...")

        prompt = self.prompt_builder.build(
            segments,
            ranked,
            self.job_id,
        )

        transcript_path = self.work_dir / "transcript.json"
        candidates_path = self.work_dir / "candidates.json"
        prompt_path = self.work_dir / "prompt.txt"

        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, indent=2, ensure_ascii=False)

        with open(candidates_path, "w", encoding="utf-8") as f:
            json.dump(ranked, f, indent=2, ensure_ascii=False)

        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)

        self._log("📤 Sending prompt to Telegram...")

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

        self.telegram.send_message(
            f"""
📊 PIPELINE PRONTO

JOB_ID: {self.job_id}

Envie o arquivo **response.json** retornado pela IA
para continuar o processamento.
"""
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
            raise RuntimeError("Manual response missing")

        self._log("🔎 Validating AI response...")

        try:

            if isinstance(self.manual_response, str):
                self.manual_response = json.loads(self.manual_response)

            text = json.dumps(self.manual_response)

            text = text.replace("“", '"').replace("”", '"')

            self.manual_response = json.loads(text)

        except Exception:
            raise RuntimeError("Invalid JSON received from AI")

        if "shorts_content" not in self.manual_response:
            raise RuntimeError("Invalid response: shorts_content missing")

        self._log("⬇️ Downloading video from storage...")

        video_path = self.work_dir / "video.mp4"

        self.storage.download(
            f"jobs/{self.job_id}/video.mp4",
            str(video_path),
        )

        if not video_path.exists():
            raise RuntimeError("Video not found in storage")

        self._log("🎬 Generating cuts...")

        cuts = self.manual_response.get("shorts_content", [])
        
        if not cuts:
            raise RuntimeError("shorts_content is empty")
        
        # filtra cortes muito curtos
        filtered_cuts = []
        
        for cut in cuts:
        
            start = float(cut["start"])
            end = float(cut["end"])
        
            if end <= start:
                continue
            
            duration = end - start
        
            if duration < 25:
                continue
            
            filtered_cuts.append(cut)
        
        self._log(f"Valid cuts after filtering: {len(filtered_cuts)}")
        
        if not filtered_cuts:
            raise RuntimeError("No valid cuts after filtering")
        
        cut_files = self.cutter.cut(video_path, filtered_cuts)

        self._log(f"📦 {len(cut_files)} cuts generated")

        for path in cut_files:
            self.telegram.send_video(path)

        return {
            "status": "success",
            "job_id": self.job_id,
            "cut_files": cut_files,
        }