from pathlib import Path

from app.runtime.subprocess_runner import run_ffmpeg


class AudioExtractor:
    def __init__(self, output_dir: Path, job_id: str | None = None):
        self.output_dir = output_dir
        self.job_id = job_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def extract_wav_16k_mono(self, video_path: Path) -> Path:
        out = self.output_dir / f"{video_path.stem}.wav"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(video_path),
            "-ac", "1",
            "-ar", "16000",
            "-vn",
            str(out),
            "-y",
        ]
        run_ffmpeg(cmd, step="extract_audio", job_id=self.job_id)
        return out
