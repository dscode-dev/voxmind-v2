from pathlib import Path
from typing import List, Dict

from app.runtime.subprocess_runner import run_ffmpeg


class VideoCutter:

    def __init__(self, work_dir: Path, min_clip_duration_sec: int = 25, job_id: str | None = None):
        self.work_dir = work_dir
        self.min_clip_duration_sec = min_clip_duration_sec
        self.job_id = job_id

    def cut(self, video_path: Path, cuts: List[Dict]) -> List[Path]:

        output_files = []

        for index, cut in enumerate(cuts, start=1):

            start = float(cut["start"])
            end = float(cut["end"])

            duration = end - start

            if duration < self.min_clip_duration_sec:
                continue

            output_path = self.work_dir / f"cut_{index:02d}.mp4"

            command = [
                "ffmpeg",
                "-y",
                "-ss", str(start),
                "-to", str(end),
                "-i", str(video_path),
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
                str(output_path),
            ]

            run_ffmpeg(command, step="cut_clip", job_id=self.job_id)

            output_files.append(output_path)

        return output_files
