import subprocess
from pathlib import Path
from typing import List, Dict
import numpy as np


class AudioPeakDetector:
    """
    Extrai áudio do vídeo, calcula energia RMS ao longo do tempo
    e anexa um score simples de pico emocional aos chunks.
    """

    def __init__(self, sample_rate: int = 16000, window_sec: float = 0.5):
        self.sample_rate = sample_rate
        self.window_sec = window_sec

    def _extract_audio(self, video_path: Path) -> np.ndarray:
        cmd = [
            "ffmpeg",
            "-i", str(video_path),
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ac", "1",
            "-ar", str(self.sample_rate),
            "-"
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

        audio = np.frombuffer(process.stdout.read(), np.int16).astype(np.float32)
        audio /= 32768.0

        return audio

    def _compute_energy_curve(self, audio: np.ndarray) -> np.ndarray:
        window = int(self.sample_rate * self.window_sec)
        if window <= 0:
            window = 1

        energies = []

        for i in range(0, len(audio), window):
            segment = audio[i:i + window]
            if len(segment) == 0:
                break
            rms = np.sqrt(np.mean(segment ** 2))
            energies.append(rms)

        return np.array(energies)

    def analyze(self, video_path: Path, chunks: List[Dict]) -> List[Dict]:

        audio = self._extract_audio(video_path)

        energy_curve = self._compute_energy_curve(audio)

        # normalização
        if len(energy_curve) > 0:
            max_val = np.max(energy_curve)
            if max_val > 0:
                energy_curve = energy_curve / max_val

        enriched = []

        for chunk in chunks:

            start = chunk["start"]
            end = chunk["end"]

            start_idx = int(start / self.window_sec)
            end_idx = int(end / self.window_sec)

            segment = energy_curve[start_idx:end_idx]

            if len(segment) == 0:
                peak_score = 0
            else:
                peak_score = float(np.max(segment))

            enriched.append(
                {
                    **chunk,
                    "audio_peak_score": peak_score
                }
            )

        return enriched