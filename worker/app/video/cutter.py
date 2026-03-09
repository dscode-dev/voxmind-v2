import subprocess
from pathlib import Path
from typing import List, Dict


class VideoCutter:

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir

    def cut(self, video_path: Path, cuts: List[Dict]) -> List[Path]:

        output_files = []

        for index, cut in enumerate(cuts, start=1):

            start = float(cut["start"])
            end = float(cut["end"])

            duration = end - start

            # ignora cortes absurdos
            if duration < 25:
                continue

            output_path = self.work_dir / f"cut_{index:02d}.mp4"

            command = [
                "ffmpeg",
                "-y",
                "-ss", str(start),
                "-i", str(video_path),
                "-t", str(duration),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-movflags", "+faststart",
                str(output_path),
            ]

            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            output_files.append(output_path)

        return output_files