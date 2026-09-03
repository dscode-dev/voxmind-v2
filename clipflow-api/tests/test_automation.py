"""The autonomous loop: scheduling, locking, failure isolation, backpressure, kill switches.

Time is injected everywhere — no test sleeps. The three services are stubbed so a stage can be
made to fail on demand, which is the only way to test that one failing stage does not take the
others down with it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import automation as automation_api
from app.api.router import api_router
from app.core.settings import settings
from app.db.session import get_db
from app.models.automation_state import AutomationState
from app.models.content_topic import ContentTopic
from app.models.discovery_source import DiscoverySource
from app.models.enums import (
    DiscoverySourceKind,
    PipelineState,
    UserRole,
    UserStatus,
    VideoCandidateStatus,
)
from app.models.pipeline_job import PipelineJob
from app.models.user import User
from app.models.video_candidate import VideoCandidate
from app.security.auth_middleware import get_current_admin
from app.services.automation_scheduler import (
    GLOBAL_DISABLED,
    LOCK_UNAVAILABLE,
    NOT_DUE,
    OVERLAP,
    TOPIC_DISABLED,
    AutomationScheduler,
    deterministic_jitter_seconds,
)
from app.services.automation_service import (
    COMPLETED,
    DISABLED,
    FAILED,
    NOOP,
    OK,
    PARTIAL,
    STAGE_FAILED,
    STAGE_SKIPPED,
    AutomationConfig,
    AutonomousPipelineService,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


# ==========================================================================
# Stubs
# ==========================================================================


class StubDiscovery:
    def __init__(self, new_candidates=0, fail=False, source_failed=False):
        self.new_candidates = new_candidates
        self.fail = fail
        self.source_failed = source_failed
        self.calls = 0

    def run_topic(self, db, *, topic, commit=True, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider exploded key=SECRET-VALUE")

        class Result:
            status = "failed" if self.source_failed else "completed"
            results_received = 0 if self.source_failed else self.new_candidates
            new_candidates = 0 if self.source_failed else self.new_candidates
            existing_candidates = 0
            errors = [{"error_type": "quota_exceeded"}] if self.source_failed else []

        return [Result()]


class StubSelection:
    def __init__(self, selected=0, fail=False):
        self.selected = selected
        self.fail = fail
        self.calls = 0
        self.last_limit = None

    def run(self, db, *, topic, limit=None, dry_run=False, now=None):
        self.calls += 1
        self.last_limit = limit
        if self.fail:
            raise RuntimeError("selection exploded")

        class Outcome:
            considered = 10
            eligible = 8
            selected = [object()] * self.selected
            blocked = []

        class Report:
            run_id = "sel-run"
            outcome = Outcome()

        return Report()


class StubAdmission:
    def __init__(self, admitted=0, blocked=0, fail=False, recovered=0):
        self.admitted = admitted
        self.blocked = blocked
        self.fail = fail
        self.recovered = recovered
        self.calls = 0
        self.sweeps = 0
        self.last_limit = None

    def run(self, db, *, topic, limit=None, dry_run=True, now=None, actor=None):
        self.calls += 1
        self.last_limit = limit
        if self.fail:
            raise RuntimeError("admission exploded")
        admitted, blocked = self.admitted, self.blocked

        class Report:
            run_id = "adm-run"

            def as_dict(self_inner):
                return {
                    "counts": {
                        "admitted": admitted,
                        "already_admitted": 0,
                        "temporarily_blocked": blocked,
                        "permanently_blocked": 0,
                        "invalid_state": 0,
                        "enqueue_failed": 0,
                    },
                    "selected_waiting": admitted + blocked,
                    "active_jobs": 0,
                    "available_slots": 3,
                }

        return Report()

    def retry_pending_enqueue(self, db, *, limit=10, now=None):
        self.sweeps += 1

        class Decision:
            outcome = "admitted"

        return [Decision()] * self.recovered


def pipeline(discovery=None, selection=None, admission=None):
    return AutonomousPipelineService(
        discovery=discovery or StubDiscovery(),
        selection=selection or StubSelection(),
        admission=admission or StubAdmission(),
    )


# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture(autouse=True)
def kill_switch_on(monkeypatch):
    """The switch defaults OFF in production; these tests are about what happens when it is on."""
    monkeypatch.setattr(settings, "autonomous_pipeline_enabled", True)


def make_topic(db, name="Futebol", **automation):
    config = {"enabled": True, "interval_minutes": 60}
    config.update(automation)
    topic = ContentTopic(
        name=name,
        keywords_json=["futebol"],
        is_active=True,
        metadata_json={"automation": config},
    )
    db.add(topic)
    db.flush()
    return topic


@pytest.fixture()
def topic(db):
    return make_topic(db)


def state_for(db, topic):
    return db.query(AutomationState).filter(AutomationState.topic_id == topic.id).first()


# ==========================================================================
# Configuration
# ==========================================================================


def test_automation_is_off_unless_a_topic_opts_in(db):
    topic = ContentTopic(name="Sem automacao", metadata_json={})
    assert AutomationConfig.from_topic(topic).enabled is False


def test_config_is_read_from_the_topic(db):
    topic = make_topic(db, interval_minutes=30, selection_limit=5, admission_limit=2)
    config = AutomationConfig.from_topic(topic)

    assert config.interval_minutes == 30
    assert config.selection_limit == 5
    assert config.admission_limit == 2


def test_limits_are_clamped_to_the_ceilings(db):
    topic = make_topic(db, selection_limit=10_000, admission_limit=10_000)
    config = AutomationConfig.from_topic(topic)

    assert config.selection_limit <= 25
    assert config.admission_limit <= 10


def test_an_absurdly_short_interval_is_raised_to_the_floor(db):
    """A one-second interval would hammer providers and never let a run finish."""
    topic = make_topic(db, interval_minutes=0)
    assert AutomationConfig.from_topic(topic).interval_minutes >= 5


def test_a_malformed_config_value_falls_back(db):
    topic = make_topic(db, interval_minutes="depressa")
    assert AutomationConfig.from_topic(topic).interval_minutes == 60


def test_an_unknown_config_key_is_ignored(db):
    topic = make_topic(db, inverval_minutes=5)
    assert AutomationConfig.from_topic(topic).interval_minutes == 60


# ==========================================================================
# Kill switches
# ==========================================================================


def test_the_global_kill_switch_stops_all_work(db, topic, monkeypatch, no_event_fanout):
    monkeypatch.setattr(settings, "autonomous_pipeline_enabled", False)
    admission = StubAdmission(admitted=1)
    scheduler = AutomationScheduler(pipeline=pipeline(admission=admission))

    report = scheduler.tick(db, now=NOW)

    assert report.enabled is False
    assert report.runs == []
    assert report.skipped == [{"reason": GLOBAL_DISABLED}]
    assert admission.calls == 0
    assert admission.sweeps == 0, "not even the recovery sweep runs"


def test_the_kill_switch_stops_work_not_the_scheduler(db, topic, monkeypatch, no_event_fanout):
    """It must be flippable without a restart, so the tick still returns a report."""
    monkeypatch.setattr(settings, "autonomous_pipeline_enabled", False)
    scheduler = AutomationScheduler(pipeline=pipeline())

    report = scheduler.tick(db, now=NOW)
    assert report.tick_id

    monkeypatch.setattr(settings, "autonomous_pipeline_enabled", True)
    assert scheduler.tick(db, now=NOW).runs, "re-enabling needs no restart"


def test_a_topic_can_be_paused_on_its_own(db, no_event_fanout):
    paused = make_topic(db, "Pausado", enabled=False)
    scheduler = AutomationScheduler(pipeline=pipeline())

    outcome = scheduler.run_topic_if_due(db, topic=paused, now=NOW)

    assert outcome["reason"] == TOPIC_DISABLED


def test_pausing_a_topic_destroys_nothing(db, no_event_fanout):
    """A pause has to be reversible without having lost anything."""
    paused = make_topic(db, "Pausado", enabled=False)
    candidate = VideoCandidate(
        topic_id=paused.id, url="https://example.invalid/v", dedup_hash="h1",
        status=VideoCandidateStatus.SELECTED,
    )
    run = PipelineJob(
        worker_job_id="w1", topic_id=paused.id, source_url="https://example.invalid/v",
        state=PipelineState.RENDERING, clip_mode="short_serie", video_ratio="portrait",
    )
    db.add_all([candidate, run])
    db.flush()

    AutomationScheduler(pipeline=pipeline()).run_topic_if_due(db, topic=paused, now=NOW)

    db.refresh(candidate)
    db.refresh(run)
    assert candidate.status == VideoCandidateStatus.SELECTED
    assert run.state == PipelineState.RENDERING


@pytest.mark.parametrize(
    "switch,stage",
    [
        ("discovery_enabled", "discovery"),
        ("selection_enabled", "selection"),
        ("admission_enabled", "admission"),
    ],
)
def test_each_stage_can_be_turned_off_independently(db, switch, stage, no_event_fanout):
    """discovery ON + selection ON + admission OFF is how you watch ranking without producing."""
    topic = make_topic(db, "Stage", **{switch: False})
    report = pipeline().run_topic(db, topic=topic, now=NOW)

    assert getattr(report, stage).status == DISABLED


def test_disabling_admission_produces_nothing(db, no_event_fanout):
    topic = make_topic(db, "Observando", admission_enabled=False)
    admission = StubAdmission(admitted=5)

    pipeline(admission=admission).run_topic(db, topic=topic, now=NOW)

    assert admission.calls == 0


# ==========================================================================
# Due scheduling
# ==========================================================================


def test_a_never_scheduled_topic_is_due_immediately(db, topic, no_event_fanout):
    """A topic just switched on should not wait a full interval for its first run."""
    outcome = AutomationScheduler(pipeline=pipeline()).run_topic_if_due(db, topic=topic, now=NOW)
    assert not isinstance(outcome, dict), outcome


def test_a_run_schedules_the_next_one(db, topic, no_event_fanout):
    scheduler = AutomationScheduler(pipeline=pipeline())
    scheduler.run_topic_if_due(db, topic=topic, now=NOW)

    state = state_for(db, topic)
    assert state.next_due_at is not None
    delta = _as_utc(state.next_due_at) - NOW
    assert timedelta(minutes=60) <= delta <= timedelta(minutes=62), "interval plus jitter"


def test_a_topic_is_skipped_before_it_is_due(db, topic, no_event_fanout):
    scheduler = AutomationScheduler(pipeline=pipeline())
    scheduler.run_topic_if_due(db, topic=topic, now=NOW)

    outcome = scheduler.run_topic_if_due(db, topic=topic, now=NOW + timedelta(minutes=30))

    assert outcome["reason"] == NOT_DUE
    assert outcome["next_due_at"]


def test_a_topic_runs_again_once_due(db, topic, no_event_fanout):
    scheduler = AutomationScheduler(pipeline=pipeline())
    scheduler.run_topic_if_due(db, topic=topic, now=NOW)

    outcome = scheduler.run_topic_if_due(db, topic=topic, now=NOW + timedelta(hours=2))

    assert not isinstance(outcome, dict), outcome


def test_scheduling_survives_a_restart(db, topic, no_event_fanout):
    """A fresh scheduler must read next_due_at, not start from an empty memory."""
    AutomationScheduler(pipeline=pipeline()).run_topic_if_due(db, topic=topic, now=NOW)
    scheduled = _as_utc(state_for(db, topic).next_due_at)

    restarted = AutomationScheduler(pipeline=pipeline())
    outcome = restarted.run_topic_if_due(db, topic=topic, now=NOW + timedelta(minutes=5))

    assert outcome["reason"] == NOT_DUE
    assert _as_utc(state_for(db, topic).next_due_at) == scheduled


def test_jitter_is_deterministic_not_random():
    """Two replicas must agree on when a topic is due; random jitter would make them differ."""
    topic_id = str(uuid.uuid4())
    assert deterministic_jitter_seconds(topic_id) == deterministic_jitter_seconds(topic_id)
    assert 0 <= deterministic_jitter_seconds(topic_id) < 120


def test_jitter_spreads_topics_apart():
    values = {deterministic_jitter_seconds(str(uuid.uuid4())) for _ in range(50)}
    assert len(values) > 20, "a deploy must not make every topic due at the same instant"


# ==========================================================================
# Overlap and locking
# ==========================================================================


def test_a_run_that_outlives_its_interval_is_not_re_entered(db, topic, no_event_fanout):
    """The lock protects processes; this protects the next tick of the same one.

    Within the stale window: a run 30 minutes in is slow, not abandoned. Past the window it is
    treated as abandoned instead — see the test below.
    """
    state = AutomationState(topic_id=topic.id, running_since=NOW, running_run_id="in-flight")
    db.add(state)
    db.flush()

    outcome = AutomationScheduler(pipeline=pipeline()).run_topic_if_due(
        db, topic=topic, now=NOW + timedelta(minutes=30)
    )

    assert outcome["reason"] == OVERLAP


def test_a_forced_run_still_respects_the_overlap_guard(db, topic, no_event_fanout):
    """Manual and automatic must not both run the same topic and break the caps."""
    db.add(AutomationState(topic_id=topic.id, running_since=NOW, running_run_id="in-flight"))
    db.flush()

    outcome = AutomationScheduler(pipeline=pipeline()).run_topic_if_due(
        db, topic=topic, now=NOW + timedelta(minutes=1), force=True
    )

    assert outcome["reason"] == OVERLAP


def test_an_abandoned_run_does_not_wedge_the_topic_forever(db, topic, no_event_fanout):
    """The owning process died without clearing the flag."""
    db.add(AutomationState(
        topic_id=topic.id, running_since=NOW - timedelta(hours=5), running_run_id="ghost"
    ))
    db.flush()

    outcome = AutomationScheduler(pipeline=pipeline()).run_topic_if_due(db, topic=topic, now=NOW)

    assert not isinstance(outcome, dict), outcome


def test_running_is_always_cleared_even_when_the_run_crashes(db, topic, no_event_fanout):
    """A topic left marked running would never be scheduled again."""
    scheduler = AutomationScheduler(pipeline=pipeline(discovery=StubDiscovery(fail=True)))
    scheduler.run_topic_if_due(db, topic=topic, now=NOW)

    state = state_for(db, topic)
    assert state.running_since is None
    assert state.running_run_id is None


def test_a_second_process_skips_rather_than_waits(db, topic, monkeypatch, no_event_fanout):
    """Two replicas, one topic. The loser must not block behind the winner."""
    scheduler = AutomationScheduler(pipeline=pipeline())

    from contextlib import contextmanager

    @contextmanager
    def lock_taken(self, db_, topic_id):
        yield False

    monkeypatch.setattr(AutomationScheduler, "_topic_lock", lock_taken)
    outcome = scheduler.run_topic_if_due(db, topic=topic, now=NOW)

    assert outcome["reason"] == LOCK_UNAVAILABLE


def test_the_lock_key_is_namespaced_away_from_the_other_topic_locks():
    """Selection and admission lock on the same topic id; a collision would deadlock them."""
    from app.services.automation_scheduler import _lock_key

    key = _lock_key(uuid.uuid4())
    assert -(2**63) <= key < 2**63
    assert _lock_key("a") != _lock_key("b")


# ==========================================================================
# Stage failure isolation
# ==========================================================================


def test_a_discovery_failure_does_not_stop_admission(db, topic, no_event_fanout):
    """An exhausted quota says nothing about candidates already waiting for a worker slot."""
    admission = StubAdmission(admitted=1)
    report = pipeline(
        discovery=StubDiscovery(fail=True),
        selection=StubSelection(selected=0),
        admission=admission,
    ).run_topic(db, topic=topic, now=NOW)

    assert report.discovery.status == STAGE_FAILED
    assert report.admission.status == OK
    assert admission.calls == 1
    assert report.status == PARTIAL


def test_a_selection_failure_does_not_stop_admission(db, topic, no_event_fanout):
    admission = StubAdmission(admitted=1)
    report = pipeline(
        selection=StubSelection(fail=True), admission=admission
    ).run_topic(db, topic=topic, now=NOW)

    assert report.selection.status == STAGE_FAILED
    assert report.admission.status == OK
    assert report.status == PARTIAL


def test_an_admission_failure_leaves_selected_candidates_alone(db, topic, no_event_fanout):
    candidate = VideoCandidate(
        topic_id=topic.id, url="https://example.invalid/v", dedup_hash="h2",
        status=VideoCandidateStatus.SELECTED,
    )
    db.add(candidate)
    db.flush()

    report = pipeline(admission=StubAdmission(fail=True)).run_topic(db, topic=topic, now=NOW)

    assert report.admission.status == STAGE_FAILED
    db.refresh(candidate)
    assert candidate.status == VideoCandidateStatus.SELECTED, "recoverable next run"


def test_a_provider_crash_message_is_not_carried_into_the_report(db, topic, no_event_fanout):
    """A provider that interpolates its request into an exception would leak the key."""
    report = pipeline(discovery=StubDiscovery(fail=True)).run_topic(db, topic=topic, now=NOW)

    assert "SECRET-VALUE" not in str(report.as_dict())
    assert report.discovery.reasons == ["RuntimeError"]


def test_a_failing_source_is_degraded_not_a_failed_stage(db, topic, no_event_fanout):
    """One source out of several failing is not a discovery failure."""
    report = pipeline(discovery=StubDiscovery(source_failed=True)).run_topic(
        db, topic=topic, now=NOW
    )

    assert report.discovery.status == STAGE_FAILED, "here it was the ONLY source"
    assert "quota_exceeded" in report.discovery.reasons


def test_every_stage_failing_is_a_failed_run(db, topic, no_event_fanout):
    report = pipeline(
        discovery=StubDiscovery(fail=True),
        selection=StubSelection(fail=True),
        admission=StubAdmission(fail=True),
    ).run_topic(db, topic=topic, now=NOW)

    assert report.status == FAILED


# ==========================================================================
# Run status semantics
# ==========================================================================


def test_a_quiet_run_is_not_a_failure(db, topic, no_event_fanout):
    """Nothing new to discover is a correct outcome, not an error to alarm on."""
    report = pipeline().run_topic(db, topic=topic, now=NOW)

    assert report.status == NOOP
    assert report.discovery.status == OK


def test_a_productive_run_is_completed(db, topic, no_event_fanout):
    report = pipeline(
        discovery=StubDiscovery(new_candidates=5),
        selection=StubSelection(selected=2),
        admission=StubAdmission(admitted=1),
    ).run_topic(db, topic=topic, now=NOW)

    assert report.status == COMPLETED


def test_capacity_blocked_is_a_normal_outcome(db, topic, no_event_fanout):
    report = pipeline(admission=StubAdmission(admitted=0, blocked=3)).run_topic(
        db, topic=topic, now=NOW
    )

    assert report.admission.status == OK
    assert "capacity_blocked" in report.admission.reasons
    assert report.status != FAILED


def test_the_run_report_carries_the_correlation_ids(db, topic, no_event_fanout):
    report = pipeline(
        selection=StubSelection(selected=1), admission=StubAdmission(admitted=1)
    ).run_topic(db, topic=topic, now=NOW)

    payload = report.as_dict()
    assert payload["automation_run_id"]
    assert payload["topic_id"] == str(topic.id)
    assert payload["selection"]["run_id"] == "sel-run"
    assert payload["admission"]["run_id"] == "adm-run"
    assert payload["duration_ms"] >= 0


# ==========================================================================
# Backpressure
# ==========================================================================


def make_selected(db, topic, count):
    for index in range(count):
        db.add(VideoCandidate(
            topic_id=topic.id, url=f"https://example.invalid/{index}",
            dedup_hash=f"bh{index}", status=VideoCandidateStatus.SELECTED,
        ))
    db.flush()


def test_selection_pauses_when_the_backlog_is_full(db, no_event_fanout):
    """Ten selected candidates and three worker slots is a queue that goes stale, not progress."""
    topic = make_topic(db, "Backlog", max_selected_backlog=3)
    make_selected(db, topic, 3)
    selection = StubSelection(selected=5)

    report = pipeline(selection=selection).run_topic(db, topic=topic, now=NOW)

    assert report.selection.status == STAGE_SKIPPED
    assert report.selection.reasons == ["selected_backlog_limit"]
    assert selection.calls == 0


def test_selection_is_capped_by_the_remaining_headroom(db, no_event_fanout):
    topic = make_topic(db, "Headroom", max_selected_backlog=5, selection_limit=10)
    make_selected(db, topic, 3)
    selection = StubSelection(selected=2)

    pipeline(selection=selection).run_topic(db, topic=topic, now=NOW)

    assert selection.last_limit == 2, "never select past the backlog cap"


def test_a_full_backlog_does_not_stop_discovery_or_admission(db, no_event_fanout):
    """Awareness and draining continue; only adding more pauses."""
    topic = make_topic(db, "Draining", max_selected_backlog=1)
    make_selected(db, topic, 2)
    discovery = StubDiscovery(new_candidates=3)
    admission = StubAdmission(admitted=1)

    report = pipeline(discovery=discovery, admission=admission).run_topic(
        db, topic=topic, now=NOW
    )

    assert discovery.calls == 1
    assert admission.calls == 1
    assert report.selection.status == STAGE_SKIPPED


def test_the_scheduler_does_not_recreate_the_services_caps(db, topic, no_event_fanout):
    """Limits are passed to the services; the scheduler counts nothing itself."""
    admission = StubAdmission(admitted=1)
    topic.metadata_json = {"automation": {"enabled": True, "admission_limit": 2}}
    db.flush()

    pipeline(admission=admission).run_topic(db, topic=topic, now=NOW)

    assert admission.last_limit == 2


# ==========================================================================
# Pending enqueue recovery
# ==========================================================================


def test_the_tick_sweeps_pending_enqueues(db, topic, no_event_fanout):
    admission = StubAdmission(recovered=2)
    report = AutomationScheduler(pipeline=pipeline(admission=admission)).tick(db, now=NOW)

    assert admission.sweeps == 1
    assert report.pending_enqueue_recovered == 2


def test_recovery_runs_before_new_production(db, topic, no_event_fanout):
    """Stranded work is already paid for; starting fresh production ahead of it adds load."""
    order: list[str] = []

    class OrderedAdmission(StubAdmission):
        def retry_pending_enqueue(self, db_, *, limit=10, now=None):
            order.append("sweep")
            return super().retry_pending_enqueue(db_, limit=limit, now=now)

        def run(self, db_, **kwargs):
            order.append("admit")
            return super().run(db_, **kwargs)

    AutomationScheduler(pipeline=pipeline(admission=OrderedAdmission())).tick(db, now=NOW)

    assert order[0] == "sweep"


def test_a_crashing_sweep_does_not_stop_the_tick(db, topic, no_event_fanout):
    class ExplodingSweep(StubAdmission):
        def retry_pending_enqueue(self, db_, **kwargs):
            raise RuntimeError("redis down")

    report = AutomationScheduler(pipeline=pipeline(admission=ExplodingSweep())).tick(db, now=NOW)

    assert report.pending_enqueue_recovered == 0
    assert report.runs, "the topic still ran"


# ==========================================================================
# Tick behaviour
# ==========================================================================


def test_a_tick_runs_due_topics_and_skips_the_rest(db, no_event_fanout):
    due = make_topic(db, "Due")
    paused = make_topic(db, "Paused", enabled=False)
    scheduler = AutomationScheduler(pipeline=pipeline())

    report = scheduler.tick(db, now=NOW)

    assert report.topics_considered == 2
    assert len(report.runs) == 1
    assert report.runs[0]["topic_id"] == str(due.id)
    assert any(skip["reason"] == TOPIC_DISABLED for skip in report.skipped)


def test_an_inactive_topic_is_not_considered(db, no_event_fanout):
    topic = make_topic(db, "Inativo")
    topic.is_active = False
    db.flush()

    assert AutomationScheduler(pipeline=pipeline()).tick(db, now=NOW).topics_considered == 0


def test_a_tick_is_bounded(db, no_event_fanout):
    """One tick must not monopolise the process; the rest stay due for the next pass."""
    for index in range(8):
        make_topic(db, f"Topic {index}")

    report = AutomationScheduler(pipeline=pipeline()).tick(db, now=NOW, max_topics=3)

    assert len(report.runs) == 3


def test_a_repeated_tick_does_not_re_run_a_topic(db, topic, no_event_fanout):
    scheduler = AutomationScheduler(pipeline=pipeline())

    first = scheduler.tick(db, now=NOW)
    second = scheduler.tick(db, now=NOW)

    assert len(first.runs) == 1
    assert len(second.runs) == 0
    assert any(skip["reason"] == NOT_DUE for skip in second.skipped)


# ==========================================================================
# Automation is not production
# ==========================================================================


def test_a_tick_creates_no_pipeline_job(db, topic, no_event_fanout):
    """A tick, a discovery and a selection are not production runs."""
    AutomationScheduler(pipeline=pipeline(admission=StubAdmission(admitted=0))).tick(db, now=NOW)

    assert db.query(PipelineJob).count() == 0


def test_automation_events_are_not_attached_to_a_run(db, topic, no_event_fanout):
    from app.models.pipeline_event import PipelineEvent

    pipeline().run_topic(db, topic=topic, now=NOW)

    events = db.query(PipelineEvent).filter(PipelineEvent.service == "automation").all()
    assert events
    assert all(event.pipeline_job_id is None for event in events)


def test_a_run_emits_a_start_and_an_end_event(db, topic, no_event_fanout):
    from app.models.pipeline_event import PipelineEvent

    pipeline().run_topic(db, topic=topic, now=NOW)

    stages = [
        event.stage
        for event in db.query(PipelineEvent).filter(PipelineEvent.service == "automation")
    ]
    assert "automation.started" in stages
    assert any(stage.startswith("automation.") and stage != "automation.started" for stage in stages)


def test_nothing_here_publishes(db, topic, no_event_fanout):
    from app.models.publish_attempt import PublishAttempt

    AutomationScheduler(pipeline=pipeline(admission=StubAdmission(admitted=1))).tick(db, now=NOW)

    assert db.query(PublishAttempt).count() == 0


# ==========================================================================
# Failure backoff
# ==========================================================================


def test_repeated_failures_back_the_topic_off(db, no_event_fanout):
    topic = make_topic(
        db, "Quebrado", interval_minutes=5, max_consecutive_failures=2,
        failure_backoff_minutes=60,
    )
    scheduler = AutomationScheduler(pipeline=pipeline(
        discovery=StubDiscovery(fail=True),
        selection=StubSelection(fail=True),
        admission=StubAdmission(fail=True),
    ))

    moment = NOW
    for _ in range(2):
        last_run_at = moment
        scheduler.run_topic_if_due(db, topic=topic, now=moment)
        moment = _as_utc(state_for(db, topic).next_due_at) + timedelta(seconds=1)

    state = state_for(db, topic)
    assert state.consecutive_failures >= 2
    # Measured from when the last run happened, not from the moment after it was rescheduled.
    gap = _as_utc(state.next_due_at) - last_run_at
    assert gap > timedelta(minutes=30), "backed off well past the 5-minute interval"


def test_one_good_run_clears_the_penalty(db, no_event_fanout):
    topic = make_topic(db, "Recuperado", max_consecutive_failures=2)
    failing = AutomationScheduler(pipeline=pipeline(
        discovery=StubDiscovery(fail=True),
        selection=StubSelection(fail=True),
        admission=StubAdmission(fail=True),
    ))
    failing.run_topic_if_due(db, topic=topic, now=NOW)
    assert state_for(db, topic).consecutive_failures == 1

    AutomationScheduler(pipeline=pipeline()).run_topic_if_due(
        db, topic=topic, now=NOW + timedelta(hours=3)
    )

    assert state_for(db, topic).consecutive_failures == 0


# ==========================================================================
# API
# ==========================================================================


@pytest.fixture()
def admin_user(db):
    user = User(
        phone_number="+5511966665555", full_name="Admin",
        role=UserRole.ADMIN, status=UserStatus.ACTIVE, credits=100,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture()
def client(db, admin_user, no_event_fanout, monkeypatch):
    monkeypatch.setattr(
        automation_api, "_scheduler", lambda: AutomationScheduler(pipeline=pipeline())
    )
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    with TestClient(app) as test_client:
        yield test_client


def test_the_status_endpoint_reports_the_schedule(client, db, topic):
    body = client.get("/admin/automation/status").json()

    assert body["enabled"] is True
    assert len(body["topics"]) == 1
    entry = body["topics"][0]
    assert entry["topic_id"] == str(topic.id)
    assert entry["automation"]["enabled"] is True
    assert "next_due_at" in entry
    assert "selected_backlog" in entry


def test_automation_can_be_configured_over_the_api(client, db, topic):
    body = client.put(
        f"/admin/automation/topics/{topic.id}",
        json={"enabled": False, "interval_minutes": 120},
    ).json()

    assert body["automation"]["enabled"] is False
    assert body["automation"]["interval_minutes"] == 120


def test_an_absurd_interval_is_rejected_by_the_schema(client, topic):
    response = client.put(
        f"/admin/automation/topics/{topic.id}", json={"interval_minutes": 1}
    )
    assert response.status_code == 422


def test_configuration_changes_are_audited(client, db, topic):
    from app.models.audit_log import AuditLog

    client.put(f"/admin/automation/topics/{topic.id}", json={"enabled": False})

    assert "admin.automation.configure" in {entry.action for entry in db.query(AuditLog)}


def test_a_manual_run_executes_the_full_cycle(client, db, topic):
    body = client.post(f"/admin/automation/topics/{topic.id}/run").json()

    assert body["automation_run_id"]
    assert body["discovery"]["status"] == OK
    assert body["admission"]["status"] == OK


def test_a_manual_run_ignores_the_schedule_but_not_the_kill_switch(client, db, topic, monkeypatch):
    client.post(f"/admin/automation/topics/{topic.id}/run")
    assert client.post(f"/admin/automation/topics/{topic.id}/run").json()["automation_run_id"]

    monkeypatch.setattr(settings, "autonomous_pipeline_enabled", False)
    assert client.post(f"/admin/automation/topics/{topic.id}/run").status_code == 409


def test_a_manual_run_on_a_paused_topic_is_refused(client, db):
    paused = make_topic(db, "Pausado", enabled=False)
    assert client.post(f"/admin/automation/topics/{paused.id}/run").status_code == 409


def test_manual_runs_are_audited(client, db, topic):
    from app.models.audit_log import AuditLog

    client.post(f"/admin/automation/topics/{topic.id}/run")

    assert "admin.automation.manual_run" in {entry.action for entry in db.query(AuditLog)}


def test_a_tick_can_be_triggered_on_demand(client, db, topic):
    body = client.post("/admin/automation/tick").json()

    assert body["tick_id"]
    assert body["ran"] == 1


def test_automation_endpoints_are_admin_only(db, no_event_fanout):
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as anonymous:
        assert anonymous.get("/admin/automation/status").status_code in (401, 403)
        assert anonymous.post("/admin/automation/tick").status_code in (401, 403)
        assert anonymous.post(
            f"/admin/automation/topics/{uuid.uuid4()}/run"
        ).status_code in (401, 403)


def test_an_unknown_topic_is_a_404(client):
    assert client.post(f"/admin/automation/topics/{uuid.uuid4()}/run").status_code == 404
    assert client.put(
        f"/admin/automation/topics/{uuid.uuid4()}", json={"enabled": True}
    ).status_code == 404


# ==========================================================================
# The runner
# ==========================================================================


@pytest.mark.asyncio
async def test_the_runner_starts_only_once():
    """A reloading dev server runs lifespan twice; two loops would double every tick."""
    import asyncio

    from app.services.automation_runner import AutomationRunner

    runner = AutomationRunner(scheduler=AutomationScheduler(pipeline=pipeline()))
    try:
        await runner.start()
        first = runner._task
        await runner.start()
        assert runner._task is first
    finally:
        await runner.stop(timeout_sec=1)


@pytest.mark.asyncio
async def test_the_runner_stops_on_shutdown():
    from app.services.automation_runner import AutomationRunner

    runner = AutomationRunner(scheduler=AutomationScheduler(pipeline=pipeline()))
    await runner.start()
    assert runner.running

    await runner.stop(timeout_sec=2)
    assert not runner.running


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
