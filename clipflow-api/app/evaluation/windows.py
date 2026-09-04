"""Canonical observation windows, and the one function that resolves them.

A raw snapshot is not comparable to another raw snapshot. "100 views" means something
extraordinary an hour after publication and something disappointing after two weeks, and the
series in ``video_performance_snapshots`` records whichever moments the collector happened to
be awake for. Comparing publications therefore requires asking every one of them the *same*
question: **what was observable roughly N hours after this video was published?**

That is all a canonical window is. It is not a metric, not a score, and not a correction — it
is a rule for picking which existing observation answers a fixed question, so that two videos
can be put beside each other without the comparison silently being about their ages.

**Deterministic, and in one place.** Resolution lives in `resolve_window` and nowhere else.
Spreading "find the snapshot near 24h" across queries is how two call sites end up with two
subtly different definitions of `views_24h`, and no way to tell which produced a given number.

**No interpolation.** If no acceptable observation exists, the answer is *unavailable* — never
a value computed between two snapshots. View growth is not linear (a video does not accrue
views at 03:00 the way it does in its first hour), so a straight line between 23h and 25h is a
fabrication that would be indistinguishable from a measurement once written down.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Protocol, Sequence

# Bumped whenever a target, a tolerance or the resolution rule below changes. Two datasets
# built under different policy versions are not comparable, and the version is what makes
# that visible instead of a silent shift in what `views_24h` means.
WINDOW_POLICY_VERSION = "windows-v1"

# ---------------------------------------------------------------------------
# Availability
#
# Five outcomes, because collapsing them to NULL would throw away the one thing a data
# quality report needs: *why* a number is absent. "Too early to know" and "we should have
# known and did not" call for opposite responses -- wait, versus go and look at the
# collector.
# ---------------------------------------------------------------------------

AVAILABLE = "available"
# The acceptance interval has not closed yet, as of the dataset's `as_of`. Not missing data:
# an observation may still legitimately arrive.
NOT_MATURE = "not_mature"
# The interval closed and no snapshot fell inside it. This is real missing data, and it is a
# statement about the collector rather than about the video.
MISSING_SNAPSHOT = "missing_snapshot"
# A snapshot exists in the interval, but the provider did not return the video: deleted, made
# private, region-blocked. An observation of absence, which is not the same as no observation.
VIDEO_NOT_RETURNED = "video_not_returned"

AVAILABILITY_STATES = (AVAILABLE, NOT_MATURE, MISSING_SNAPSHOT, VIDEO_NOT_RETURNED)

# What the ingestion layer writes when it actually measured a video.
_SNAPSHOT_OK = "ok"


@dataclass(frozen=True)
class Window:
    """One canonical question, and how much lateness still counts as an answer."""

    name: str
    target: timedelta
    tolerance: timedelta

    @property
    def target_seconds(self) -> int:
        return int(self.target.total_seconds())

    @property
    def tolerance_seconds(self) -> int:
        return int(self.tolerance.total_seconds())


# The canonical set. Five windows, deliberately: one per regime a short video actually passes
# through (the first hour, the first evening, the first day, the weekend, the first week).
# Twenty windows would not add information -- adjacent ones would be answered by the same
# snapshot -- and every extra window is another column of mostly-NULL in the export.
#
# The tolerances are MEASURED, not assumed. Replaying the shipped PR-METRICS-01 cadence
# (hourly under 24h, then 6-hourly to 7d, then daily) against a 15-minute collection tick
# gives the earliest observation at or after each target:
#
#     window   first observation   lag
#     1h       1h00m               0h00m
#     6h       6h00m               0h00m
#     24h      29h00m              5h00m
#     72h      77h00m              5h00m
#     7d       191h00m             23h00m
#
# The 24h/72h/7d lags are not noise: the collection interval widens *at* those ages, so the
# schedule steps straight over the boundary it was asked about. A 24h-old video is due again
# 6h later, not 1h later, so nothing is captured between 24h and 29h. Tolerances are therefore
# sized as the measured need plus real margin, rather than copied from a plausible-looking
# table that would have dropped every 24h and 7d row the first time the loop paused.
#
# This is a limitation of the ingestion schedule, and it is reported as such: `views_24h` is
# honestly "views at the first observation from 24h onward", and `observation_lag_seconds` on
# every row says how much later that was.
WINDOWS: tuple[Window, ...] = (
    Window("1h", timedelta(hours=1), timedelta(hours=1)),
    Window("6h", timedelta(hours=6), timedelta(hours=2)),
    Window("24h", timedelta(hours=24), timedelta(hours=8)),
    Window("72h", timedelta(hours=72), timedelta(hours=12)),
    Window("7d", timedelta(days=7), timedelta(hours=30)),
)

WINDOWS_BY_NAME: dict[str, Window] = {window.name: window for window in WINDOWS}
WINDOW_NAMES: tuple[str, ...] = tuple(window.name for window in WINDOWS)


class SnapshotLike(Protocol):
    """What the resolver needs from an observation. Deliberately not the ORM model.

    Keeping the contract this narrow is what lets the resolution rule be tested as pure
    arithmetic over a handful of tuples, with no database and no fixtures.
    """

    id: object
    captured_at: datetime
    availability: str
    view_count: int | None
    like_count: int | None
    comment_count: int | None


@dataclass(frozen=True)
class WindowObservation:
    """The answer for one window: a verdict, and — when there is one — the evidence."""

    window: str
    availability: str
    target_age_seconds: int
    tolerance_seconds: int

    # Populated only when a snapshot was selected (AVAILABLE or VIDEO_NOT_RETURNED).
    snapshot_id: str | None = None
    observed_at: datetime | None = None
    actual_age_seconds: int | None = None

    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None

    @property
    def measured(self) -> bool:
        return self.availability == AVAILABLE

    @property
    def observation_lag_seconds(self) -> int | None:
        """How much later than asked the observation actually was.

        Reported rather than corrected. It is the honest width of the gap between the
        question ("at 24h") and the evidence ("at 29h"), and an analyst who cares can filter
        on it; a resolver that quietly hid it would make every window look exact.
        """
        if self.actual_age_seconds is None:
            return None
        return self.actual_age_seconds - self.target_age_seconds


def resolve_window(
    published_at: datetime,
    snapshots: Sequence[SnapshotLike],
    window: Window,
    *,
    as_of: datetime,
) -> WindowObservation:
    """Answer one window for one publication. Pure, and total.

    The rule, in order:

    1. **Discard anything captured after ``as_of``.** This is the look-ahead guard, and it is
       first for a reason: a dataset rebuilt tomorrow must not quietly improve because the
       collector kept working. See `as_of` in the dataset service.
    2. **Accept observations in ``[target, target + tolerance]``.** At or after the target, so
       the counter provably represents *at least* that much exposure — an observation at 23h
       cannot answer "what did 24h look like", however close it feels. Not beyond the
       tolerance, so "nearest available" can never reach across days to manufacture an answer.
    3. **Prefer the earliest that actually measured the video.** Earliest keeps the answer
       closest to the question; preferring a real measurement means one `not_returned` blip at
       24h05 does not discard a good observation at 24h30.
    4. **Otherwise say why.** Still inside the acceptance interval → ``not_mature``; past it →
       ``missing_snapshot``; snapshots present but none measuring → ``video_not_returned``.

    Nothing is interpolated, nothing is carried forward from an earlier window, and no counter
    is invented. Absence is an answer.
    """
    published_at = _as_utc(published_at)
    as_of = _as_utc(as_of)

    visible = [
        snapshot for snapshot in snapshots
        if _as_utc(snapshot.captured_at) <= as_of
    ]
    lower = published_at + window.target
    upper = lower + window.tolerance

    in_window = sorted(
        (s for s in visible if lower <= _as_utc(s.captured_at) <= upper),
        key=lambda s: _as_utc(s.captured_at),
    )

    if not in_window:
        # The interval is still open, so an observation may yet arrive: too early to call this
        # missing. Judged against `as_of` and never against the wall clock, or the same
        # dataset would answer differently every time it was rebuilt.
        state = NOT_MATURE if as_of < upper else MISSING_SNAPSHOT
        return WindowObservation(
            window=window.name,
            availability=state,
            target_age_seconds=window.target_seconds,
            tolerance_seconds=window.tolerance_seconds,
        )

    measured = [s for s in in_window if s.availability == _SNAPSHOT_OK]
    chosen = measured[0] if measured else in_window[0]
    observed_at = _as_utc(chosen.captured_at)

    if not measured:
        # The video was asked about and the provider declined to return it. Counters stay
        # NULL: this is emphatically not "zero views", and writing a zero here would put a
        # cliff into every chart drawn from the dataset.
        return WindowObservation(
            window=window.name,
            availability=VIDEO_NOT_RETURNED,
            target_age_seconds=window.target_seconds,
            tolerance_seconds=window.tolerance_seconds,
            snapshot_id=str(chosen.id),
            observed_at=observed_at,
            actual_age_seconds=int((observed_at - published_at).total_seconds()),
        )

    return WindowObservation(
        window=window.name,
        availability=AVAILABLE,
        target_age_seconds=window.target_seconds,
        tolerance_seconds=window.tolerance_seconds,
        snapshot_id=str(chosen.id),
        observed_at=observed_at,
        actual_age_seconds=int((observed_at - published_at).total_seconds()),
        # Counters as observed. A NULL survives as NULL — the owner hiding likes is not the
        # audience declining to like.
        view_count=chosen.view_count,
        like_count=chosen.like_count,
        comment_count=chosen.comment_count,
    )


def resolve_all(
    published_at: datetime,
    snapshots: Sequence[SnapshotLike],
    *,
    as_of: datetime,
    windows: Iterable[Window] = WINDOWS,
) -> dict[str, WindowObservation]:
    """Every canonical window for one publication, in one pass over its snapshots."""
    return {
        window.name: resolve_window(published_at, snapshots, window, as_of=as_of)
        for window in windows
    }


def policy_description() -> list[dict[str, object]]:
    """The window contract, as data — so an export can carry the rule it was built under."""
    return [
        {
            "window": window.name,
            "target_age_seconds": window.target_seconds,
            "tolerance_seconds": window.tolerance_seconds,
            "rule": (
                f"earliest measuring snapshot with age in "
                f"[{_hours(window.target)}, {_hours(window.target + window.tolerance)}]"
            ),
        }
        for window in windows_in_order()
    ]


def windows_in_order() -> tuple[Window, ...]:
    return tuple(sorted(WINDOWS, key=lambda window: window.target))


# --------------------------------------------------------------------- helpers


def _hours(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 3600:g}h"


def _as_utc(value: datetime) -> datetime:
    """Every temporal comparison here is UTC-aware.

    A naive datetime compared against an aware one raises, and a naive one *assumed* to be
    local would silently shift every window by the container's timezone offset — which is the
    class of bug the publication budget already had once.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
