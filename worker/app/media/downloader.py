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

    def _find_audio(self) -> Path | None:

        for ext in ["wav", "m4a", "webm", "mp3"]:
            p = self.work_dir / f"audio.{ext}"
            if p.exists():
                return p

        return None

    def download(self, youtube_url: str) -> Path:

        output_tpl = str(self.work_dir / "audio.%(ext)s")

        strategies = [

            # STRATEGY 1 — ANDROID CLIENT (mais estável)
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

            # STRATEGY 2 — IOS CLIENT
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

            # STRATEGY 3 — WEB CLIENT
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

            # STRATEGY 4 — FALLBACK ABSOLUTO
            [
                "yt-dlp",
                "--no-playlist",
                "-f",
                "b",
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

                audio_file = self._find_audio()

                if audio_file:
                    return audio_file

        raise RuntimeError("All yt-dlp download strategies failed")