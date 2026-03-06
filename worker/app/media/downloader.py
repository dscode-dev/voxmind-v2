import subprocess
from pathlib import Path


class VideoDownloader:

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir

    def download(self, youtube_url: str) -> Path:

        output_tpl = str(self.work_dir / "audio.%(ext)s")

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--geo-bypass",
            "--no-check-certificate",
            "-f", "bestaudio",
            "-x",
            "--audio-format", "wav",
            "-o", output_tpl,
            youtube_url,
        ]

        subprocess.run(cmd, check=True)

        # yt-dlp vai gerar audio.wav
        audio_file = self.work_dir / "audio.wav"

        if not audio_file.exists():
            raise RuntimeError("Audio extraction failed")

        return audio_file