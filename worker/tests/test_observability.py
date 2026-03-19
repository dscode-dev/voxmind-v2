import json

from app.observability import ArtifactTracker, RuntimeTracker


def test_runtime_tracker_persists_latest_stage_state(tmp_path):
    tracker = RuntimeTracker(tmp_path, "job-123")

    tracker.mark("prepare", "transcribe", "completed", segment_count=42)

    payload = json.loads((tmp_path / "runtime_status.json").read_text(encoding="utf-8"))

    assert payload["job_id"] == "job-123"
    assert payload["pipeline_stage"] == "prepare"
    assert payload["step"] == "transcribe"
    assert payload["status"] == "completed"
    assert payload["details"]["segment_count"] == 42


def test_artifact_tracker_persists_local_and_remote_artifacts(tmp_path):
    local_file = tmp_path / "prompt.txt"
    local_file.write_text("prompt", encoding="utf-8")

    tracker = ArtifactTracker(tmp_path, "job-456")
    tracker.mark_local("prompt", "prepare", local_file, artifact_type="text")
    tracker.mark_remote("prompt", "prepare", "jobs/job-456/prompt.txt", local_file)

    payload = json.loads((tmp_path / "artifacts_manifest.json").read_text(encoding="utf-8"))
    artifact = payload["artifacts"]["prompt"]

    assert payload["job_id"] == "job-456"
    assert artifact["artifact_name"] == "prompt"
    assert artifact["storage_object"] == "jobs/job-456/prompt.txt"
    assert artifact["local_path"] == str(local_file)
