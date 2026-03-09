import subprocess
import numpy as np
from faster_whisper import WhisperModel


class Transcriber:

    def __init__(
        self,
        model_size: str,
        compute_type: str,
        language: str,
        beam_size: int,
        vad_filter: bool,
    ):

        self.model = WhisperModel(
            model_size,
            compute_type=compute_type,
            cpu_threads=4,
            num_workers=2,
        )

        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter

    def transcribe(self, video_path):

        cmd = [
            "ffmpeg",
            "-i",
            str(video_path),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-"
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

        audio = np.frombuffer(
            process.stdout.read(),
            np.int16
        ).astype(np.float32) / 32768.0

        segments, info = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
        )

        results = []

        for segment in segments:

            results.append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text.strip(),
                }
            )

        return results