from pathlib import Path
import yt_dlp

class VideoDownloader:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def download(self, url: str) -> Path:
        ydl_opts = {
            "outtmpl": str(self.output_dir / "%(id)s.%(ext)s"),
            "format": "mp4/best",
            "quiet": True,
            "noplaylist": True,
            "retries": 3,
            "fragment_retries": 3,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        return Path(filename)
