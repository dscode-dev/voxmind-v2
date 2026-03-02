import json
import shutil
from pathlib import Path

from app.media.downloader import VideoDownloader
from app.media.audio_extractor import AudioExtractor
from app.media.transcriber import Transcriber
from worker.app.pipeline.chunker import Chunker
from worker.app.pipeline.candidate_builder import CandidateBuilder
from worker.app.pipeline.scorer import Scorer
from worker.app.pipeline.manual_prompt_builder import ManualPromptBuilder
from worker.app.integrations.telegram_sender import TelegramSender
from app.video.cutter import VideoCutter
from worker.app.settings import settings

class Pipeline:

    def __init__(self, video_url: str, job_id: str, manual_response: dict | None = None):
        self.video_url = video_url
        self.job_id = job_id
        self.manual_response = manual_response

        self.work_dir = Path(f"/tmp/voxmind/{job_id}")
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.downloader = VideoDownloader(self.work_dir)
        self.extractor = AudioExtractor(self.work_dir)
        self.transcriber = Transcriber()
        self.chunker = Chunker()
        self.builder = CandidateBuilder()
        self.scorer = Scorer()
        self.cutter = VideoCutter(self.work_dir)
        self.telegram = TelegramSender()
        self.prompt_builder = ManualPromptBuilder()

    def run(self):

        try:

            if settings.pipeline_stage == "prepare":
                return self._prepare_stage()

            if settings.pipeline_stage == "finalize":
                return self._finalize_stage()

            raise RuntimeError("Invalid PIPELINE_STAGE")

        except Exception as e:
            return {
                "status": "error",
                "job_id": self.job_id,
                "error": str(e)
            }

        finally:
            if self.work_dir.exists():
                shutil.rmtree(self.work_dir, ignore_errors=True)

    # =========================
    # STAGE 1 - PREPARE
    # =========================
    def _prepare_stage(self):

        video_path = self.downloader.download(self.video_url)
        audio_path = self.extractor.extract(video_path)

        segments = self.transcriber.transcribe(audio_path)
        chunks = self.chunker.chunk(segments)
        candidates = self.builder.build(chunks)

        prompt = self.prompt_builder.build(segments, candidates, self.job_id)

        transcript_path = self.work_dir / "transcript.json"
        candidates_path = self.work_dir / "candidates.json"
        prompt_path = self.work_dir / "prompt.txt"

        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)

        with open(candidates_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f, ensure_ascii=False, indent=2)

        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)

        # Envia arquivos para Telegram
        self.telegram.send_video(str(transcript_path), caption="📄 Transcrição")
        self.telegram.send_video(str(candidates_path), caption="📄 Candidatos")
        self.telegram.send_video(str(prompt_path), caption="📌 Prompt para IA")

        return {
            "status": "awaiting_manual_llm",
            "job_id": self.job_id
        }

    # =========================
    # STAGE 2 - FINALIZE
    # =========================
    def _finalize_stage(self):

        if not self.manual_response:
            raise RuntimeError("Manual LLM response not provided")

        video_path = self.downloader.download(self.video_url)

        cuts = self.manual_response.get("cuts", [])

        cut_files = self.cutter.cut(video_path, cuts)

        title = self.manual_response.get("title", "")
        description = self.manual_response.get("description", "")
        hashtags = " ".join(self.manual_response.get("hashtags", []))

        caption = f"{title}\n\n{description}\n\n{hashtags}"

        for path in cut_files:
            self.telegram.send_video(path, caption=caption)

        return {
            "status": "success",
            "job_id": self.job_id
        }