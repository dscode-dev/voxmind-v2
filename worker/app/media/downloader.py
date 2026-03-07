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

    def _find_video(self) -> Path | None:

        for ext in ["mp4", "mkv", "webm", "mov"]:
            p = self.work_dir / f"video.{ext}"
            if p.exists():
                return p

        return None

    def download(self, youtube_url: str) -> Path:

        output_tpl = str(self.work_dir / "video.%(ext)s")

        strategies = [

            # STRATEGY 1 — ANDROID CLIENT (mais estável)
            [
                "yt-dlp",
                "--no-playlist",
                "--geo-bypass",
                "--extractor-args",
                "youtube:player_client=android",
                "-f",
                "bestvideo+bestaudio/best",
                "--merge-output-format",
                "mp4",
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
                "bestvideo+bestaudio/best",
                "--merge-output-format",
                "mp4",
                "-o",
                output_tpl,
                youtube_url,
            ],

            # STRATEGY 3 — FALLBACK
            [
                "yt-dlp",
                "--no-playlist",
                "-f",
                "best",
                "-o",
                output_tpl,
                youtube_url,
            ],
        ]

        for cmd in strategies:

            if self._run(cmd):

                video = self._find_video()

                if video:
                    return video

        raise RuntimeError("All yt-dlp download strategies failed")