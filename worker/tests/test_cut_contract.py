"""Duration contract, silent-drop invariant and stable cut identity (PR-CUT-01).

Characterization first: the tests named ``..._used_to_...`` describe the behaviour that was
present before this PR, so the regression is pinned rather than remembered.
"""

from pathlib import Path

import pytest

from app.pipeline.cut_contract import (
    CutContractViolation,
    CutLedger,
    DurationContract,
    assign_cut_ids,
    cut_duration,
    make_cut_id,
)
from app.pipeline.presets import resolve_clip_preset
from app.video.cutter import VideoCutter


def cut(start, end, **extra):
    return {"start": start, "end": end, "safe_start": start, "safe_end": end, **extra}


@pytest.fixture
def cutter(tmp_path):
    return VideoCutter(tmp_path, min_renderable_duration_sec=1.0, job_id="job-test")


# ==========================================================================
# The three minimums are distinct and consistent
# ==========================================================================


def test_contract_names_three_distinct_minimums():
    preset = resolve_clip_preset("short_serie", "portrait")
    contract = DurationContract.from_preset(preset, min_renderable_cut_duration_sec=1.0)

    assert contract.min_renderable_cut_duration_sec == 1.0
    assert contract.min_internal_cut_duration_sec == 12.0
    assert contract.min_final_video_duration_sec == 60.0
    assert contract.max_final_video_duration_sec == 120.0


def test_technical_floor_may_never_exceed_the_editorial_minimum():
    """The exact inversion that caused the bug: cutter 25s vs preset 12s."""
    with pytest.raises(ValueError, match="inverted"):
        DurationContract(
            min_renderable_cut_duration_sec=25.0,
            min_internal_cut_duration_sec=12.0,
            min_final_video_duration_sec=60.0,
            max_final_video_duration_sec=120.0,
        ).validate()


@pytest.mark.parametrize(
    "clip_mode,ratio",
    [("short_serie", "portrait"), ("short", "portrait"), ("long", "landscape")],
)
def test_every_preset_yields_a_consistent_contract(clip_mode, ratio):
    preset = resolve_clip_preset(clip_mode, ratio)
    DurationContract.from_preset(preset, min_renderable_cut_duration_sec=1.0).validate()


# ==========================================================================
# Case A — short cut preservation
# ==========================================================================


def test_cut_allowed_by_the_preset_is_not_dropped_by_the_cutter(cutter):
    """The regression. An 18s payoff is valid for short_serie (min_internal 12s); the old
    cutter's 25s floor deleted it, turning a two-cut series into a single cut with no
    conclusion."""
    preset = resolve_clip_preset("short_serie", "portrait")
    assert preset.min_internal_cut_duration_sec == 12.0

    cuts = [cut(0.0, 40.0), cut(45.0, 63.0)]  # 40s hook + 18s payoff
    renderable, ledger = cutter.plan(cuts)

    assert len(renderable) == 2
    assert ledger.silent_drop_count == 0
    assert ledger.rejections == []


def test_the_old_25s_floor_would_have_dropped_it():
    """Documents the behaviour being fixed."""
    old_floor = 25.0
    payoff = cut(45.0, 63.0)
    assert cut_duration(payoff) == 18.0
    assert cut_duration(payoff) < old_floor


@pytest.mark.parametrize("duration", [12.0, 12.5, 18.0, 24.9])
def test_durations_between_the_two_minimums_survive(cutter, duration):
    renderable, ledger = cutter.plan([cut(0.0, duration)])
    assert len(renderable) == 1
    assert ledger.silent_drop_count == 0


# ==========================================================================
# 5.3 Render preservation — silent_drop_count == 0
# ==========================================================================


def test_every_accepted_cut_is_either_planned_or_rejected(cutter):
    cuts = [cut(0.0, 30.0), cut(30.0, 30.0), cut(40.0, 40.5), cut(60.0, 90.0)]
    renderable, ledger = cutter.plan(cuts)

    assert len(ledger.accepted) == 4
    assert ledger.silent_drop_count == 0
    assert len(ledger.planned) + len(ledger.rejections) == 4
    assert len(renderable) == 2


def test_rejections_are_attributed_to_a_cut_id_and_a_reason(cutter):
    renderable, ledger = cutter.plan([cut(0.0, 0.0), cut(10.0, 10.4)])

    assert renderable == []
    reasons = {r.reason for r in ledger.rejections}
    assert reasons == {"non_positive_duration", "below_technical_render_floor"}
    for rejection in ledger.rejections:
        assert rejection.cut_id
        assert rejection.stage == "cutter"
        assert rejection.duration_sec is not None


def test_a_silent_drop_is_a_hard_error(tmp_path):
    """The invariant is asserted, not assumed."""
    ledger = CutLedger()
    ledger.accept("cut_a")
    ledger.accept("cut_b")
    ledger.plan("cut_a")

    assert ledger.silent_drops == ["cut_b"]
    assert ledger.silent_drop_count == 1


def test_cut_raises_rather_than_returning_a_short_list(tmp_path, monkeypatch):
    from app.video import cutter as cutter_module

    monkeypatch.setattr(cutter_module, "run_ffmpeg", lambda *a, **k: None)
    instance = VideoCutter(tmp_path, min_renderable_duration_sec=1.0)

    outputs = instance.cut(Path("video.mp4"), [cut(0.0, 30.0), cut(30.0, 60.0)])

    assert len(outputs) == 2
    assert instance.ledger.silent_drop_count == 0
    assert len(instance.ledger.rendered) == 2


def test_cut_contract_violation_names_the_cut():
    error = CutContractViolation("cut_01_00_abc", "silent_drop", "1 cut(s)")
    assert "cut_01_00_abc" in str(error)
    assert error.reason == "silent_drop"


# ==========================================================================
# Case B — cut identity survives a sibling's rejection
# ==========================================================================


def test_cut_ids_are_deterministic():
    first = assign_cut_ids([cut(10.0, 40.0), cut(50.0, 80.0)])
    second = assign_cut_ids([cut(10.0, 40.0), cut(50.0, 80.0)])
    assert [c["cut_id"] for c in first] == [c["cut_id"] for c in second]


def test_cut_ids_are_unique_within_a_video():
    ids = [c["cut_id"] for c in assign_cut_ids([cut(0, 30), cut(30, 60), cut(60, 90)])]
    assert len(set(ids)) == 3


def test_rejecting_b_does_not_relabel_c(cutter):
    """C must keep its own identity when B is rejected — the index-shift bug."""
    a, b, c = cut(0.0, 30.0), cut(30.0, 30.2), cut(40.0, 70.0)
    ids = {x["cut_id"]: x for x in assign_cut_ids([a, b, c])}
    a_id, b_id, c_id = list(ids)

    renderable, ledger = cutter.plan([a, b, c])

    assert [x["cut_id"] for x in renderable] == [a_id, c_id]
    assert ledger.rejected_ids == {b_id}
    # C is still C: it did not inherit B's identity by moving up a slot.
    assert renderable[1]["cut_id"] == c_id


def test_an_existing_cut_id_is_preserved():
    tagged = assign_cut_ids([{**cut(0, 30), "cut_id": "already-assigned"}])
    assert tagged[0]["cut_id"] == "already-assigned"


def test_cut_id_changes_when_the_cut_moves():
    before = make_cut_id(cut(10.0, 40.0), position=0)
    after = make_cut_id(cut(12.0, 40.0), position=0)
    assert before != after
