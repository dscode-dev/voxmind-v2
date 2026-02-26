from pathlib import Path
import subprocess

class AudioExtractor:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
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
        subprocess.run(cmd, check=True)
        return out
