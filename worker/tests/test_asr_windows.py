"""Window planning, absolute offsets and overlap cost (PR-ASR-01)."""

import pytest

from app.media.asr_windows import (
    AsrWindow,
    build_windows,
    overlap_overhead,
    total_processed_seconds,
)


# ==========================================================================
# Overlap generation
# ==========================================================================


def test_windows_without_overlap_are_adjacent():
    """The previous behaviour, still reachable by setting overlap to 0."""
    windows = build_windows(2700, 900, 0)

    assert [(w.start, w.end) for w in windows] == [
        (0.0, 900.0),
        (900.0, 1800.0),
        (1800.0, 2700.0),
    ]
    assert all(w.overlap_duration == 0 for w in windows)


def test_each_window_after_the_first_overlaps_its_predecessor():
    windows = build_windows(2700, 900, 5)

    for previous, current in zip(windows, windows[1:]):
        assert current.start < previous.end, "windows must share a region"
        assert previous.end - current.start == pytest.approx(5.0)


def test_the_first_window_has_no_overlap():
    windows = build_windows(2700, 900, 5)
    assert windows[0].start == 0.0
    assert windows[0].overlap_duration == 0.0


def test_windows_cover_the_whole_audio():
    windows = build_windows(2700, 900, 5)
    assert windows[0].start == 0.0
    assert windows[-1].end == 2700.0
    # No gaps: each window starts before the previous ended.
    for previous, current in zip(windows, windows[1:]):
        assert current.start <= previous.end


def test_shared_region_is_from_the_window_start_to_the_previous_end():
    windows = build_windows(2700, 900, 5)
    first, second = windows[0], windows[1]

    assert second.overlap_start == second.start == 895.0
    assert second.overlap_end == 900.0 == first.end


@pytest.mark.parametrize("overlap", [0, 1, 5, 30, 120])
def test_overlap_is_configurable(overlap):
    windows = build_windows(3600, 600, overlap)
    for previous, current in zip(windows, windows[1:]):
        assert previous.end - current.start == pytest.approx(overlap)


def test_overlap_larger_than_the_window_is_clamped():
    """Never let overlap stall the scan."""
    windows = build_windows(1000, 100, 500)
    assert len(windows) > 1
    for previous, current in zip(windows, windows[1:]):
        assert current.start > previous.start


def test_short_final_window_is_kept():
    windows = build_windows(950, 900, 5)
    assert len(windows) == 2
    assert windows[-1].end == 950.0
    assert windows[-1].duration == pytest.approx(55.0)


def test_audio_shorter_than_one_window_produces_one_window():
    windows = build_windows(120, 900, 5)
    assert len(windows) == 1
    assert (windows[0].start, windows[0].end) == (0.0, 120.0)


def test_zero_duration_produces_no_windows():
    assert build_windows(0, 900, 5) == []


def test_non_positive_window_is_rejected():
    with pytest.raises(ValueError):
        build_windows(100, 0, 5)


# ==========================================================================
# Absolute offsets
# ==========================================================================


def test_window_relative_times_convert_to_absolute():
    window = AsrWindow(index=2, start=1790.0, end=2690.0, overlap_sec=5.0)

    assert window.to_absolute(0.0) == 1790.0
    assert window.to_absolute(12.5) == 1802.5


def test_absolute_offsets_stay_referenced_to_the_original_video():
    windows = build_windows(2700, 900, 5)
    # A segment 10s into the third window is at 1800s of video, not 10s.
    assert windows[2].to_absolute(10.0) == pytest.approx(1800.0)


def test_edge_detection_identifies_truncated_segments():
    window = AsrWindow(index=1, start=895.0, end=1795.0, overlap_sec=5.0)

    assert window.touches_start(895.0) is True
    assert window.touches_start(900.0) is False
    assert window.touches_end(1795.0) is True
    assert window.touches_end(1700.0) is False


# ==========================================================================
# Cost
# ==========================================================================


def test_overhead_ratio_is_one_without_overlap():
    windows = build_windows(5400, 900, 0)
    assert overlap_overhead(windows, 5400)["overhead_ratio"] == 1.0


def test_five_second_overlap_costs_well_under_one_percent():
    windows = build_windows(5400, 900, 5)
    profile = overlap_overhead(windows, 5400)

    assert profile["overhead_ratio"] < 1.01
    assert profile["overlap_seconds_total"] == pytest.approx(30.0)


def test_processed_seconds_includes_the_duplicated_region():
    windows = build_windows(5400, 900, 5)
    assert total_processed_seconds(windows) > 5400
