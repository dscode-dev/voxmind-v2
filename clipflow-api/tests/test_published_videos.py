"""The list the performance screen is built on.

It replaced the evaluation dataset, which resolved five canonical windows per video to answer
a question nobody was asking yet. What is asserted here is the difference that matters to a
person reading the screen: only cuts that actually reached the channel appear, and a video
nobody has measured says so instead of claiming zero views.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import api_router
from app.db.session import get_db
from app.models.enums import (
    PipelineState,
    PublishAttemptStatus,
    PublishPlatform,
    PublishTargetConnectionStatus,
    UserRole,
    UserStatus,
)
from app.models.publish_attempt import PublishAttempt
from app.models.publish_target import PublishTarget
from app.models.user import User
from app.models.video_performance_snapshot import VideoPerformanceSnapshot
from app.security.auth_middleware import get_current_admin
from tests.conftest import make_run

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def admin_user(db):
    user = User(
        phone_number="+5511999999999",
        full_name="Admin",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture()
def client(db, admin_user, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def target(db):
    row = PublishTarget(
        platform=PublishPlatform.YOUTUBE,
        name="Voxmind FC",
        is_active=True,
        connection_status=PublishTargetConnectionStatus.CONNECTED,
        config_json={},
    )
    db.add(row)
    db.flush()
    return row


def attempt(db, target, *, index=1, status=PublishAttemptStatus.SUCCEEDED,
            external_id="vid_1", finished_at=NOW):
    job = make_run(db, state=PipelineState.PUBLISHED)
    row = PublishAttempt(
        pipeline_job_id=job.id,
        target_id=target.id,
        media_identity=f"final_clips/final_clip_{index:02d}.mp4",
        media_storage_key=f"jobs/x/final_clips/final_clip_{index:02d}.mp4",
        status=status,
        attempt_no=1,
        max_attempts=3,
        initiator="automatic",
        external_id=external_id,
        finished_at=finished_at,
    )
    db.add(row)
    db.flush()
    return row


def snapshot(db, row, *, views=None, likes=None, captured_at=NOW, **kw):
    snap = VideoPerformanceSnapshot(
        publish_attempt_id=row.id,
        publish_target_id=row.target_id,
        external_video_id=row.external_id,
        provider="youtube",
        captured_at=captured_at,
        capture_slot=kw.pop("capture_slot", "h24"),
        view_count=views,
        like_count=likes,
        comment_count=kw.pop("comments", None),
        availability=kw.pop("availability", "ok"),
        privacy_status=kw.pop("privacy_status", "private"),
    )
    db.add(snap)
    db.flush()
    return snap


def test_only_cuts_that_reached_the_channel_are_listed(client, db, target):
    """A failed upload is not a published video, however much work went into it."""
    attempt(db, target, index=1, external_id="vid_ok")
    attempt(db, target, index=2, status=PublishAttemptStatus.FAILED_FINAL, external_id=None)
    attempt(db, target, index=3, status=PublishAttemptStatus.UNKNOWN, external_id=None)
    db.commit()

    body = client.get("/admin/published-videos").json()

    assert body["total"] == 1
    assert body["items"][0]["external_id"] == "vid_ok"


def test_a_video_nobody_measured_reports_nothing_rather_than_zero(client, db, target):
    """Never measured and measured at zero are different facts.

    Only one of them is about the video; the other is about the collection not having run.
    A zero on the screen would say the cut flopped when nothing had looked at it yet.
    """
    attempt(db, target, external_id="vid_unmeasured")
    db.commit()

    item = client.get("/admin/published-videos").json()["items"][0]

    assert item["views"] is None
    assert item["likes"] is None
    assert item["measured_at"] is None


def test_the_counters_are_the_latest_measurement(client, db, target):
    """Two measurements of one video, and the screen shows the newer one.

    Different capture slots because the snapshots are immutable and unique per slot — a video
    is measured once at each window, never overwritten.
    """
    row = attempt(db, target, external_id="vid_1")
    snapshot(db, row, views=100, likes=5, capture_slot="h1",
             captured_at=NOW - timedelta(days=2))
    snapshot(db, row, views=940, likes=61, capture_slot="h24", captured_at=NOW)
    db.commit()

    item = client.get("/admin/published-videos").json()["items"][0]

    assert item["views"] == 940
    assert item["likes"] == 61
    assert item["measured_at"].startswith("2026-09-10")


def test_the_watch_url_is_built_so_the_screen_can_link_out(client, db, target):
    attempt(db, target, external_id="eo0AiGWxKzw")
    db.commit()

    item = client.get("/admin/published-videos").json()["items"][0]

    assert item["external_url"] == "https://www.youtube.com/watch?v=eo0AiGWxKzw"


def test_the_page_is_bounded(client, db, target):
    for index in range(4):
        attempt(db, target, index=index + 1, external_id=f"vid_{index}")
    db.commit()

    body = client.get("/admin/published-videos", params={"limit": 2}).json()

    assert len(body["items"]) == 2
    assert body["total"] == 4
    assert client.get("/admin/published-videos", params={"limit": 500}).status_code == 422


def test_no_credential_reaches_the_response(client, db, target):
    attempt(db, target, external_id="vid_1")
    db.commit()

    raw = client.get("/admin/published-videos").text

    for forbidden in ("refresh_token", "upload_session", "access_token", "encrypted"):
        assert forbidden not in raw


def test_published_videos_is_admin_only(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        assert anonymous.get("/admin/published-videos").status_code in (401, 403)
