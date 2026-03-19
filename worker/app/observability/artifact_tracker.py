import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ArtifactTracker:

    def __init__(self, work_dir: Path, job_id: str):
        self.work_dir = work_dir
        self.job_id = job_id
        self.manifest_path = self.work_dir / "artifacts_manifest.json"

    def mark_local(
        self,
        artifact_name: str,
        pipeline_stage: str,
        local_path: str | Path,
        **metadata: Any,
    ) -> None:
        path = Path(local_path)
        self._update_artifact(
            artifact_name,
            pipeline_stage,
            local_path=str(path),
            exists=path.exists(),
            size_bytes=path.stat().st_size if path.exists() else None,
            **metadata,
        )

    def mark_remote(
        self,
        artifact_name: str,
        pipeline_stage: str,
        storage_object: str,
        local_path: str | Path | None = None,
        **metadata: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "storage_object": storage_object,
            **metadata,
        }

        if local_path is not None:
            path = Path(local_path)
            payload.update(
                {
                    "local_path": str(path),
                    "exists": path.exists(),
                    "size_bytes": path.stat().st_size if path.exists() else None,
                }
            )

        self._update_artifact(artifact_name, pipeline_stage, **payload)

    def _update_artifact(
        self,
        artifact_name: str,
        pipeline_stage: str,
        **fields: Any,
    ) -> None:
        payload = self._load()
        artifacts = payload.setdefault("artifacts", {})

        artifacts[artifact_name] = {
            "artifact_name": artifact_name,
            "pipeline_stage": pipeline_stage,
            **artifacts.get(artifact_name, {}),
            **fields,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        payload["job_id"] = self.job_id
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()

        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {}

        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def read(self) -> dict[str, Any]:
        return self._load()
