"""The selection engine: eligibility, pre-rank, semantic evaluation, composition, policy.

Operates on ``CandidateView`` — a plain snapshot of a candidate — rather than on ORM rows, so
the same code runs against the database and against evaluation fixtures. If the harness
scored candidates through a different path, it would be measuring a different engine.

The two-stage shape exists for cost. Semantic evaluation is a model call per candidate, so
sending every discovered video would mean 100+ calls per run to select 3. Instead a cheap
deterministic pre-rank narrows the field to ``semantic_top_k``, and only those are evaluated:

    100 discovered -> eligibility -> 60 eligible -> pre-rank -> top 20 -> semantic -> top 3
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.selection import features
from app.selection.policy import (
    SCORE_VERSION,
    Composition,
    Eligibility,
    PolicyState,
    SelectionConfig,
    apply_policy,
    commit_to_state,
    compose,
    evaluate_eligibility,
    explain,
)
from app.selection.semantic import (
    OK,
    UNAVAILABLE,
    CandidateBrief,
    NullSemanticEvaluator,
    SemanticResult,
)


@dataclass(frozen=True)
class CandidateView:
    """A candidate, flattened. Everything the engine is allowed to look at."""

    candidate_id: str
    status: str
    title: str | None = None
    description: str | None = None
    channel: str | None = None
    channel_id: str | None = None
    url: str | None = None
    source_id: str | None = None
    source_config: dict[str, Any] = field(default_factory=dict)
    published_at: datetime | None = None
    duration_sec: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    live_status: str | None = None
    available: bool | None = True
    discovery_query: str | None = None

    @property
    def channel_key(self) -> str | None:
        """Prefer the stable channel id; fall back to the display name.

        A channel can rename itself, and two channels can share a display name. The id is
        what makes a per-channel cap mean anything.
        """
        return self.channel_id or self.channel


@dataclass(frozen=True)
class TopicView:
    topic_id: str
    name: str
    description: str | None = None
    keywords: list[str] = field(default_factory=list)


@dataclass
class CandidateAssessment:
    """Everything decided about one candidate, and why."""

    candidate: CandidateView
    eligibility: Eligibility
    signals: dict[str, features.Signal] = field(default_factory=dict)
    semantic: SemanticResult | None = None
    composition: Composition | None = None
    prerank_score: float = 0.0
    rank: int | None = None
    decision: str = "not_evaluated"
    reasons: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)

    @property
    def final_score(self) -> float:
        return self.composition.final_score if self.composition else 0.0

    @property
    def semantic_used(self) -> bool:
        return self.semantic is not None and self.semantic.ok

    def breakdown(self) -> dict[str, Any]:
        """The persisted ``scores_json`` payload.

        Versioned, because a score is only comparable to another score from the same formula.
        Without the stamp a 0.82 from today and a 0.82 from three formula revisions ago look
        identical and are not.
        """
        payload: dict[str, Any] = {
            "version": SCORE_VERSION,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "final_score": self.final_score,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "eligibility": self.eligibility.as_dict(),
            "signals": {name: signal.as_dict() for name, signal in self.signals.items()},
        }
        if self.composition is not None:
            payload["composition"] = {
                "components": self.composition.components,
                "weights_used": self.composition.weights_used,
                "unmeasurable": self.composition.missing,
            }
        payload["semantic"] = (
            self.semantic.as_dict() if self.semantic else {"status": "not_attempted"}
        )
        if self.blocked_by:
            payload["blocked_by"] = list(self.blocked_by)
        return payload

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate.candidate_id,
            "title": self.candidate.title,
            "channel": self.candidate.channel,
            "eligible": self.eligibility.eligible,
            "rank": self.rank,
            "score": self.final_score,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "blocked_by": list(self.blocked_by),
            "semantic_status": self.semantic.status if self.semantic else "not_attempted",
        }


@dataclass
class SelectionOutcome:
    """The result of one run over one topic."""

    topic_id: str
    score_version: str = SCORE_VERSION
    considered: int = 0
    eligible: int = 0
    ineligible: int = 0
    semantic_evaluated: int = 0
    semantic_failures: int = 0
    selected: list[CandidateAssessment] = field(default_factory=list)
    blocked: list[CandidateAssessment] = field(default_factory=list)
    ineligible_items: list[CandidateAssessment] = field(default_factory=list)
    ranked: list[CandidateAssessment] = field(default_factory=list)
    duration_ms: int = 0
    semantic_provider: str | None = None

    def as_dict(self, *, verbose: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "topic_id": self.topic_id,
            "score_version": self.score_version,
            "considered": self.considered,
            "eligible": self.eligible,
            "ineligible": self.ineligible,
            "semantic_evaluated": self.semantic_evaluated,
            "semantic_failures": self.semantic_failures,
            "semantic_provider": self.semantic_provider,
            "duration_ms": self.duration_ms,
            "selected": [item.as_dict() for item in self.selected],
            "blocked": [item.as_dict() for item in self.blocked],
        }
        if verbose:
            payload["ranked"] = [item.as_dict() for item in self.ranked]
            payload["ineligible_items"] = [item.as_dict() for item in self.ineligible_items]
        return payload


class SelectionEngine:
    """Ranks candidates for a topic and applies the selection policy."""

    def __init__(self, evaluator=None, config: SelectionConfig | None = None) -> None:
        self.evaluator = evaluator or NullSemanticEvaluator()
        self.config = config or SelectionConfig()

    def run(
        self,
        *,
        topic: TopicView,
        candidates: list[CandidateView],
        config: SelectionConfig | None = None,
        now: datetime | None = None,
        already_selected_today: int = 0,
        channel_last_selected: dict[str, datetime] | None = None,
    ) -> SelectionOutcome:
        config = config or self.config
        now = now or datetime.now(timezone.utc)
        started = time.monotonic()

        outcome = SelectionOutcome(topic_id=topic.topic_id)
        outcome.semantic_provider = getattr(self.evaluator, "name", None)
        outcome.considered = len(candidates)

        # --- 1. eligibility -------------------------------------------------
        eligible: list[CandidateAssessment] = []
        for candidate in candidates:
            eligibility = evaluate_eligibility(
                status=candidate.status,
                available=candidate.available,
                live_status=candidate.live_status,
                duration_sec=candidate.duration_sec,
                published_at=candidate.published_at,
                now=now,
                config=config,
            )
            assessment = CandidateAssessment(candidate=candidate, eligibility=eligibility)
            if not eligibility.eligible:
                assessment.decision = "ineligible"
                assessment.reasons = list(eligibility.reasons)
                outcome.ineligible_items.append(assessment)
                continue
            eligible.append(assessment)

        outcome.eligible = len(eligible)
        outcome.ineligible = len(outcome.ineligible_items)

        # --- 2. deterministic signals + pre-rank ----------------------------
        for assessment in eligible:
            self._deterministic_signals(assessment, topic=topic, now=now, config=config)
            assessment.prerank_score = self._prerank(assessment, config)

        eligible.sort(key=lambda item: (-item.prerank_score, item.candidate.candidate_id))

        # --- 3. semantic evaluation, on the top K only ----------------------
        top_k = max(0, config.semantic_top_k)
        for assessment in eligible[:top_k]:
            result = self._evaluate_semantically(assessment, topic)
            assessment.semantic = result
            if result.ok:
                outcome.semantic_evaluated += 1
            elif result.status != UNAVAILABLE:
                outcome.semantic_failures += 1

        # --- 4. final composition -------------------------------------------
        for assessment in eligible:
            self._compose(assessment, config)

        eligible.sort(key=lambda item: (-item.final_score, item.candidate.candidate_id))
        for index, assessment in enumerate(eligible, start=1):
            assessment.rank = index
        outcome.ranked = eligible

        # --- 5. policy -------------------------------------------------------
        state = PolicyState(
            selected_today=already_selected_today,
            channel_last_selected=dict(channel_last_selected or {}),
        )
        for assessment in eligible:
            blocked = apply_policy(
                channel=assessment.candidate.channel_key,
                source_id=assessment.candidate.source_id,
                score=assessment.final_score,
                relevance=assessment.signals["effective_relevance"].value,
                semantic_used=assessment.semantic_used,
                state=state,
                config=config,
                now=now,
            )
            if blocked:
                assessment.decision = "blocked"
                assessment.blocked_by = blocked
                outcome.blocked.append(assessment)
                continue

            assessment.decision = "selected"
            commit_to_state(
                channel=assessment.candidate.channel_key,
                source_id=assessment.candidate.source_id,
                state=state,
                now=now,
            )
            outcome.selected.append(assessment)

        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        return outcome

    # ------------------------------------------------------------------ steps

    def _deterministic_signals(
        self,
        assessment: CandidateAssessment,
        *,
        topic: TopicView,
        now: datetime,
        config: SelectionConfig,
    ) -> None:
        candidate = assessment.candidate
        assessment.signals["freshness"] = features.freshness(
            candidate.published_at, now=now, half_life_hours=config.freshness_half_life_hours
        )
        assessment.signals["velocity"] = features.observed_average_view_velocity(
            candidate.view_count, candidate.published_at, now=now
        )
        assessment.signals["audience"] = features.audience_size(candidate.view_count)
        assessment.signals["engagement"] = features.engagement_rate(
            candidate.like_count, candidate.comment_count, candidate.view_count
        )
        assessment.signals["deterministic_relevance"] = features.deterministic_relevance(
            topic_name=topic.name,
            topic_keywords=topic.keywords,
            title=candidate.title,
            description=candidate.description,
            discovery_query=candidate.discovery_query,
        )
        assessment.signals["source"] = features.source_priority(candidate.source_config)

    def _prerank(self, assessment: CandidateAssessment, config: SelectionConfig) -> float:
        """Cheap ordering used only to pick who gets a model call.

        Same signals as the final composition minus the semantic ones, so the pre-rank is a
        genuine approximation of the final ranking rather than a different opinion.
        """
        trend = self._trend(assessment)
        composition = compose(
            relevance=assessment.signals["deterministic_relevance"].value,
            trend=trend,
            freshness=assessment.signals["freshness"].value,
            interest=None,
            source=assessment.signals["source"].value,
            config=config,
        )
        return composition.final_score

    def _evaluate_semantically(
        self, assessment: CandidateAssessment, topic: TopicView
    ) -> SemanticResult:
        """One model call. Never allowed to take down the run.

        A provider that is down, slow or returning nonsense degrades this candidate to its
        deterministic signals — it does not abort the topic's selection.
        """
        candidate = assessment.candidate
        brief = CandidateBrief(
            title=candidate.title,
            description=candidate.description,
            channel=candidate.channel,
            published_at=candidate.published_at.isoformat() if candidate.published_at else None,
            discovery_query=candidate.discovery_query,
        )
        try:
            return self.evaluator.evaluate(
                topic_name=topic.name,
                topic_description=topic.description,
                topic_keywords=list(topic.keywords or []),
                brief=brief,
            )
        except Exception as exc:  # noqa: BLE001 — an unclassified evaluator bug
            # The message is not carried: an adapter that interpolates its request into an
            # exception would put the API key into a stored breakdown.
            return SemanticResult(status="failed", error=type(exc).__name__)

    def _trend(self, assessment: CandidateAssessment) -> float | None:
        """Momentum: observed velocity plus how strongly the audience reacted.

        Freshness is deliberately NOT folded in here — it is its own weighted term, and
        including it in both would count recency twice. Trend answers "is this catching on?",
        which is a different question from "is this new?".

        Velocity needs both a count and an age; audience size is the fallback when only the
        count exists. When neither is available, trend is unmeasurable and its weight is
        renormalised away rather than scored as zero.
        """
        velocity = assessment.signals["velocity"]
        engagement = assessment.signals["engagement"]
        audience = assessment.signals["audience"]

        primary = velocity.value if velocity.measurable else (
            audience.value if audience.measurable else None
        )
        if primary is None:
            return None
        if not engagement.measurable:
            return primary
        return round(0.7 * primary + 0.3 * engagement.value, 4)

    def _compose(self, assessment: CandidateAssessment, config: SelectionConfig) -> None:
        deterministic = assessment.signals["deterministic_relevance"]
        semantic = assessment.semantic

        # Semantic relevance, when trustworthy, blends with the deterministic baseline rather
        # than replacing it: keyword evidence is weak but real, and a model that has only seen
        # a title should not be able to overrule it outright. Blending is weighted by the
        # model's own stated confidence.
        if semantic is not None and semantic.ok and semantic.verdict is not None:
            confidence = semantic.verdict.confidence
            if deterministic.measurable:
                effective_relevance = (
                    confidence * semantic.verdict.relevance
                    + (1 - confidence) * deterministic.value
                )
            else:
                effective_relevance = semantic.verdict.relevance
            interest = semantic.verdict.editorial_interest
        else:
            effective_relevance = deterministic.value
            interest = None

        assessment.composition = compose(
            relevance=effective_relevance,
            trend=self._trend(assessment),
            freshness=assessment.signals["freshness"].value,
            interest=interest,
            source=assessment.signals["source"].value,
            config=config,
        )
        assessment.signals["effective_relevance"] = features.Signal(
            effective_relevance,
            {
                "deterministic": deterministic.value,
                "semantic": semantic.verdict.relevance if (semantic and semantic.ok and semantic.verdict) else None,
                "semantic_status": semantic.status if semantic else "not_attempted",
            },
        )
        assessment.reasons = explain(
            freshness=assessment.signals["freshness"].value,
            trend=self._trend(assessment),
            relevance=effective_relevance,
            interest=interest,
            engagement=assessment.signals["engagement"].value,
            semantic_used=assessment.semantic_used,
        )


def recency_baseline(candidates: list[CandidateView], *, limit: int) -> list[CandidateView]:
    """What discovery already offered: newest first, nothing else considered.

    The comparison point for the evaluation harness. Not a straw man — it is exactly what the
    system did before this PR.
    """
    dated = [c for c in candidates if c.published_at is not None]
    dated.sort(key=lambda c: (c.published_at, c.candidate_id), reverse=True)
    return dated[:limit]
