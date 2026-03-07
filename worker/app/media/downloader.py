import subprocess
from pathlib import Path


class VideoDownloader:

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir

    def _run(self, cmd: list[str]) -> bool:
        try:
            subprocess.run(cmd, check=True)
            return True
        except subprocess.CalledProcessError:
            return False

    def download(self, youtube_url: str) -> Path:

        output_tpl = str(self.work_dir / "audio.%(ext)s")

        strategies = [
            # strategy 1 — android client (mais estável)
            [
                "yt-dlp",
                "--no-playlist",
                "--geo-bypass",
                "--extractor-args",
                "youtube:player_client=android",
                "-f",
                "bestaudio/best",
                "-x",
                "--audio-format",
                "wav",
                "-o",
                output_tpl,
                youtube_url,
            ],
            # strategy 2 — ios client
            [
                "yt-dlp",
                "--no-playlist",
                "--geo-bypass",
                "--extractor-args",
                "youtube:player_client=ios",
                "-f",
                "bestaudio/best",
                "-x",
                "--audio-format",
                "wav",
                "-o",
                output_tpl,
                youtube_url,
            ],
            # strategy 3 — web client
            [
                "yt-dlp",
                "--no-playlist",
                "--geo-bypass",
                "-f",
                "bestaudio/best",
                "-x",
                "--audio-format",
                "wav",
                "-o",
                output_tpl,
                youtube_url,
            ],
            # strategy 4 — fallback absoluto
            [
                "yt-dlp",
                "--no-playlist",
                "-f",
                "best",
                "-x",
                "--audio-format",
                "wav",
                "-o",
                output_tpl,
                youtube_url,
            ],
        ]

        for cmd in strategies:

            if self._run(cmd):

                audio_file = self.work_dir / "audio.wav"

                if audio_file.exists():
                    return audio_file

        raise RuntimeError("All yt-dlp download strategies failed")
