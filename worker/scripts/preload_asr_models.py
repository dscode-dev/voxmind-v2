import os
from pathlib import Path

from huggingface_hub import snapshot_download


def main() -> int:
    models = [item.strip() for item in os.environ.get("PRELOAD_ASR_MODELS", "").split(",") if item.strip()]
    required = os.environ.get("PRELOAD_ASR_MODELS_REQUIRED", "false").strip().lower() == "true"
    base_dir = Path(os.environ.get("ASR_PRELOADED_MODELS_DIR", "/opt/voxmind-models/asr"))
    base_dir.mkdir(parents=True, exist_ok=True)

    failures: list[tuple[str, str]] = []

    for model in models:
        repo_id = f"Systran/faster-whisper-{model}"
        target_dir = base_dir / model
        if target_dir.exists():
            print(f"preloaded_asr_model_exists={model}")
            continue

        try:
            snapshot_download(
                repo_id=repo_id,
                local_dir=str(target_dir),
                local_dir_use_symlinks=False,
            )
            print(f"preloaded_asr_model={model}")
        except Exception as exc:  # pragma: no cover
            failures.append((model, str(exc)))

    if failures:
        for model, error in failures:
            print(f"failed_preload_asr_model={model}: {error}")
        if required:
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
