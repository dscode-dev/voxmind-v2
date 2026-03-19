from importlib import import_module
from pathlib import Path
from typing import Dict, List


class SpeakerDiarizer:

    def __init__(
        self,
        enabled: bool,
        model_name: str,
        hf_token: str | None = None,
    ):
        self.enabled = enabled
        self.model_name = model_name
        self.hf_token = hf_token
        self._pipeline = None
        self._availability_reason = "disabled"

        if not enabled:
            return

        if not hf_token:
            self._availability_reason = "missing_hf_token"
            return

        try:
            pipeline_module = import_module("pyannote.audio")
            pipeline_cls = getattr(pipeline_module, "Pipeline")
            self._pipeline = pipeline_cls.from_pretrained(
                model_name,
                use_auth_token=hf_token,
            )
            self._availability_reason = "available"
        except Exception as exc:  # pragma: no cover
            self._pipeline = None
            self._availability_reason = f"unavailable:{exc.__class__.__name__}"

    @property
    def availability_reason(self) -> str:
        return self._availability_reason

    @property
    def is_available(self) -> bool:
        return self._pipeline is not None

    def diarize(self, audio_path: Path) -> List[Dict]:
        if not self.is_available:
            return []

        diarization = self._pipeline(str(audio_path))
        turns: List[Dict] = []

        for turn, _track, speaker in diarization.itertracks(yield_label=True):
            turns.append(
                {
                    "speaker": str(speaker),
                    "start": float(turn.start),
                    "end": float(turn.end),
                }
            )

        return turns
