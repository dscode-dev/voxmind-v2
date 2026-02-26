from pathlib import Path
from typing import Any
from faster_whisper import WhisperModel

class Transcriber:
    def __init__(self, *, model_size: str, compute_type: str, language: str, beam_size: int, vad_filter: bool):
        self.model = WhisperModel(model_size, device="cpu", compute_type=compute_type)
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter

    def transcribe(self, audio_path: Path) -> list[dict[str, Any]]:
        segments, _info = self.model.transcribe(
            str(audio_path),
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
        )
        out: list[dict[str, Any]] = []
        for s in segments:
            out.append({"start": float(s.start), "end": float(s.end), "text": (s.text or "").strip()})
        return out
