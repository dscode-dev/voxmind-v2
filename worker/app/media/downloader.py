from pathlib import Path

from app.runtime.subprocess_runner import SubprocessError, run_download


class VideoDownloader:

    def __init__(self, work_dir: Path, job_id: str | None = None):
        self.work_dir = work_dir
        self.job_id = job_id
        self.last_error: SubprocessError | None = None

    def _run(self, cmd: list[str]) -> bool:
        try:
            run_download(cmd, step="download_video", job_id=self.job_id)
            return True
        except SubprocessError as exc:
            # Each strategy is allowed to fail; the last failure is reported if all do.
            self.last_error = exc
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

        detail = ""
        if self.last_error is not None:
            detail = f" Last error: {self.last_error}"
            if self.last_error.stderr:
                detail += f" | stderr: {self.last_error.stderr}"

        raise RuntimeError(f"All yt-dlp download strategies failed.{detail}")