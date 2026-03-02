import json
from pathlib import Path

class JobRegistry:

    def __init__(self):
        self.file_path = Path("/tmp/voxmind_job_registry.json")

        if not self.file_path.exists():
            self._write({})

    def register(self, job_id: str, video_url: str):
        data = self._read()
        data[job_id] = video_url
        self._write(data)

    def get_video_url(self, job_id: str):
        data = self._read()
        return data.get(job_id)

    def _read(self):
        with open(self.file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data):
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f)