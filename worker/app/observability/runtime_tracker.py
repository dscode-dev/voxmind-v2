import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RuntimeTracker:

    def __init__(self, work_dir: Path, job_id: str):
        self.work_dir = work_dir
        self.job_id = job_id
        self.runtime_path = self.work_dir / "runtime_status.json"

    def mark(
        self,
        pipeline_stage: str,
        step: str,
        status: str,
        **details: Any,
    ) -> None:
        payload = self._load()
        payload.update(
            {
                "job_id": self.job_id,
                "pipeline_stage": pipeline_stage,
                "step": step,
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        if details:
            payload.setdefault("details", {}).update(details)

        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self) -> dict[str, Any]:
        if not self.runtime_path.exists():
            return {}

        try:
            return json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
