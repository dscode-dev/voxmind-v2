import json
import os
import logging
from pathlib import Path

from ..storage.minio_client import MinioStorage
from ..services.video_downloader import VideoDownloader
from ..services.transcriber import Transcriber
from ..services.candidate_ranker import CandidateRanker
from ..services.manual_prompt_builder import ManualPromptBuilder
from ..services.api_prompt_builder import ApiPromptBuilder
from ..services.ai_client import AIClient


logger = logging.getLogger(__name__)


class PreparePipeline:

    def __init__(self):

        self.storage = MinioStorage()
        self.downloader = VideoDownloader()
        self.transcriber = Transcriber()
        self.ranker = CandidateRanker()
        self.prompt_builder = ManualPromptBuilder()
        self.api_prompt_builder = ApiPromptBuilder()
        self.ai_client = AIClient()

    def run(
        self,
        video_url: str,
        job_id: str,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
        build_ia: bool = False,
    ):

        workdir = f"/tmp/{job_id}"

        Path(workdir).mkdir(parents=True, exist_ok=True)

        video_path = f"{workdir}/video.mp4"

        logger.info("Downloading video")

        self.downloader.download(video_url, video_path)

        self.storage.upload(
            video_path,
            f"jobs/{job_id}/video.mp4"
        )

        logger.info("Transcribing")

        transcript = self.transcriber.transcribe(video_path)

        transcript_path = f"{workdir}/transcript.json"

        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False, indent=2)

        self.storage.upload(
            transcript_path,
            f"jobs/{job_id}/transcript.json"
        )

        logger.info("Ranking candidates")

        candidates = self.ranker.rank(transcript)
        span_catalog = []
        hook_candidates = []

        candidates_path = f"{workdir}/candidates.json"

        with open(candidates_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f, ensure_ascii=False, indent=2)

        self.storage.upload(
            candidates_path,
            f"jobs/{job_id}/candidates.json"
        )

        if build_ia:

            logger.info("Building API prompt")

            api_prompt = self.api_prompt_builder.build(
                transcript=transcript,
                candidates=candidates,
                span_catalog=span_catalog,
                hook_candidates=hook_candidates,
                job_id=job_id,
                clip_mode=clip_mode,
                video_ratio=video_ratio,
            )

            api_prompt_path = f"{workdir}/api_prompt.txt"

            with open(api_prompt_path, "w", encoding="utf-8") as f:
                f.write(api_prompt)

            self.storage.upload(
                api_prompt_path,
                f"jobs/{job_id}/api_prompt.txt"
            )

            logger.info("Sending prompt to AI API")

            ai_response = self.ai_client.generate(api_prompt)

            ai_response_path = f"{workdir}/ai_response.json"

            with open(ai_response_path, "w", encoding="utf-8") as f:
                f.write(ai_response)

            self.storage.upload(
                ai_response_path,
                f"jobs/{job_id}/ai_response.json"
            )

            logger.info("Prepare pipeline finished with API AI mode")
            return

        logger.info("Building manual prompt")

        prompt = self.prompt_builder.build(
            transcript=transcript,
            candidates=candidates,
            span_catalog=span_catalog,
            hook_candidates=hook_candidates,
            job_id=job_id,
            clip_mode=clip_mode,
            video_ratio=video_ratio,
        )

        prompt_path = f"{workdir}/prompt.txt"

        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)

        self.storage.upload(
            prompt_path,
            f"jobs/{job_id}/prompt.txt"
        )

        logger.info("Prepare pipeline finished in manual mode")
