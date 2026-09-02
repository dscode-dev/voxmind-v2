"""Artifact contract reconciliation (PR-RUNTIME-01).

Two mismatches confirmed by the audit:

1. the worker's automatic path writes ``ai_response.json`` while the API only ever probed
   ``ai_output.json``;
2. clip assets were recorded under ``jobs/{id}/cuts/{cut_NN.mp4}``, a prefix the worker
   never uploads — only ``jobs/{id}/final_clips/{final_clip_NN.mp4}`` exists.

Both are fixed while keeping already-stored jobs readable.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest

from app.models.enums import AssetStatus, ClipAssetType
from app.services.job_artifact_sync import JOB_ARTIFACT_FIELDS, JobArtifactSyncService


JOB_ID = uuid.uuid4()


class FakeQuery:
    """Minimal SQLAlchemy query stand-in returning from a fixed row set."""

    def __init__(self, rows):
        self._rows = rows
        self._filters = []

    def filter(self, *criteria):
        self._filters.extend(criteria)
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, existing_assets=None):
        self.added = []
        self.existing_assets = existing_assets or []
        self.lookups = []

    def query(self, model):
        return FakeQuery([])

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def refresh(self, obj):
        pass


@pytest.fixture
def service():
    with mock.patch("app.services.job_artifact_sync.Minio"), mock.patch(
        "app.services.job_artifact_sync.AssetUrlService"
    ) as url_service:
        url_service.return_value.build_signed_url.side_effect = (
            lambda key: f"https://minio.example/{key}" if key else None
        )
        yield JobArtifactSyncService()


def make_job():
    """A ClipJob stand-in carrying every attribute sync_job reads or assigns."""
    fields = {field: None for field in JOB_ARTIFACT_FIELDS}
    return SimpleNamespace(
        id=JOB_ID,
        events=[],
        pipeline_stage="finalize",
        status=None,
        started_at=None,
        finished_at=None,
        error_message=None,
        metadata_json={},
        **fields,
    )


# ==========================================================================
# AI response naming
# ==========================================================================


def test_ai_response_is_the_canonical_key_and_is_probed_first():
    candidates = JOB_ARTIFACT_FIELDS["ai_response_storage_key"]
    assert candidates[0] == "jobs/{job_id}/ai_response.json"
    assert "jobs/{job_id}/ai_output.json" in candidates


def test_new_jobs_resolve_the_canonical_ai_response(service):
    job = make_job()
    present = {f"jobs/{JOB_ID}/ai_response.json"}

    with mock.patch.object(service, "_object_exists", side_effect=lambda k: k in present), \
         mock.patch.object(service, "_load_json", return_value=None):
        result = service.sync_job(db=FakeSession(), job=job)

    assert job.ai_response_storage_key == f"jobs/{JOB_ID}/ai_response.json"
    assert "ai_response_storage_key" in result["synced_artifacts"]


def test_legacy_jobs_still_resolve_ai_output(service):
    """A job stored before this PR only ever had ai_output.json."""
    job = make_job()
    present = {f"jobs/{JOB_ID}/ai_output.json"}

    with mock.patch.object(service, "_object_exists", side_effect=lambda k: k in present), \
         mock.patch.object(service, "_load_json", return_value=None):
        service.sync_job(db=FakeSession(), job=job)

    assert job.ai_response_storage_key == f"jobs/{JOB_ID}/ai_output.json"


def test_canonical_wins_when_both_exist(service):
    job = make_job()
    present = {
        f"jobs/{JOB_ID}/ai_response.json",
        f"jobs/{JOB_ID}/ai_output.json",
    }

    with mock.patch.object(service, "_object_exists", side_effect=lambda k: k in present), \
         mock.patch.object(service, "_load_json", return_value=None):
        service.sync_job(db=FakeSession(), job=job)

    assert job.ai_response_storage_key == f"jobs/{JOB_ID}/ai_response.json"


def test_missing_ai_response_leaves_the_field_unset(service):
    job = make_job()

    with mock.patch.object(service, "_object_exists", return_value=False), \
         mock.patch.object(service, "_load_json", return_value=None):
        result = service.sync_job(db=FakeSession(), job=job)

    assert "ai_response_storage_key" not in result["synced_artifacts"]


# ==========================================================================
# Clip storage keys
# ==========================================================================


DELIVERY_PACKAGE = {
    "clips": [
        {
            "clip_index": 1,
            "file_name": "cut_01.mp4",
            "final_file_name": "final_clip_01.mp4",
            "start": 10.0,
            "end": 40.0,
            "duration": 30.0,
            "hashtags": ["#a"],
        },
        {
            "clip_index": 2,
            "file_name": "cut_02.mp4",
            "final_file_name": "final_clip_02.mp4",
            "start": 50.0,
            "end": 80.0,
            "duration": 30.0,
        },
    ]
}

QA_REPORT = {
    "clips": [
        {"clip_index": 1, "file_name": "cut_01.mp4", "score": 88, "decision": "approved"},
        {"clip_index": 2, "file_name": "cut_02.mp4", "score": 71, "decision": "needs_review"},
    ]
}


def test_clip_assets_point_at_the_uploaded_final_clips(service):
    db = FakeSession()
    service._sync_clip_assets(db, make_job(), DELIVERY_PACKAGE, QA_REPORT)

    keys = [asset.storage_key for asset in db.added]
    assert keys == [
        f"jobs/{JOB_ID}/final_clips/final_clip_01.mp4",
        f"jobs/{JOB_ID}/final_clips/final_clip_02.mp4",
    ]
    # The prefix that was never uploaded must not appear anywhere.
    assert not any("/cuts/" in key for key in keys)


def test_signed_urls_are_built_from_the_corrected_key(service):
    db = FakeSession()
    service._sync_clip_assets(db, make_job(), DELIVERY_PACKAGE, QA_REPORT)

    assert db.added[0].public_url.endswith("final_clips/final_clip_01.mp4")


def test_qa_report_is_still_matched_by_the_raw_cut_name(service):
    """QA is keyed by the intermediate cut name; the storage key is not."""
    db = FakeSession()
    service._sync_clip_assets(db, make_job(), DELIVERY_PACKAGE, QA_REPORT)

    assert db.added[0].extra_json["qa"]["score"] == 88
    assert db.added[1].extra_json["qa"]["score"] == 71


def test_clip_metadata_is_preserved(service):
    db = FakeSession()
    service._sync_clip_assets(db, make_job(), DELIVERY_PACKAGE, QA_REPORT)

    first = db.added[0]
    assert first.asset_type == ClipAssetType.SHORT_CLIP
    assert first.status == AssetStatus.READY
    assert first.order_index == 1
    assert first.start_sec == Decimal("10.0")
    assert first.duration_sec == Decimal("30.0")
    assert first.hashtags_json == ["#a"]


def test_package_without_final_file_name_falls_back_to_the_cut_name(service):
    """Delivery packages written before final_file_name existed still produce a key."""
    db = FakeSession()
    legacy_package = {"clips": [{"clip_index": 1, "file_name": "cut_01.mp4"}]}

    service._sync_clip_assets(db, make_job(), legacy_package, None)

    assert db.added[0].storage_key == f"jobs/{JOB_ID}/final_clips/cut_01.mp4"


def test_clip_without_any_name_is_skipped(service):
    db = FakeSession()
    service._sync_clip_assets(db, make_job(), {"clips": [{"clip_index": 1}]}, None)

    assert db.added == []
