from importlib import import_module
from pathlib import Path
from typing import Dict, List
import gc


class SpeakerDiarizer:

    def __init__(
        self,
        enabled: bool,
        model_name: str,
        device: str = "cpu",
        hf_token: str | None = None,
        fallback_to_cpu_on_oom: bool = True,
    ):
        self.enabled = enabled
        self.model_name = model_name
        self.device = device
        self.hf_token = hf_token
        self.fallback_to_cpu_on_oom = fallback_to_cpu_on_oom
        self._pipeline = None
        self._availability_reason = "disabled"
        self._last_run_reason = "not_started"
        self._resolved_device = "cpu"
        self._load_attempted = False

        if not enabled:
            self._last_run_reason = "disabled"
            return

        if not hf_token:
            self._availability_reason = "missing_hf_token"
            self._last_run_reason = "missing_hf_token"
            return

    @property
    def availability_reason(self) -> str:
        self._ensure_pipeline_loaded()
        return self._availability_reason

    @property
    def is_available(self) -> bool:
        self._ensure_pipeline_loaded()
        return self._pipeline is not None

    def diagnostics(self) -> Dict:
        self._ensure_pipeline_loaded()
        return {
            "enabled": self.enabled,
            "model_name": self.model_name,
            "requested_device": self.device,
            "resolved_device": self._resolved_device,
            "token_present": bool(self.hf_token),
            "available": self.is_available,
            "availability_reason": self.availability_reason,
            "last_run_reason": self._last_run_reason,
        }

    def diarize(self, audio_path: Path) -> List[Dict]:
        self._ensure_pipeline_loaded()
        if not self.is_available:
            self._last_run_reason = self._availability_reason
            return []

        try:
            diarization = self._pipeline(str(audio_path))
        except Exception as exc:  # pragma: no cover
            if self._should_fallback_to_cpu(exc):
                self._fallback_to_cpu()
                return self.diarize(audio_path)
            self._last_run_reason = self._format_exception_reason("runtime_error", exc)
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

    def _ensure_pipeline_loaded(self) -> None:
        if self._load_attempted or not self.enabled or not self.hf_token:
            return

        self._load_attempted = True
        try:
            pipeline_module = import_module("pyannote.audio")
            pipeline_cls = getattr(pipeline_module, "Pipeline")
            self._pipeline = self._load_pipeline(pipeline_cls, self.model_name, self.hf_token)
            if self._pipeline is None:
                raise RuntimeError(
                    "pipeline_not_loaded; verify Hugging Face token and accept gated model terms"
                )
            self._set_device()
            self._availability_reason = "available"
            self._last_run_reason = "ready"
        except Exception as exc:  # pragma: no cover
            if self._should_fallback_to_cpu(exc):
                self._fallback_to_cpu()
                self._ensure_pipeline_loaded()
                return
            self._pipeline = None
            self._availability_reason = self._format_exception_reason("unavailable", exc)
            self._last_run_reason = self._availability_reason

    def _load_pipeline(self, pipeline_cls, model_name: str, hf_token: str):
        try:
            return pipeline_cls.from_pretrained(
                model_name,
                token=hf_token,
            )
        except TypeError as exc:
            message = str(exc)
            if "unexpected keyword argument 'token'" not in message:
                raise

            return pipeline_cls.from_pretrained(
                model_name,
                use_auth_token=hf_token,
            )

    def _set_device(self) -> None:
        if self._pipeline is None:
            return

        torch_module = import_module("torch")
        requested = str(self.device or "cpu").strip().lower()
        resolved = "cpu"
        if requested == "cuda" and bool(getattr(torch_module.cuda, "is_available", lambda: False)()):
            resolved = "cuda"

        target_device = torch_module.device(resolved)
        if hasattr(self._pipeline, "to"):
            self._pipeline.to(target_device)
        self._resolved_device = resolved

    def _should_fallback_to_cpu(self, exc: Exception) -> bool:
        if not self.fallback_to_cpu_on_oom:
            return False
        if str(self.device).strip().lower() != "cuda":
            return False
        message = str(exc).lower()
        return "cuda" in message and "out of memory" in message

    def _fallback_to_cpu(self) -> None:
        self._pipeline = None
        self._load_attempted = False
        self.device = "cpu"
        self._resolved_device = "cpu"
        self._clear_cuda_memory()

    def _clear_cuda_memory(self) -> None:
        gc.collect()
        try:
            torch_module = import_module("torch")
            if bool(getattr(torch_module.cuda, "is_available", lambda: False)()):
                torch_module.cuda.empty_cache()
        except Exception:
            pass

    def release_resources(self) -> None:
        self._pipeline = None
        self._load_attempted = False
        self._clear_cuda_memory()

    def _format_exception_reason(self, prefix: str, exc: Exception) -> str:
        message = str(exc).strip().replace("\n", " ")
        details: List[str] = []

        if message:
            details.append(message)

        if not details:
            details.append(repr(exc).replace("\n", " "))

        if exc.__cause__ is not None:
            cause_message = str(exc.__cause__).strip().replace("\n", " ")
            if cause_message:
                details.append(f"cause={exc.__cause__.__class__.__name__}:{cause_message}")
            else:
                details.append(f"cause={repr(exc.__cause__).replace(chr(10), ' ')}")

        if exc.__context__ is not None and exc.__context__ is not exc.__cause__:
            context_message = str(exc.__context__).strip().replace("\n", " ")
            if context_message:
                details.append(f"context={exc.__context__.__class__.__name__}:{context_message}")
            else:
                details.append(f"context={repr(exc.__context__).replace(chr(10), ' ')}")

        if details:
            return f"{prefix}:{exc.__class__.__name__}:{' | '.join(details)}"
        return f"{prefix}:{exc.__class__.__name__}"
