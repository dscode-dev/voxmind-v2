import json
from pathlib import Path

from app.media.downloader import VideoDownloader
from app.media.audio_extractor import AudioExtractor
from app.media.transcriber import Transcriber
from worker.app.pipeline.chunker import Chunker
from worker.app.pipeline.candidate_builder import CandidateBuilder
from worker.app.pipeline.scorer import Scorer
from app.video.cutter import VideoCutter


class Pipeline:

    def __init__(self, video_url: str, job_id: str):
        self.video_url = video_url
        self.job_id = job_id

        self.work_dir = Path(f"/tmp/voxmind/{job_id}")
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.downloader = VideoDownloader(self.work_dir)
        self.extractor = AudioExtractor(self.work_dir)
        self.transcriber = Transcriber()
        self.chunker = Chunker()
        self.builder = CandidateBuilder()
        self.scorer = Scorer()
        self.cutter = VideoCutter(self.work_dir)

    def run(self):

        video_path = self.downloader.download(self.video_url)
        audio_path = self.extractor.extract(video_path)

        segments = self.transcriber.transcribe(audio_path)

        chunks = self.chunker.chunk(segments)

        candidates = self.builder.build(chunks)

        top_cuts = self.scorer.score(candidates)

        cut_files = self.cutter.cut(video_path, top_cuts)

        transcript_path = self.work_dir / "transcript.json"
        cuts_path = self.work_dir / "cuts.json"

        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)

        with open(cuts_path, "w", encoding="utf-8") as f:
            json.dump(top_cuts, f, ensure_ascii=False, indent=2)

        return {
            "transcript_path": str(transcript_path),
            "cuts_path": str(cuts_path),
            "cut_files": [str(p) for p in cut_files]
        }