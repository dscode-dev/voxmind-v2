"""Deterministic signals derived from candidate metadata.

Every function here is pure: same inputs, same output, no clock reads except the ``now`` that
is passed in, no I/O. That is what makes a ranking reproducible and a regression bisectable.

Two rules run through all of it.

**Unknown is not zero.** Discovery stores ``None`` for anything a source did not publish, and
that distinction has to survive into scoring. An RSS feed publishes no view count; treating
that as zero views would rank every RSS candidate below every YouTube one for a reason that
has nothing to do with the content. Signals that cannot be computed return ``None`` and are
excluded from the composition, which then renormalises over what it does have.

**Raw counts are not comparable.** A video with 1,000,000 views over five years and one with
100,000 views in three hours are not ranked by the first number. Counts are age-normalised
into an observed rate, then log-compressed, because social metrics are heavily skewed and a
single outlier would otherwise flatten every other candidate to nothing.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Above this, a value is treated as "as good as it gets" for the log scale. A candidate at
# 5,000 views/hour and one at 50,000 are both simply viral; the difference stops carrying
# ranking information and starts crowding everything else out.
VELOCITY_SATURATION_PER_HOUR = 5_000.0

# Same idea for absolute counts, used only when age is unknown and a rate cannot be formed.
VIEW_SATURATION = 1_000_000.0


@dataclass(frozen=True)
class Signal:
    """One measurement, with the raw inputs that produced it.

    ``value is None`` means *not measurable*, which is different from a measured zero. Both
    are legitimate; conflating them is the bug this type exists to prevent.
    """

    value: float | None
    detail: dict[str, Any]

    @property
    def measurable(self) -> bool:
        return self.value is not None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"score": _round(self.value)}
        payload.update(self.detail)
        return payload


def unmeasurable(reason: str, **detail: Any) -> Signal:
    return Signal(None, {"status": "unmeasurable", "reason": reason, **detail})


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def freshness(published_at: datetime | None, *, now: datetime, half_life_hours: float = 24.0) -> Signal:
    """Exponential decay on age, not a cliff.

    A step function ("under 24h good, over 24h bad") makes a 23-hour-old video beat a
    25-hour-old one by the entire weight of the signal, and makes a 25-hour-old video
    indistinguishable from a two-year-old one. Both are wrong. Decay keeps the ordering
    smooth and keeps very old content genuinely far away:

        0h -> 1.00    12h -> 0.71    24h -> 0.50    72h -> 0.13    1 week -> 0.007

    Something published in the future (a scheduled premiere, or clock skew) is treated as
    brand new rather than given a score above 1.
    """
    if published_at is None:
        return unmeasurable("no_published_at")

    moment = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
    age_hours = (now - moment).total_seconds() / 3600.0
    if age_hours < 0:
        age_hours = 0.0

    score = 0.5 ** (age_hours / max(half_life_hours, 0.1))
    return Signal(
        _clamp(score),
        {"age_hours": round(age_hours, 2), "half_life_hours": half_life_hours},
    )


# ---------------------------------------------------------------------------
# Engagement
# ---------------------------------------------------------------------------


def observed_average_view_velocity(
    view_count: int | None,
    published_at: datetime | None,
    *,
    now: datetime,
) -> Signal:
    """Views per hour *averaged over the video's whole life*.

    The name is deliberate. This is not current velocity and it is not acceleration: there is
    no time series here, only one cumulative count and one timestamp. A video that got all its
    views on day one and none since reports the same number as one growing steadily. Calling
    it "trending velocity" would claim knowledge the data does not contain — proper velocity
    needs engagement snapshots over time, which is listed as remaining debt.

    Compressed with log1p against a saturation point so one enormous outlier does not flatten
    the field.
    """
    if view_count is None:
        return unmeasurable("no_view_count")
    if published_at is None:
        return unmeasurable("no_published_at")

    moment = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
    age_hours = max((now - moment).total_seconds() / 3600.0, 0.0)
    # Under an hour old, the denominator is unstable: 100 views in 90 seconds would read as
    # 4,000/hour. One hour is the floor, so very new videos are not flattered by arithmetic.
    effective_hours = max(age_hours, 1.0)
    per_hour = view_count / effective_hours

    score = math.log1p(per_hour) / math.log1p(VELOCITY_SATURATION_PER_HOUR)
    return Signal(
        _clamp(score),
        {
            "view_count": view_count,
            "age_hours": round(age_hours, 2),
            "observed_average_views_per_hour": round(per_hour, 1),
            "saturation_per_hour": VELOCITY_SATURATION_PER_HOUR,
        },
    )


def audience_size(view_count: int | None) -> Signal:
    """Absolute reach, log-compressed. Only useful when age is unknown."""
    if view_count is None:
        return unmeasurable("no_view_count")
    score = math.log1p(max(view_count, 0)) / math.log1p(VIEW_SATURATION)
    return Signal(_clamp(score), {"view_count": view_count})


def engagement_rate(
    like_count: int | None,
    comment_count: int | None,
    view_count: int | None,
) -> Signal:
    """How strongly the audience reacted, relative to how many saw it.

    Rates rather than counts, so a small channel with a highly engaged audience is not
    automatically beaten by a large one. Typical YouTube like rates sit around 2-5%, so the
    scale is normalised against 10% rather than 100% — otherwise every real video would score
    near zero and the signal would carry no information.
    """
    if view_count is None or view_count <= 0:
        return unmeasurable("no_view_count")
    if like_count is None and comment_count is None:
        return unmeasurable("no_reaction_counts")

    detail: dict[str, Any] = {"view_count": view_count}
    parts: list[float] = []

    if like_count is not None:
        like_rate = like_count / view_count
        detail["like_rate"] = round(like_rate, 5)
        parts.append(min(like_rate / 0.10, 1.0))
    if comment_count is not None:
        comment_rate = comment_count / view_count
        detail["comment_rate"] = round(comment_rate, 5)
        # Comments are perhaps 10x rarer than likes, so they get their own scale.
        parts.append(min(comment_rate / 0.01, 1.0))

    return Signal(_clamp(sum(parts) / len(parts)), detail)


# ---------------------------------------------------------------------------
# Deterministic topic relevance
# ---------------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)
# Portuguese and English function words. They appear in every text, so counting them as
# topical overlap would make every candidate look relevant to every topic.
_STOPWORDS = frozenset(
    """
    a o e de da do das dos em no na nos nas um uma uns umas para por com que se ao aos as os
    the of and to in on for at is are was were be been by from as it this that with
    """.split()
)


def normalize_text(value: str | None) -> str:
    """Casefold and strip accents so "polêmica" and "polemica" are the same token."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(value))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return stripped.casefold()


def tokenize(value: str | None) -> set[str]:
    return {
        token
        for token in _WORD.findall(normalize_text(value))
        if len(token) > 2 and token not in _STOPWORDS
    }


def deterministic_relevance(
    *,
    topic_name: str | None,
    topic_keywords: list[str] | None,
    title: str | None,
    description: str | None,
    discovery_query: str | None = None,
) -> Signal:
    """Keyword overlap between the topic and the candidate's text.

    A cheap, explainable baseline — and the fallback when no semantic evaluator is available.
    It is not trying to understand the video; it is asking whether the words the topic is
    about appear where they would if the video were on-topic.

    The title is weighted above the description because a description is often boilerplate
    (channel links, sponsor text) that mentions everything the channel ever covers.

    Deliberately not a list of football regexes: the terms come from the ContentTopic, so the
    engine works for any subject.
    """
    terms = {normalize_text(term) for term in (topic_keywords or []) if str(term or "").strip()}
    topic_tokens = tokenize(topic_name)
    if not terms and not topic_tokens:
        return unmeasurable("topic_has_no_keywords")

    title_tokens = tokenize(title)
    description_tokens = tokenize(description)
    haystack_title = normalize_text(title)
    haystack_description = normalize_text(description)

    matched_terms: list[str] = []
    title_hits = 0
    description_hits = 0

    for term in sorted(terms):
        # A multi-word keyword ("futebol entrevista") is matched as a phrase first, then by
        # its tokens, so a partial match still counts for something.
        term_tokens = tokenize(term)
        if term and term in haystack_title:
            title_hits += 1
            matched_terms.append(term)
        elif term_tokens and term_tokens <= title_tokens:
            title_hits += 1
            matched_terms.append(term)
        elif term and term in haystack_description:
            description_hits += 1
            matched_terms.append(term)
        elif term_tokens and term_tokens <= description_tokens:
            description_hits += 1
            matched_terms.append(term)

    # Saturating, NOT a fraction of the keyword list.
    #
    # Dividing by len(terms) treats keywords as a conjunction: a topic listing six terms
    # would need a video to match most of them. But keywords are alternatives — "futebol" OR
    # "entrevista" OR "polemica" — and a video squarely about one of them is on topic. Real
    # feed data made this obvious: news items whose titles clearly matched "futebol" scored
    # 0.167 and were blocked by the relevance floor, purely because the topic happened to
    # list six keywords instead of two. Adding keywords made every candidate look less
    # relevant, which is exactly backwards.
    #
    # The first strong match carries most of the weight and further matches add less:
    #   1 title hit -> 0.40    2 -> 0.64    3 -> 0.78    4 -> 0.87
    total_terms = len(terms) or 1
    weighted_hits = title_hits * 1.0 + description_hits * 0.5
    coverage = 1.0 - (0.6 ** weighted_hits) if weighted_hits > 0 else 0.0

    # Loose token overlap with the topic's own name, as a weak secondary signal.
    name_overlap = 0.0
    if topic_tokens:
        name_overlap = len(topic_tokens & (title_tokens | description_tokens)) / len(topic_tokens)

    # The query that surfaced the candidate is evidence in itself: the source asked for this.
    query_bonus = 0.0
    if discovery_query:
        query_tokens = tokenize(discovery_query)
        if query_tokens and query_tokens & title_tokens:
            query_bonus = 0.15

    score = _clamp(0.7 * min(coverage, 1.0) + 0.3 * name_overlap + query_bonus)
    return Signal(
        score,
        {
            "matched_terms": matched_terms[:10],
            "title_hits": title_hits,
            "description_hits": description_hits,
            "topic_terms": total_terms,
            "name_overlap": round(name_overlap, 3),
            "query_bonus": query_bonus,
        },
    )


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


def source_priority(source_config: dict[str, Any] | None) -> Signal:
    """An operator-configured nudge, bounded on purpose.

    Read from ``DiscoverySource.config_json``, never from a list of channel names in the
    code. A hardcoded "ESPN is good" would be unmaintainable, invisible to the operator who
    has to live with it, and would quietly turn into a cartel of favoured channels that
    nothing else can outrank.

    Its weight in the composition is small for the same reason: a source preference should
    break ties, not decide the ranking.
    """
    config = dict(source_config or {})
    raw = config.get("priority")
    if raw is None:
        return unmeasurable("no_source_priority")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return unmeasurable("source_priority_not_numeric")
    return Signal(_clamp(value), {"configured_priority": value})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clamp(value: float) -> float:
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)
