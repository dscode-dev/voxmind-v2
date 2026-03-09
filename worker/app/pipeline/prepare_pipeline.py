import json
import os
import logging
from pathlib import Path

from ..storage.minio_client import MinioStorage
from ..services.video_downloader import VideoDownloader
from ..services.transcriber import Transcriber
from ..services.candidate_ranker import CandidateRanker
from ..services.manual_prompt_builder import ManualPromptBuilder


logger = logging.getLogger(__name__)


class PreparePipeline:

    def __init__(self):

        self.storage = MinioStorage()
        self.downloader = VideoDownloader()
        self.transcriber = Transcriber()
        self.ranker = CandidateRanker()
        self.prompt_builder = ManualPromptBuilder()

    def run(self, video_url: str, job_id: str):

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

        with open(transcript_path, "w") as f:
            json.dump(transcript, f)

        self.storage.upload(
            transcript_path,
            f"jobs/{job_id}/transcript.json"
        )

        logger.info("Ranking candidates")

        candidates = self.ranker.rank(transcript)

        candidates_path = f"{workdir}/candidates.json"

        with open(candidates_path, "w") as f:
            json.dump(candidates, f)

        self.storage.upload(
            candidates_path,
            f"jobs/{job_id}/candidates.json"
        )

        logger.info("Building prompt")

        prompt = self.prompt_builder.build(
            transcript,
            candidates,
            job_id
        )

        prompt_path = f"{workdir}/prompt.txt"

        with open(prompt_path, "w") as f:
            f.write(prompt)

        self.storage.upload(
            prompt_path,
            f"jobs/{job_id}/prompt.txt"
        )

        logger.info("Prepare pipeline finished")