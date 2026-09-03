"""Configuration, eligibility, score composition and selection policy.

Three separate questions, deliberately not one function:

1. **Eligibility** — may this candidate take part at all? A binary gate with a reason code.
   Never expressed as a very negative score: a score can be outweighed, a gate cannot, and
   "unavailable video" must not be something a high relevance can overcome.
2. **Ranking** — among those eligible, which looks best? Weighted composition of signals.
3. **Policy** — should we select it *now*? Caps, diversity and cooldown, applied to an
   already-ranked list.

Collapsing them produces a single opaque number that cannot answer "why was this rejected?".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# Bumped whenever a formula or weight changes. Scores from different versions are not
# comparable, and without this stamp a historical 0.82 looks like today's 0.82.
SCORE_VERSION = "selection-v1"

# ---------------------------------------------------------------------------
# Eligibility reason codes
# ---------------------------------------------------------------------------

UNAVAILABLE = "unavailable"
UPCOMING_LIVE = "upcoming_live"
CURRENTLY_LIVE = "currently_live"
OUTSIDE_FRESHNESS_WINDOW = "outside_freshness_window"
DURATION_TOO_SHORT = "duration_too_short"
DURATION_TOO_LONG = "duration_too_long"
ALREADY_SELECTED = "already_selected"
ALREADY_CONSUMED = "already_consumed"
PREVIOUSLY_REJECTED = "previously_rejected"
MISSING_REQUIRED_METADATA = "missing_required_metadata"

# Reasons that are a property of the *video* and will not change on their own. A candidate
# blocked by one of these is genuinely finished.
PERMANENT_REASONS = frozenset({UNAVAILABLE, DURATION_TOO_SHORT, DURATION_TOO_LONG})

# ---------------------------------------------------------------------------
# Policy reason codes (post-ranking)
# ---------------------------------------------------------------------------

BELOW_MINIMUM_SCORE = "below_minimum_score"
INSUFFICIENT_TOPIC_RELEVANCE = "insufficient_topic_relevance"
CHANNEL_CAP_REACHED = "channel_cap_reached"
SOURCE_CAP_REACHED = "source_cap_reached"
RUN_LIMIT_REACHED = "run_limit_reached"
DAILY_CAP_REACHED = "daily_cap_reached"
CHANNEL_COOLDOWN = "channel_cooldown"

# ---------------------------------------------------------------------------
# Positive explanations
# ---------------------------------------------------------------------------

FRESH_CONTENT = "fresh_content"
HIGH_OBSERVED_VELOCITY = "high_observed_velocity"
STRONG_TOPIC_RELEVANCE = "strong_topic_relevance"
SEMANTIC_INTEREST = "semantic_interest"
STRONG_ENGAGEMENT = "strong_engagement"
DETERMINISTIC_ONLY = "deterministic_only"


@dataclass(frozen=True)
class SelectionConfig:
    """Everything tunable, in one object.

    Defaults live here rather than in a dozen environment variables, and a topic overrides
    them through ``ContentTopic.metadata_json["selection"]`` — policy belongs next to the
    editorial intention it serves, not in the deployment.

    The weights are **V1 heuristics**, chosen against the evaluation fixtures and rounded to
    two decimals. Anything more precise would be false precision: there are no human labels
    to fit against, so 0.35 is a defensible starting point and 0.3472 would be a fabrication.
    """

    # --- eligibility ---
    freshness_hours: float = 72.0
    min_duration_sec: int = 60
    max_duration_sec: int = 14_400  # 4h; beyond this a source is a stream archive
    # Freshness decay half-life. Shorter than the eligibility window on purpose: a 48h-old
    # video can still compete, it just starts well behind a 4h-old one.
    freshness_half_life_hours: float = 24.0

    # --- cost control ---
    # Deterministic pre-ranking narrows the field before anything is sent to a model.
    semantic_top_k: int = 20

    # --- composition weights (need not sum to 1; they are renormalised) ---
    weight_relevance: float = 0.35
    weight_trend: float = 0.30
    weight_freshness: float = 0.20
    weight_interest: float = 0.15
    weight_source: float = 0.05

    # --- policy ---
    minimum_score: float = 0.45
    # A floor on topic relevance, checked separately from the composed score.
    #
    # Composition is linear, so a video with zero relevance can still reach a respectable
    # total on freshness and virality alone — measured on the fixtures, an entirely off-topic
    # viral video scored 0.5724 and outranked an on-topic one. Being popular is not a reason
    # to publish something the channel is not about, so relevance gets a gate of its own
    # rather than a larger weight, which would only have shifted the same problem.
    minimum_relevance: float = 0.25
    # Raised when the semantic leg could not run: with less evidence, the bar for acting
    # automatically goes up rather than the engine pretending it knows as much.
    minimum_score_without_semantic: float = 0.55
    max_selected_per_run: int = 3
    max_per_channel: int = 1
    max_per_source: int = 5
    max_selections_per_day: int = 12
    channel_cooldown_hours: float = 24.0

    def with_overrides(self, overrides: dict[str, Any] | None) -> "SelectionConfig":
        """Apply a topic's overrides, ignoring anything unrecognised or malformed.

        A typo in configuration must not silently reshape the ranking, and it must not crash
        a run either. Unknown keys are dropped; bad values fall back to the default.
        """
        if not overrides:
            return self
        fields = {f.name: getattr(self, f.name) for f in self.__dataclass_fields__.values()}
        for key, value in overrides.items():
            if key not in fields or value is None:
                continue
            current = fields[key]
            try:
                fields[key] = type(current)(value)
            except (TypeError, ValueError):
                continue
        return SelectionConfig(**fields)

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    permanent: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": list(self.reasons), "permanent": self.permanent}


def evaluate_eligibility(
    *,
    status: str,
    available: bool | None,
    live_status: str | None,
    duration_sec: int | None,
    published_at: datetime | None,
    now: datetime,
    config: SelectionConfig,
) -> Eligibility:
    """The gate. Reason codes, never a penalty score.

    ``permanent`` separates "this video will never be usable" from "not right now". Only the
    former justifies REJECTED; the latter leaves the candidate where it is so a later run can
    reconsider it. Marking a candidate rejected because today's freshness window excluded it
    would destroy tomorrow's chance to pick it up.
    """
    reasons: list[str] = []

    if status == "selected":
        reasons.append(ALREADY_SELECTED)
    if status == "consumed":
        reasons.append(ALREADY_CONSUMED)
    if status == "rejected":
        reasons.append(PREVIOUSLY_REJECTED)

    if available is False:
        reasons.append(UNAVAILABLE)

    normalized_live = (live_status or "").strip().lower()
    if normalized_live == "upcoming":
        reasons.append(UPCOMING_LIVE)
    elif normalized_live == "live":
        reasons.append(CURRENTLY_LIVE)

    if duration_sec is not None:
        if duration_sec < config.min_duration_sec:
            reasons.append(DURATION_TOO_SHORT)
        elif duration_sec > config.max_duration_sec:
            reasons.append(DURATION_TOO_LONG)
    # A missing duration is NOT a rejection: RSS sources do not publish one, and requiring it
    # would exclude an entire provider for a reason unrelated to the content.

    if published_at is None:
        reasons.append(MISSING_REQUIRED_METADATA)
    else:
        moment = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
        age_hours = (now - moment).total_seconds() / 3600.0
        if age_hours > config.freshness_hours:
            reasons.append(OUTSIDE_FRESHNESS_WINDOW)

    permanent = any(reason in PERMANENT_REASONS for reason in reasons)
    return Eligibility(eligible=not reasons, reasons=reasons, permanent=permanent)


# ---------------------------------------------------------------------------
# Score composition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Composition:
    final_score: float
    components: dict[str, Any]
    weights_used: dict[str, float]
    missing: list[str]


def compose(
    *,
    relevance: float | None,
    trend: float | None,
    freshness: float | None,
    interest: float | None,
    source: float | None,
    config: SelectionConfig,
) -> Composition:
    """Weighted mean over the signals that could actually be measured.

    Renormalisation is the point. If a candidate has no view counts, treating trend as 0 and
    dividing by the full weight would drag its final score down for missing data rather than
    for being a worse video — and would systematically bury every RSS candidate beneath every
    YouTube one. Instead the weight of an unmeasurable signal is removed from the denominator,
    so the candidate is judged on the evidence that exists.

    That is fair, not free: a candidate with less evidence has to clear a higher bar to be
    selected automatically (``minimum_score_without_semantic``), and the missing signals are
    listed in the breakdown.
    """
    weighted = [
        ("relevance", relevance, config.weight_relevance),
        ("trend", trend, config.weight_trend),
        ("freshness", freshness, config.weight_freshness),
        ("interest", interest, config.weight_interest),
        ("source", source, config.weight_source),
    ]

    total_weight = 0.0
    accumulated = 0.0
    components: dict[str, Any] = {}
    weights_used: dict[str, float] = {}
    missing: list[str] = []

    for name, value, weight in weighted:
        if value is None or weight <= 0:
            components[name] = None
            if value is None and weight > 0:
                missing.append(name)
            continue
        components[name] = round(float(value), 4)
        weights_used[name] = weight
        accumulated += float(value) * weight
        total_weight += weight

    if total_weight <= 0:
        return Composition(0.0, components, {}, missing)

    final = max(0.0, min(1.0, accumulated / total_weight))
    return Composition(round(final, 4), components, weights_used, missing)


def explain(
    *,
    freshness: float | None,
    trend: float | None,
    relevance: float | None,
    interest: float | None,
    engagement: float | None,
    semantic_used: bool,
) -> list[str]:
    """Machine-readable reasons a candidate ranked where it did.

    Codes, not prose. An operator filters on these and a future policy can act on them; an
    LLM sentence can do neither.
    """
    reasons: list[str] = []
    if freshness is not None and freshness >= 0.6:
        reasons.append(FRESH_CONTENT)
    if trend is not None and trend >= 0.6:
        reasons.append(HIGH_OBSERVED_VELOCITY)
    if relevance is not None and relevance >= 0.6:
        reasons.append(STRONG_TOPIC_RELEVANCE)
    if interest is not None and interest >= 0.6:
        reasons.append(SEMANTIC_INTEREST)
    if engagement is not None and engagement >= 0.6:
        reasons.append(STRONG_ENGAGEMENT)
    if not semantic_used:
        reasons.append(DETERMINISTIC_ONLY)
    return reasons


# ---------------------------------------------------------------------------
# Selection policy
# ---------------------------------------------------------------------------


@dataclass
class PolicyState:
    """What the policy has committed to so far, within one run and recent history."""

    selected_channels: dict[str, int] = field(default_factory=dict)
    selected_sources: dict[str, int] = field(default_factory=dict)
    selected_in_run: int = 0
    selected_today: int = 0
    channel_last_selected: dict[str, datetime] = field(default_factory=dict)


def apply_policy(
    *,
    channel: str | None,
    source_id: str | None,
    score: float,
    relevance: float | None,
    semantic_used: bool,
    state: PolicyState,
    config: SelectionConfig,
    now: datetime,
) -> list[str]:
    """Reasons this ranked candidate must not be selected right now. Empty means select.

    Every reason here is *temporary* by nature — a cap resets, a cooldown expires, a better
    day comes. None of them mark a candidate rejected.
    """
    blocked: list[str] = []

    threshold = (
        config.minimum_score if semantic_used else config.minimum_score_without_semantic
    )
    if score < threshold:
        blocked.append(BELOW_MINIMUM_SCORE)

    # An unmeasurable relevance (a topic with no keywords) does not block: that is a
    # configuration gap, not evidence the candidate is off-topic.
    if relevance is not None and relevance < config.minimum_relevance:
        blocked.append(INSUFFICIENT_TOPIC_RELEVANCE)

    if state.selected_in_run >= config.max_selected_per_run:
        blocked.append(RUN_LIMIT_REACHED)

    if state.selected_today >= config.max_selections_per_day:
        blocked.append(DAILY_CAP_REACHED)

    if channel:
        # Diversity: four videos from one channel is a repetitive feed even when all four
        # rank highest. A simple per-channel cap, not MMR.
        if state.selected_channels.get(channel, 0) >= config.max_per_channel:
            blocked.append(CHANNEL_CAP_REACHED)
        last = state.channel_last_selected.get(channel)
        if last is not None:
            elapsed = (now - last).total_seconds() / 3600.0
            if elapsed < config.channel_cooldown_hours:
                blocked.append(CHANNEL_COOLDOWN)

    if source_id and state.selected_sources.get(source_id, 0) >= config.max_per_source:
        blocked.append(SOURCE_CAP_REACHED)

    return blocked


def commit_to_state(
    *,
    channel: str | None,
    source_id: str | None,
    state: PolicyState,
    now: datetime,
) -> None:
    state.selected_in_run += 1
    state.selected_today += 1
    if channel:
        state.selected_channels[channel] = state.selected_channels.get(channel, 0) + 1
        state.channel_last_selected[channel] = now
    if source_id:
        state.selected_sources[source_id] = state.selected_sources.get(source_id, 0) + 1
