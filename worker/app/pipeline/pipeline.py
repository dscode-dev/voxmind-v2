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

    def _log(self, message: str):

        self.telegram.send_message(
            f"""
🧠 VOXMIND PIPELINE

JOB_ID: {self.job_id}

{message}
"""
        )

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

    def _prepare_stage(self):

        self._log("⬇️ Downloading audio...")

        audio_path = self.downloader.download(self.video_url)

        self._log("🧠 Transcribing audio...")

        segments = self.transcriber.transcribe(audio_path)

        self._log("✂️ Generating chunks...")

        chunks = self.chunker.chunk(segments)

        self._log("🔥 Extracting candidates...")

        candidates = self.builder.build(chunks)

        self._log("📊 Ranking candidates...")

        ranked = self.scorer.score(candidates)

        prompt = self.prompt_builder.build(segments, ranked, self.job_id)

        transcript_path = self.work_dir / "transcript.json"
        candidates_path = self.work_dir / "candidates.json"
        prompt_path = self.work_dir / "prompt.txt"

        with open(transcript_path, "w") as f:
            json.dump(segments, f, indent=2, ensure_ascii=False)

        with open(candidates_path, "w") as f:
            json.dump(ranked, f, indent=2, ensure_ascii=False)

        with open(prompt_path, "w") as f:
            f.write(prompt)

        self.telegram.send_document(str(prompt_path), caption="PROMPT")

        return {
            "status": "awaiting_manual_llm",
            "job_id": self.job_id,
            "transcript_path": str(transcript_path),
            "candidates_path": str(candidates_path),
            "prompt_path": str(prompt_path),
        }

    def _finalize_stage(self):

        if not self.manual_response:
            raise RuntimeError("Manual response missing")

        self._log("⬇️ Downloading video for cuts...")

        video_path = self.downloader.download(self.video_url)

        cuts = self.manual_response.get("shorts_content", [])

        cut_files = self.cutter.cut(video_path, cuts)

        for path in cut_files:
            self.telegram.send_video(path)

        return {
            "status": "success",
            "job_id": self.job_id,
            "cut_files": cut_files,
        }