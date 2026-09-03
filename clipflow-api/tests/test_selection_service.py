"""Selection against persisted candidates: status transitions, persistence, API, concurrency."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import discovery as discovery_api
from app.api.router import api_router
from app.db.session import get_db
from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.enums import (
    DiscoverySourceKind,
    UserRole,
    UserStatus,
    VideoCandidateStatus,
)
from app.models.pipeline_job import PipelineJob
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin
from app.selection.engine import SelectionEngine
from app.selection.policy import SCORE_VERSION
from app.services.selection_service import (
    HARD_MAX_SELECTED_PER_RUN,
    METHOD_MANUAL,
    METHOD_POLICY,
    SelectionService,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def ago(**kwargs) -> datetime:
    return NOW - timedelta(**kwargs)


@pytest.fixture()
def topic(db):
    topic = ContentTopic(
        name="Futebol brasileiro",
        description="Noticias e polemicas",
        keywords_json=["futebol", "entrevista", "polemica", "arbitragem"],
        metadata_json={},
    )
    db.add(topic)
    db.flush()
    return topic


@pytest.fixture()
def source(db, topic):
    source = DiscoverySource(
        topic_id=topic.id,
        kind=DiscoverySourceKind.YOUTUBE_SEARCH,
        is_active=True,
        config_json={"queries": ["futebol entrevista"]},
    )
    db.add(source)
    db.flush()
    return source


def make_candidate(db, topic, source, candidate_id: str, **overrides):
    normalized = {
        "description": "Coletiva de futebol",
        "channel_id": overrides.pop("channel_id", f"UC_{candidate_id}"),
        "view_count": overrides.pop("view_count", 40_000),
        "like_count": overrides.pop("like_count", 3_000),
        "comment_count": overrides.pop("comment_count", 500),
        "live_status": overrides.pop("live_status", "none"),
        "available": overrides.pop("available", True),
    }
    row = VideoCandidate(
        topic_id=topic.id,
        source_id=source.id,
        external_id=candidate_id,
        url=f"https://www.youtube.com/watch?v={candidate_id}",
        title=overrides.pop("title", "Entrevista polemica sobre futebol e arbitragem"),
        channel=overrides.pop("channel", "Canal Esportivo"),
        duration_sec=overrides.pop("duration_sec", 900),
        published_at=overrides.pop("published_at", ago(hours=4)),
        dedup_hash=f"hash-{candidate_id}",
        status=overrides.pop("status", VideoCandidateStatus.DISCOVERED),
        metadata_json={"provider": "youtube", "normalized": normalized},
        **overrides,
    )
    db.add(row)
    db.flush()
    return row


def service() -> SelectionService:
    return SelectionService(engine=SelectionEngine())


def rows(db):
    return db.query(VideoCandidate).order_by(VideoCandidate.external_id).all()


# ==========================================================================
# Dry run
# ==========================================================================


def test_a_dry_run_changes_nothing(db, topic, source, no_event_fanout):
    for index in range(3):
        make_candidate(db, topic, source, f"c{index}")

    report = service().run(db, topic=topic, dry_run=True, now=NOW)

    assert len(report.outcome.selected) > 0, "it still ranks"
    assert report.committed == 0
    assert all(row.status == VideoCandidateStatus.DISCOVERED for row in rows(db))
    assert all(row.scores_json is None for row in rows(db))
    assert all(row.selected_at is None for row in rows(db))


def test_a_dry_run_still_explains_itself(db, topic, source, no_event_fanout):
    make_candidate(db, topic, source, "c0")
    report = service().run(db, topic=topic, dry_run=True, now=NOW)

    payload = report.as_dict(verbose=True)
    assert payload["dry_run"] is True
    assert payload["selected"][0]["reasons"]
    assert payload["score_version"] == SCORE_VERSION


# ==========================================================================
# Committed run
# ==========================================================================


def test_a_committed_run_marks_candidates_selected(db, topic, source, no_event_fanout):
    for index in range(3):
        make_candidate(db, topic, source, f"c{index}", channel_id=f"UC_{index}")

    report = service().run(db, topic=topic, dry_run=False, now=NOW)

    selected = [row for row in rows(db) if row.status == VideoCandidateStatus.SELECTED]
    assert len(selected) == report.committed > 0
    assert all(row.selected_at is not None for row in selected)


def test_selection_creates_no_pipeline_job(db, topic, source, no_event_fanout):
    """The boundary this PR holds: an editorial decision, not an admission to production."""
    for index in range(3):
        make_candidate(db, topic, source, f"c{index}", channel_id=f"UC_{index}")

    service().run(db, topic=topic, dry_run=False, now=NOW)

    assert db.query(PipelineJob).count() == 0


def test_selection_never_marks_a_candidate_consumed(db, topic, source, no_event_fanout):
    """CONSUMED means 'already produced', which is a fact about production."""
    make_candidate(db, topic, source, "c0")
    service().run(db, topic=topic, dry_run=False, now=NOW)

    assert not any(row.status == VideoCandidateStatus.CONSUMED for row in rows(db))


def test_the_selection_method_is_recorded(db, topic, source, no_event_fanout):
    make_candidate(db, topic, source, "c0")
    service().run(db, topic=topic, dry_run=False, now=NOW)

    selection = rows(db)[0].metadata_json["selection"]
    assert selection["method"] == METHOD_POLICY
    assert selection["score_version"] == SCORE_VERSION
    assert selection["selection_run_id"]


def test_ranked_candidates_keep_their_breakdown(db, topic, source, no_event_fanout):
    """Answering 'why was this NOT chosen?' after the run requires writing losers' scores."""
    for index in range(5):
        make_candidate(db, topic, source, f"c{index}", channel_id=f"UC_{index}")

    service().run(db, topic=topic, dry_run=False, now=NOW)

    scored = [row for row in rows(db) if row.scores_json]
    assert len(scored) == 5, "every ranked candidate, not just the winners"
    assert all(row.scores_json["version"] == SCORE_VERSION for row in scored)


def test_unselected_candidates_become_ranked_not_rejected(db, topic, source, no_event_fanout):
    for index in range(5):
        make_candidate(db, topic, source, f"c{index}", channel_id=f"UC_{index}")

    service().run(db, topic=topic, dry_run=False, now=NOW)

    statuses = {row.status for row in rows(db)}
    assert VideoCandidateStatus.RANKED in statuses
    assert VideoCandidateStatus.REJECTED not in statuses


def test_only_permanently_unusable_candidates_are_rejected(db, topic, source, no_event_fanout):
    make_candidate(db, topic, source, "gone", available=False)
    make_candidate(db, topic, source, "old", published_at=ago(days=40), channel_id="UC_old")
    make_candidate(db, topic, source, "short", duration_sec=20, channel_id="UC_short")

    service().run(db, topic=topic, dry_run=False, now=NOW)

    by_id = {row.external_id: row for row in rows(db)}
    assert by_id["gone"].status == VideoCandidateStatus.REJECTED
    assert by_id["short"].status == VideoCandidateStatus.REJECTED
    # Freshness is temporary: rejecting on it would burn a candidate a later run may want.
    assert by_id["old"].status == VideoCandidateStatus.DISCOVERED


def test_score_columns_carry_only_what_this_pr_defines(db, topic, source, no_event_fanout):
    """A column existing is not a reason to put a number in it."""
    make_candidate(db, topic, source, "c0")
    service().run(db, topic=topic, dry_run=False, now=NOW)

    row = rows(db)[0]
    assert row.relevance_score is not None
    assert row.trend_score is not None
    # Nothing here has seen the video, and exact dedup already lives in dedup_hash.
    assert row.quality_score is None
    assert row.duplicate_score is None


# ==========================================================================
# Repeat runs
# ==========================================================================


def test_a_second_run_does_not_re_select(db, topic, source, no_event_fanout):
    for index in range(2):
        make_candidate(db, topic, source, f"c{index}", channel_id=f"UC_{index}")

    first = service().run(db, topic=topic, dry_run=False, now=NOW)
    selected_ids = {
        row.id for row in rows(db) if row.status == VideoCandidateStatus.SELECTED
    }

    second = service().run(db, topic=topic, dry_run=False, now=NOW)

    still_selected = {
        row.id for row in rows(db) if row.status == VideoCandidateStatus.SELECTED
    }
    assert still_selected == selected_ids
    assert second.committed == 0 or not (
        {a.candidate.candidate_id for a in second.outcome.selected}
        & {str(i) for i in selected_ids}
    )


def test_selected_candidates_are_not_reloaded(db, topic, source, no_event_fanout):
    make_candidate(db, topic, source, "done", status=VideoCandidateStatus.SELECTED)
    make_candidate(db, topic, source, "used", status=VideoCandidateStatus.CONSUMED,
                   channel_id="UC_2")
    make_candidate(db, topic, source, "no", status=VideoCandidateStatus.REJECTED,
                   channel_id="UC_3")

    report = service().run(db, topic=topic, dry_run=True, now=NOW)

    assert report.outcome.considered == 0


def test_the_daily_cap_counts_earlier_selections(db, topic, source, no_event_fanout):
    topic.metadata_json = {"selection": {"max_selections_per_day": 1}}
    make_candidate(db, topic, source, "earlier", status=VideoCandidateStatus.SELECTED,
                   selected_at=ago(hours=2))
    make_candidate(db, topic, source, "now", channel_id="UC_new")
    db.flush()

    report = service().run(db, topic=topic, dry_run=True, now=NOW)

    assert report.outcome.selected == []
    assert "daily_cap_reached" in report.outcome.blocked[0].blocked_by


def test_the_channel_cooldown_reads_recent_history(db, topic, source, no_event_fanout):
    make_candidate(db, topic, source, "earlier", status=VideoCandidateStatus.SELECTED,
                   selected_at=ago(hours=1), channel_id="UC_hot")
    make_candidate(db, topic, source, "now", channel_id="UC_hot")
    db.flush()

    report = service().run(db, topic=topic, dry_run=True, now=NOW)

    assert report.outcome.selected == []
    assert "channel_cooldown" in report.outcome.blocked[0].blocked_by


# ==========================================================================
# Caps
# ==========================================================================


def test_a_topic_can_configure_its_own_policy(db, topic, source, no_event_fanout):
    topic.metadata_json = {"selection": {"max_selected_per_run": 2, "max_per_channel": 2}}
    for index in range(4):
        make_candidate(db, topic, source, f"c{index}", channel_id=f"UC_{index}")
    db.flush()

    report = service().run(db, topic=topic, dry_run=True, now=NOW)

    assert len(report.outcome.selected) == 2


def test_a_caller_cannot_ask_for_an_unbounded_run(db, topic, source, no_event_fanout):
    """§55: the last line before automation can act at scale."""
    for index in range(3):
        make_candidate(db, topic, source, f"c{index}", channel_id=f"UC_{index}")

    report = service().run(db, topic=topic, limit=100_000, dry_run=True, now=NOW)

    assert len(report.outcome.selected) <= HARD_MAX_SELECTED_PER_RUN


def test_a_topic_cannot_configure_past_the_server_ceiling(db, topic, source, no_event_fanout):
    topic.metadata_json = {"selection": {"max_selected_per_run": 5_000}}
    db.flush()

    config = service()._config_for(topic, None)

    assert config.max_selected_per_run == HARD_MAX_SELECTED_PER_RUN


# ==========================================================================
# Concurrency
# ==========================================================================


def test_two_runs_do_not_select_the_same_candidate_twice(db, topic, source, no_event_fanout):
    """Sequential proxy for the race the advisory lock serialises.

    Two runs against one topic must not both count a candidate as their own. The lock makes
    the second run observe what the first committed; here the second run genuinely re-reads
    the status the first one wrote.
    """
    for index in range(4):
        make_candidate(db, topic, source, f"c{index}", channel_id=f"UC_{index}")

    first = service().run(db, topic=topic, limit=2, dry_run=False, now=NOW)
    second = service().run(db, topic=topic, limit=2, dry_run=False, now=NOW)

    first_ids = {a.candidate.candidate_id for a in first.outcome.selected}
    second_ids = {a.candidate.candidate_id for a in second.outcome.selected}

    assert first_ids and not (first_ids & second_ids), "no candidate selected by both runs"
    total = len([r for r in rows(db) if r.status == VideoCandidateStatus.SELECTED])
    assert total == len(first_ids) + len(second_ids)


def test_the_daily_cap_holds_across_consecutive_runs(db, topic, source, no_event_fanout):
    topic.metadata_json = {"selection": {"max_selections_per_day": 2, "max_selected_per_run": 5}}
    for index in range(6):
        make_candidate(db, topic, source, f"c{index}", channel_id=f"UC_{index}")
    db.flush()

    service().run(db, topic=topic, dry_run=False, now=NOW)
    service().run(db, topic=topic, dry_run=False, now=NOW)

    total = len([r for r in rows(db) if r.status == VideoCandidateStatus.SELECTED])
    assert total <= 2, "the cap is a property of the day, not of one run"


# ==========================================================================
# API
# ==========================================================================


@pytest.fixture()
def admin_user(db):
    user = User(
        phone_number="+5511988887777",
        full_name="Admin",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
        credits=100,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture()
def client(db, admin_user, no_event_fanout, monkeypatch):
    monkeypatch.setattr(discovery_api, "_selection_service", lambda: service())
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    with TestClient(app) as test_client:
        yield test_client


def test_the_run_endpoint_defaults_to_a_dry_run(client, db, topic, source):
    """Committing is the exception that has to be asked for."""
    make_candidate(db, topic, source, "c0")

    body = client.post("/admin/selection/run", json={"topic_id": str(topic.id)}).json()

    assert body["dry_run"] is True
    assert rows(db)[0].status == VideoCandidateStatus.DISCOVERED


def test_the_run_endpoint_can_commit(client, db, topic, source):
    make_candidate(db, topic, source, "c0")

    body = client.post(
        "/admin/selection/run", json={"topic_id": str(topic.id), "dry_run": False}
    ).json()

    assert body["committed"] == 1
    assert rows(db)[0].status == VideoCandidateStatus.SELECTED


def test_the_run_endpoint_reports_blocked_candidates_with_reasons(client, db, topic, source):
    for index in range(4):
        make_candidate(db, topic, source, f"c{index}", channel_id="UC_same")

    body = client.post("/admin/selection/run", json={"topic_id": str(topic.id)}).json()

    assert body["blocked"]
    assert all(item["blocked_by"] for item in body["blocked"])


def test_an_unbounded_limit_is_rejected_by_the_schema(client, topic):
    response = client.post(
        "/admin/selection/run", json={"topic_id": str(topic.id), "limit": 100_000}
    )
    assert response.status_code == 422


def test_running_an_unknown_topic_is_a_404(client):
    response = client.post("/admin/selection/run", json={"topic_id": str(uuid.uuid4())})
    assert response.status_code == 404


def test_the_run_endpoint_is_admin_only(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        response = anonymous.post(
            "/admin/selection/run", json={"topic_id": str(uuid.uuid4())}
        )
    assert response.status_code in (401, 403)


def test_a_selection_run_is_audited(client, db, topic, source):
    from app.models.audit_log import AuditLog

    make_candidate(db, topic, source, "c0")
    client.post("/admin/selection/run", json={"topic_id": str(topic.id), "dry_run": False})

    assert "admin.selection.run" in {entry.action for entry in db.query(AuditLog).all()}


def test_candidates_can_be_filtered_by_score(client, db, topic, source):
    make_candidate(db, topic, source, "c0")
    client.post("/admin/selection/run", json={"topic_id": str(topic.id), "dry_run": False})

    high = client.get("/admin/video-candidates", params={"min_score": 0.99}).json()
    low = client.get("/admin/video-candidates", params={"min_score": 0.0}).json()

    assert low["total"] >= high["total"]


def test_candidates_can_be_filtered_by_selection_method(client, db, topic, source):
    make_candidate(db, topic, source, "c0")
    client.post("/admin/selection/run", json={"topic_id": str(topic.id), "dry_run": False})

    body = client.get("/admin/video-candidates", params={"selection_method": "policy"}).json()

    assert body["total"] == 1


# ==========================================================================
# Manual selection still works, and is still marked manual
# ==========================================================================


def test_manual_selection_is_recorded_as_manual(client, db, topic, source):
    row = make_candidate(db, topic, source, "c0")

    client.post(f"/admin/video-candidates/{row.id}/select")

    db.refresh(row)
    assert row.metadata_json["selection"]["method"] == METHOD_MANUAL


def test_manual_selection_does_not_bypass_availability(client, db, topic, source):
    """A human may bypass the policy — caps, cooldown, thresholds — not the invariants."""
    row = make_candidate(db, topic, source, "gone", available=False)
    row.status = VideoCandidateStatus.REJECTED
    db.flush()

    response = client.post(f"/admin/video-candidates/{row.id}/select")

    assert response.status_code == 409


def test_auto_and_manual_selection_are_distinguishable(client, db, topic, source):
    auto = make_candidate(db, topic, source, "auto", channel_id="UC_a")
    manual = make_candidate(db, topic, source, "manual", channel_id="UC_b",
                            published_at=ago(hours=50), view_count=10)

    # limit=1 so the policy takes only `auto`; `manual` is left for a person to promote.
    client.post(
        "/admin/selection/run",
        json={"topic_id": str(topic.id), "dry_run": False, "limit": 1},
    )
    client.post(f"/admin/video-candidates/{manual.id}/select")

    db.refresh(auto)
    db.refresh(manual)
    assert auto.metadata_json["selection"]["method"] == METHOD_POLICY
    assert manual.metadata_json["selection"]["method"] == METHOD_MANUAL


# ==========================================================================
# Events
# ==========================================================================


def test_a_run_emits_one_aggregate_event_plus_one_per_selection(db, topic, source, no_event_fanout):
    from app.models.pipeline_event import PipelineEvent

    for index in range(6):
        make_candidate(db, topic, source, f"c{index}", channel_id=f"UC_{index}")

    service().run(db, topic=topic, limit=2, dry_run=False, now=NOW)

    events = db.query(PipelineEvent).filter(PipelineEvent.service == "selection").all()
    stages = [event.stage for event in events]
    assert stages.count("selection.completed") == 1
    assert stages.count("candidate.selected") == 2, "one per domain change, not one per rank"


def test_selection_events_are_not_attached_to_a_pipeline_job(db, topic, source, no_event_fanout):
    from app.models.pipeline_event import PipelineEvent

    make_candidate(db, topic, source, "c0")
    service().run(db, topic=topic, dry_run=False, now=NOW)

    events = db.query(PipelineEvent).filter(PipelineEvent.service == "selection").all()
    assert all(event.pipeline_job_id is None for event in events)
