import subprocess
from pathlib import Path
from typing import List, Dict


class VideoCutter:

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir

    def cut(self, video_path: Path, cuts: List[Dict]) -> List[Path]:

        output_files = []

        for index, cut in enumerate(cuts, start=1):

            output_path = self.work_dir / f"cut_{index:02d}.mp4"

            start = cut["start"]
            duration = cut["end"] - cut["start"]

            command = [
                "ffmpeg",
                "-ss", str(start),
                "-i", str(video_path),
                "-t", str(duration),
                "-c", "copy",
                str(output_path),
                "-y"
            ]

            subprocess.run(command, check=True)

            output_files.append(output_path)

        return output_files