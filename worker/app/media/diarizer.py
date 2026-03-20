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
        self._last_run_reason = "not_started"

        if not enabled:
            self._last_run_reason = "disabled"
            return

        if not hf_token:
            self._availability_reason = "missing_hf_token"
            self._last_run_reason = "missing_hf_token"
            return

        try:
            pipeline_module = import_module("pyannote.audio")
            pipeline_cls = getattr(pipeline_module, "Pipeline")
            self._pipeline = pipeline_cls.from_pretrained(
                model_name,
                use_auth_token=hf_token,
            )
            self._availability_reason = "available"
            self._last_run_reason = "ready"
        except Exception as exc:  # pragma: no cover
            self._pipeline = None
            self._availability_reason = f"unavailable:{exc.__class__.__name__}"
            self._last_run_reason = self._availability_reason

    @property
    def availability_reason(self) -> str:
        return self._availability_reason

    @property
    def is_available(self) -> bool:
        return self._pipeline is not None

    def diagnostics(self) -> Dict:
        return {
            "enabled": self.enabled,
            "model_name": self.model_name,
            "token_present": bool(self.hf_token),
            "available": self.is_available,
            "availability_reason": self.availability_reason,
            "last_run_reason": self._last_run_reason,
        }

    def diarize(self, audio_path: Path) -> List[Dict]:
        if not self.is_available:
            self._last_run_reason = self._availability_reason
            return []

        try:
            diarization = self._pipeline(str(audio_path))
        except Exception as exc:  # pragma: no cover
            self._last_run_reason = f"runtime_error:{exc.__class__.__name__}"
            return []

        turns: List[Dict] = []

        for turn, _track, speaker in diarization.itertracks(yield_label=True):
            turns.append(
                {
                    "speaker": str(speaker),
                    "start": float(turn.start),
                    "end": float(turn.end),
                }
            )

        self._last_run_reason = "completed"
        return turns
