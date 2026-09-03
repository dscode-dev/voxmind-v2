"""The selection engine: signals, eligibility, composition, ranking and policy.

Pure — no database, no network, no model. Everything here runs against a fixed clock so the
same inputs always produce the same ranking.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.selection import features
from app.selection.engine import (
    CandidateView,
    SelectionEngine,
    TopicView,
    recency_baseline,
)
from app.selection.policy import (
    BELOW_MINIMUM_SCORE,
    CHANNEL_CAP_REACHED,
    CHANNEL_COOLDOWN,
    CURRENTLY_LIVE,
    DURATION_TOO_LONG,
    DURATION_TOO_SHORT,
    INSUFFICIENT_TOPIC_RELEVANCE,
    OUTSIDE_FRESHNESS_WINDOW,
    RUN_LIMIT_REACHED,
    SCORE_VERSION,
    UNAVAILABLE,
    UPCOMING_LIVE,
    SelectionConfig,
    compose,
    evaluate_eligibility,
)
from app.selection.semantic import (
    OK,
    UNAVAILABLE as SEMANTIC_UNAVAILABLE,
    CandidateBrief,
    SemanticResult,
    SemanticVerdict,
)
from evaluation.selection.fixtures import NOW, TOPIC, load_candidates

CONFIG = SelectionConfig()


def ago(**kwargs) -> datetime:
    return NOW - timedelta(**kwargs)


def candidate(candidate_id="c1", **overrides) -> CandidateView:
    fields = {
        "candidate_id": candidate_id,
        "status": "discovered",
        "title": "Entrevista sobre a polemica do futebol",
        "description": "Coletiva de futebol",
        "channel": "Canal",
        "channel_id": "UC_1",
        "source_id": "src-1",
        "published_at": ago(hours=4),
        "duration_sec": 900,
        "live_status": "none",
        "available": True,
    }
    fields.update(overrides)
    return CandidateView(**fields)


def rank(candidates, *, config=None, evaluator=None, **kwargs):
    engine = SelectionEngine(evaluator=evaluator, config=config or CONFIG)
    return engine.run(topic=TOPIC, candidates=candidates, config=config or CONFIG, now=NOW, **kwargs)


# ==========================================================================
# Freshness
# ==========================================================================


def test_freshness_decays_rather_than_stepping():
    """A 23h video must not beat a 25h one by the whole weight of the signal."""
    scores = [
        features.freshness(ago(hours=hours), now=NOW).value
        for hours in (0, 12, 24, 48, 72, 168)
    ]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(1.0, abs=0.01)
    assert scores[2] == pytest.approx(0.5, abs=0.01), "24h is one half-life"
    assert scores[5] < 0.02, "a week old is effectively cold"


def test_freshness_has_no_cliff():
    just_under = features.freshness(ago(hours=23.9), now=NOW).value
    just_over = features.freshness(ago(hours=24.1), now=NOW).value
    assert abs(just_under - just_over) < 0.01


def test_a_future_publish_date_is_treated_as_brand_new():
    """Scheduled premieres and clock skew must not score above 1."""
    signal = features.freshness(NOW + timedelta(hours=5), now=NOW)
    assert signal.value == pytest.approx(1.0)


def test_freshness_without_a_date_is_unmeasurable_not_zero():
    signal = features.freshness(None, now=NOW)
    assert signal.value is None
    assert signal.measurable is False


# ==========================================================================
# Velocity and engagement
# ==========================================================================


def test_recent_momentum_beats_old_accumulation():
    """1M views over five years is not 100k views in three hours."""
    old_viral = features.observed_average_view_velocity(1_000_000, ago(days=1825), now=NOW)
    fresh = features.observed_average_view_velocity(100_000, ago(hours=3), now=NOW)
    assert fresh.value > old_viral.value


def test_velocity_is_log_compressed():
    """One enormous outlier must not flatten every other candidate to zero."""
    big = features.observed_average_view_velocity(10_000_000, ago(hours=2), now=NOW).value
    modest = features.observed_average_view_velocity(20_000, ago(hours=2), now=NOW).value
    assert big <= 1.0
    assert modest > 0.5, "a solidly performing video is not crushed by a viral one"


def test_a_very_new_video_is_not_flattered_by_arithmetic():
    """100 views in 90 seconds is not 4,000 views/hour of momentum."""
    signal = features.observed_average_view_velocity(100, ago(minutes=1.5), now=NOW)
    assert signal.detail["observed_average_views_per_hour"] <= 100.0


def test_velocity_is_named_for_what_it_measures():
    signal = features.observed_average_view_velocity(1000, ago(hours=10), now=NOW)
    assert "observed_average_views_per_hour" in signal.detail
    # There is no time series here, so nothing may claim acceleration or current velocity.
    assert not any("accel" in key for key in signal.detail)


def test_velocity_needs_both_a_count_and_an_age():
    assert features.observed_average_view_velocity(None, ago(hours=1), now=NOW).value is None
    assert features.observed_average_view_velocity(1000, None, now=NOW).value is None


def test_engagement_uses_rates_not_counts():
    """A small channel with a devoted audience is not automatically beaten by a large one."""
    small = features.engagement_rate(500, 100, 5_000).value
    large = features.engagement_rate(1_000, 100, 500_000).value
    assert small > large


def test_engagement_without_views_is_unmeasurable():
    assert features.engagement_rate(100, 10, None).value is None
    assert features.engagement_rate(100, 10, 0).value is None


def test_engagement_with_only_likes_still_measures():
    assert features.engagement_rate(500, None, 10_000).measurable


def test_no_reaction_counts_is_unmeasurable_not_zero():
    signal = features.engagement_rate(None, None, 10_000)
    assert signal.value is None
    assert signal.detail["reason"] == "no_reaction_counts"


# ==========================================================================
# Deterministic relevance
# ==========================================================================


def test_relevance_matches_topic_keywords_in_the_title():
    signal = features.deterministic_relevance(
        topic_name="Futebol brasileiro",
        topic_keywords=["futebol", "entrevista", "polemica"],
        title="Entrevista polemica sobre o futebol",
        description=None,
    )
    assert signal.value > 0.65
    assert signal.detail["title_hits"] == 3


def test_adding_keywords_to_a_topic_does_not_make_candidates_less_relevant():
    """Keywords are alternatives, not a checklist.

    Coverage used to be hits/len(keywords), so listing six terms instead of two made every
    candidate score three times lower — and real feed items matching "futebol" clearly were
    blocked by the relevance floor for it. Adding vocabulary to a topic must not punish the
    videos that match part of it.
    """
    narrow = features.deterministic_relevance(
        topic_name="Futebol", topic_keywords=["futebol"],
        title="Senado aprova diretrizes para o futebol", description=None,
    ).value
    wide = features.deterministic_relevance(
        topic_name="Futebol",
        topic_keywords=["futebol", "entrevista", "coletiva", "polemica", "arbitragem", "tecnico"],
        title="Senado aprova diretrizes para o futebol", description=None,
    ).value

    assert wide == pytest.approx(narrow, abs=0.001)


def test_one_strong_match_clears_the_relevance_floor():
    """A video plainly about the topic must not be gated out for matching only one term."""
    signal = features.deterministic_relevance(
        topic_name="Futebol brasileiro",
        topic_keywords=["futebol", "entrevista", "coletiva", "polemica", "arbitragem"],
        title="Corinthians faz a maior venda de sua historia no futebol",
        description=None,
    )
    assert signal.value > SelectionConfig().minimum_relevance


def test_more_matches_still_rank_higher():
    def score(title):
        return features.deterministic_relevance(
            topic_name="Futebol brasileiro",
            topic_keywords=["futebol", "entrevista", "coletiva", "polemica"],
            title=title, description=None,
        ).value

    assert score("Entrevista sobre futebol") > score("Noticias do futebol")
    assert score("Entrevista e coletiva sobre a polemica do futebol") > score(
        "Entrevista sobre futebol"
    )


def test_relevance_is_accent_and_case_insensitive():
    with_accents = features.deterministic_relevance(
        topic_name="Futebol", topic_keywords=["polêmica"],
        title="POLEMICA no jogo", description=None,
    )
    assert with_accents.value > 0


def test_an_off_topic_video_scores_near_zero():
    signal = features.deterministic_relevance(
        topic_name="Futebol brasileiro",
        topic_keywords=["futebol", "entrevista", "arbitragem"],
        title="Receita de bolo de chocolate",
        description="Aprenda a fazer um bolo.",
    )
    assert signal.value < 0.1


def test_the_title_counts_for_more_than_the_description():
    """Descriptions are often boilerplate listing everything a channel covers."""
    in_title = features.deterministic_relevance(
        topic_name="X", topic_keywords=["arbitragem"],
        title="Polemica da arbitragem", description="texto",
    ).value
    in_description = features.deterministic_relevance(
        topic_name="X", topic_keywords=["arbitragem"],
        title="Video do dia", description="Polemica da arbitragem",
    ).value
    assert in_title > in_description


def test_stopwords_do_not_create_false_relevance():
    signal = features.deterministic_relevance(
        topic_name="O futebol de que todos falam",
        topic_keywords=[],
        title="A receita do bolo que a gente faz",
        description=None,
    )
    assert signal.value < 0.2


def test_a_topic_with_no_keywords_is_unmeasurable():
    signal = features.deterministic_relevance(
        topic_name=None, topic_keywords=[], title="qualquer coisa", description=None
    )
    assert signal.value is None


# ==========================================================================
# Source priority
# ==========================================================================


def test_source_priority_comes_from_configuration():
    assert features.source_priority({"priority": 0.8}).value == pytest.approx(0.8)


def test_an_unconfigured_source_is_unmeasurable_not_penalised():
    assert features.source_priority({}).value is None
    assert features.source_priority(None).value is None


def test_a_malformed_priority_is_ignored_not_crashed():
    assert features.source_priority({"priority": "muito bom"}).value is None


# ==========================================================================
# Composition — missing data policy
# ==========================================================================


def test_an_unmeasurable_signal_is_renormalised_away_not_scored_zero():
    """The RSS problem: no view counts must not mean a low score.

    Treating trend as 0 while keeping its weight in the denominator would bury every RSS
    candidate beneath every YouTube one for reasons unrelated to the content.
    """
    with_trend = compose(
        relevance=0.8, trend=0.8, freshness=0.8, interest=None, source=None, config=CONFIG
    )
    without_trend = compose(
        relevance=0.8, trend=None, freshness=0.8, interest=None, source=None, config=CONFIG
    )
    assert without_trend.final_score == pytest.approx(with_trend.final_score, abs=0.001)
    assert "trend" in without_trend.missing
    assert "trend" not in without_trend.weights_used


def test_a_measured_zero_is_not_the_same_as_missing():
    measured = compose(
        relevance=0.8, trend=0.0, freshness=0.8, interest=None, source=None, config=CONFIG
    )
    missing = compose(
        relevance=0.8, trend=None, freshness=0.8, interest=None, source=None, config=CONFIG
    )
    assert measured.final_score < missing.final_score


def test_composition_stays_within_bounds():
    result = compose(
        relevance=1.0, trend=1.0, freshness=1.0, interest=1.0, source=1.0, config=CONFIG
    )
    assert result.final_score == pytest.approx(1.0)


def test_nothing_measurable_yields_zero_not_a_crash():
    result = compose(
        relevance=None, trend=None, freshness=None, interest=None, source=None, config=CONFIG
    )
    assert result.final_score == 0.0


# ==========================================================================
# Eligibility
# ==========================================================================


def eligibility(**overrides):
    fields = {
        "status": "discovered",
        "available": True,
        "live_status": "none",
        "duration_sec": 900,
        "published_at": ago(hours=4),
        "now": NOW,
        "config": CONFIG,
    }
    fields.update(overrides)
    return evaluate_eligibility(**fields)


def test_a_normal_candidate_is_eligible():
    assert eligibility().eligible


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"available": False}, UNAVAILABLE),
        ({"live_status": "upcoming"}, UPCOMING_LIVE),
        ({"live_status": "live"}, CURRENTLY_LIVE),
        ({"duration_sec": 30}, DURATION_TOO_SHORT),
        ({"duration_sec": 30_000}, DURATION_TOO_LONG),
        ({"published_at": ago(days=30)}, OUTSIDE_FRESHNESS_WINDOW),
        ({"status": "selected"}, "already_selected"),
        ({"status": "consumed"}, "already_consumed"),
        ({"status": "rejected"}, "previously_rejected"),
        ({"published_at": None}, "missing_required_metadata"),
    ],
)
def test_each_ineligibility_has_its_own_reason_code(overrides, reason):
    result = eligibility(**overrides)
    assert not result.eligible
    assert reason in result.reasons


def test_ineligibility_is_a_gate_not_a_penalty_score():
    """A score can be outweighed; a gate cannot. That is the point of the separation."""
    result = eligibility(available=False)
    assert result.eligible is False
    assert isinstance(result.reasons, list) and result.reasons


def test_permanent_and_temporary_ineligibility_are_distinguished():
    assert eligibility(available=False).permanent is True
    assert eligibility(duration_sec=30).permanent is True
    # These change on their own; rejecting on them would burn a candidate a later run wants.
    assert eligibility(published_at=ago(days=30)).permanent is False
    assert eligibility(live_status="upcoming").permanent is False


def test_a_missing_duration_does_not_make_a_candidate_ineligible():
    """RSS publishes no duration; requiring it would exclude an entire provider."""
    assert eligibility(duration_sec=None).eligible


# ==========================================================================
# Characterisation cases (PR-SELECTION-01 §54)
# ==========================================================================


def test_case_a_relevant_and_recent_beats_old_and_viral():
    fresh = candidate("fresh", title="Entrevista polemica sobre futebol",
                      published_at=ago(hours=3), view_count=50_000, like_count=3_000,
                      comment_count=500)
    old_viral = candidate("old", title="Entrevista polemica sobre futebol",
                          channel_id="UC_2", published_at=ago(days=400),
                          view_count=5_000_000, like_count=200_000, comment_count=50_000)

    outcome = rank([fresh, old_viral])

    assert [item.candidate.candidate_id for item in outcome.selected] == ["fresh"]
    assert "old" in {item.candidate.candidate_id for item in outcome.ineligible_items}


def test_case_b_an_unavailable_video_is_never_selected():
    hot_but_gone = candidate("gone", published_at=ago(minutes=30), view_count=500_000,
                             like_count=40_000, comment_count=9_000, available=False)

    outcome = rank([hot_but_gone])

    assert outcome.selected == []
    assert outcome.ineligible_items[0].eligibility.reasons == [UNAVAILABLE]


def test_case_c_an_irrelevant_video_does_not_win_on_virality():
    on_topic = candidate("on_topic", title="Entrevista sobre a polemica do futebol",
                         published_at=ago(hours=6), view_count=5_000)
    viral_junk = candidate("junk", title="Receita de bolo de chocolate",
                           description="Como fazer um bolo", channel_id="UC_food",
                           published_at=ago(hours=1), view_count=900_000,
                           like_count=80_000, comment_count=20_000)

    outcome = rank([on_topic, viral_junk])

    selected = [item.candidate.candidate_id for item in outcome.selected]
    assert "junk" not in selected
    blocked = {item.candidate.candidate_id: item.blocked_by for item in outcome.blocked}
    assert INSUFFICIENT_TOPIC_RELEVANCE in blocked["junk"]


def test_case_d_missing_metrics_do_not_zero_an_rss_candidate():
    rss = candidate("rss", title="Polemica da arbitragem no futebol",
                    published_at=ago(hours=4), view_count=None, like_count=None,
                    comment_count=None, duration_sec=None, source_id="src-rss")

    outcome = rank([rss])

    assert [item.candidate.candidate_id for item in outcome.selected] == ["rss"]
    assert outcome.ranked[0].final_score > 0.5
    assert "trend" in outcome.ranked[0].composition.missing


def test_case_e_channel_diversity_prevents_a_monoculture():
    same_channel = [
        candidate(f"c{index}", title="Entrevista polemica sobre futebol e arbitragem",
                  channel_id="UC_prolific", published_at=ago(hours=index + 1),
                  view_count=80_000 - index * 1_000, like_count=6_000, comment_count=900)
        for index in range(4)
    ]

    outcome = rank(same_channel)

    assert len(outcome.selected) == 1, "one per channel, not four"
    assert all(
        CHANNEL_CAP_REACHED in item.blocked_by or CHANNEL_COOLDOWN in item.blocked_by
        for item in outcome.blocked
    )


def test_case_f_already_selected_or_consumed_never_reappears():
    rows = [
        candidate("sel", status="selected"),
        candidate("con", status="consumed", channel_id="UC_2"),
    ]
    outcome = rank(rows)

    assert outcome.selected == []
    assert outcome.eligible == 0


def test_case_g_a_dead_semantic_provider_falls_back_to_deterministic():
    class Exploding:
        name = "exploding"

        def is_available(self):
            return True

        def evaluate(self, **_):
            raise RuntimeError("provider down: key=SECRET-VALUE")

    outcome = rank([candidate("c1", published_at=ago(hours=2), view_count=20_000)],
                   evaluator=Exploding())

    assert len(outcome.ranked) == 1, "the run survives"
    assert outcome.semantic_failures == 1
    assert outcome.ranked[0].final_score > 0
    # A provider that interpolates its request into an exception must not leak it.
    assert "SECRET-VALUE" not in str(outcome.ranked[0].breakdown())


def test_case_h_the_deterministic_path_is_reproducible():
    candidates = load_candidates()
    first = rank(candidates)
    second = rank(candidates)

    assert [item.candidate.candidate_id for item in first.ranked] == [
        item.candidate.candidate_id for item in second.ranked
    ]
    assert [item.final_score for item in first.ranked] == [
        item.final_score for item in second.ranked
    ]


# ==========================================================================
# Semantic integration
# ==========================================================================


class StubEvaluator:
    name = "stub"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def is_available(self):
        return True

    def evaluate(self, **_):
        self.calls += 1
        return self.result


def verdict(relevance=0.9, interest=0.8, confidence=0.9):
    return SemanticResult(
        status=OK,
        verdict=SemanticVerdict(
            relevance=relevance, editorial_interest=interest,
            confidence=confidence, reason="on topic",
        ),
        provider="stub", model="stub-1",
    )


def test_semantic_evaluation_only_runs_on_the_pre_ranked_top_k():
    """Cost control: 100 candidates must not mean 100 model calls to pick 3."""
    evaluator = StubEvaluator(verdict())
    candidates = [
        candidate(f"c{index}", channel_id=f"UC_{index}", published_at=ago(hours=index + 1))
        for index in range(30)
    ]
    config = SelectionConfig(semantic_top_k=5)

    rank(candidates, config=config, evaluator=evaluator)

    assert evaluator.calls == 5


def test_a_confident_semantic_verdict_moves_relevance():
    low_keyword_match = candidate("c1", title="O que aconteceu ontem",
                                  description="Analise do episodio")
    deterministic = rank([low_keyword_match]).ranked[0].final_score
    with_semantic = rank([low_keyword_match], evaluator=StubEvaluator(verdict())).ranked[0]

    assert with_semantic.final_score > deterministic
    assert with_semantic.semantic_used


def test_low_confidence_leans_on_the_deterministic_baseline():
    """A model that has only seen a title should not overrule keyword evidence outright."""
    row = candidate("c1", title="Entrevista polemica sobre futebol e arbitragem")
    confident = rank([row], evaluator=StubEvaluator(verdict(relevance=0.1, confidence=0.95)))
    unsure = rank([row], evaluator=StubEvaluator(verdict(relevance=0.1, confidence=0.05)))

    assert unsure.ranked[0].final_score > confident.ranked[0].final_score


def test_the_semantic_leg_cannot_rescue_an_ineligible_candidate():
    """§31: eligibility stays the authority whatever the model says."""
    outcome = rank(
        [candidate("gone", available=False)],
        evaluator=StubEvaluator(verdict(relevance=1.0, interest=1.0, confidence=1.0)),
    )
    assert outcome.selected == []


def test_the_semantic_leg_cannot_break_a_channel_cap():
    rows = [
        candidate(f"c{index}", channel_id="UC_same", published_at=ago(hours=index + 1))
        for index in range(3)
    ]
    outcome = rank(rows, evaluator=StubEvaluator(verdict(relevance=1.0, interest=1.0)))
    assert len(outcome.selected) == 1


def test_an_unavailable_provider_leaves_the_engine_deterministic():
    outcome = rank([candidate("c1")])
    assessment = outcome.ranked[0]

    assert assessment.semantic.status == SEMANTIC_UNAVAILABLE
    assert not assessment.semantic_used
    assert "deterministic_only" in assessment.reasons
    assert assessment.breakdown()["semantic"]["status"] == SEMANTIC_UNAVAILABLE


def test_no_semantic_score_is_invented_when_the_provider_is_absent():
    breakdown = rank([candidate("c1")]).ranked[0].breakdown()
    semantic = breakdown["semantic"]
    assert "relevance" not in semantic
    assert "editorial_interest" not in semantic


def test_the_fallback_bar_is_higher_than_the_semantic_one():
    """Less evidence means more caution, not the same confidence."""
    config = SelectionConfig()
    assert config.minimum_score_without_semantic > config.minimum_score


def test_an_out_of_range_verdict_is_rejected_not_clamped():
    """A model answering 4.7 for a 0-1 field has not understood the request."""
    with pytest.raises(Exception):
        SemanticVerdict(relevance=4.7, editorial_interest=0.5, confidence=0.5, reason="x")


def test_an_empty_reason_is_rejected():
    with pytest.raises(Exception):
        SemanticVerdict(relevance=0.5, editorial_interest=0.5, confidence=0.5, reason="")


def test_the_brief_never_carries_a_transcript_sized_payload():
    """Discovery has no transcript, and downloading one per candidate would cost more than
    producing the video."""
    brief = CandidateBrief(
        title="t", description="x" * 5000, channel="c",
        published_at=None, discovery_query=None,
    )
    assert len(brief.as_prompt_fields()["description"]) < 700


# ==========================================================================
# Policy
# ==========================================================================


def test_the_run_limit_is_respected():
    rows = [
        candidate(f"c{index}", channel_id=f"UC_{index}", published_at=ago(hours=index + 1),
                  view_count=50_000, like_count=4_000, comment_count=600)
        for index in range(10)
    ]
    outcome = rank(rows, config=SelectionConfig(max_selected_per_run=2))

    assert len(outcome.selected) == 2
    assert any(RUN_LIMIT_REACHED in item.blocked_by for item in outcome.blocked)


def test_a_low_score_is_blocked_not_rejected():
    weak = candidate("weak", title="Video generico", description="nada",
                     published_at=ago(hours=60))
    outcome = rank([weak])

    blocked_ids = {item.candidate.candidate_id for item in outcome.blocked}
    assert "weak" in blocked_ids
    # Blocking is temporary; nothing here marks the candidate permanently rejected.
    assert outcome.blocked[0].decision == "blocked"


def test_the_channel_cooldown_spans_runs_not_just_one_run():
    """A per-run cap alone lets three consecutive runs pick the same channel three times."""
    row = candidate("c1", channel_id="UC_hot", published_at=ago(hours=2), view_count=90_000)
    outcome = rank([row], channel_last_selected={"UC_hot": ago(hours=2)})

    assert outcome.selected == []
    assert CHANNEL_COOLDOWN in outcome.blocked[0].blocked_by


def test_the_daily_cap_stops_a_runaway_loop():
    rows = [candidate(f"c{index}", channel_id=f"UC_{index}") for index in range(3)]
    outcome = rank(rows, config=SelectionConfig(max_selections_per_day=5),
                   already_selected_today=5)

    assert outcome.selected == []
    assert all("daily_cap_reached" in item.blocked_by for item in outcome.blocked)


def test_blocked_and_ineligible_are_different_outcomes():
    rows = [candidate("gone", available=False), candidate("weak", title="x", description="y",
                                                          channel_id="UC_2")]
    outcome = rank(rows)

    assert {i.candidate.candidate_id for i in outcome.ineligible_items} == {"gone"}
    assert "weak" not in {i.candidate.candidate_id for i in outcome.ineligible_items}


# ==========================================================================
# Breakdown and versioning
# ==========================================================================


def test_the_breakdown_explains_the_decision():
    outcome = rank([candidate("c1", published_at=ago(hours=2), view_count=40_000,
                              like_count=3_000, comment_count=400)])
    breakdown = outcome.ranked[0].breakdown()

    assert breakdown["version"] == SCORE_VERSION
    assert "final_score" in breakdown
    assert set(breakdown["signals"]) >= {
        "freshness", "velocity", "engagement", "deterministic_relevance"
    }
    assert breakdown["composition"]["weights_used"]
    assert breakdown["eligibility"]["eligible"] is True


def test_the_breakdown_carries_the_raw_inputs_behind_each_signal():
    outcome = rank([candidate("c1", published_at=ago(hours=3), view_count=30_000)])
    signals = outcome.ranked[0].breakdown()["signals"]

    assert signals["freshness"]["age_hours"] == pytest.approx(3.0, abs=0.01)
    assert signals["velocity"]["view_count"] == 30_000
    assert "observed_average_views_per_hour" in signals["velocity"]


def test_an_unmeasurable_signal_says_so_in_the_breakdown():
    outcome = rank([candidate("c1", view_count=None)])
    signals = outcome.ranked[0].breakdown()["signals"]

    assert signals["velocity"]["score"] is None
    assert signals["velocity"]["status"] == "unmeasurable"


def test_scores_are_versioned():
    """A 0.82 from a different formula is not the same 0.82."""
    assert SCORE_VERSION == "selection-v1"
    assert rank([candidate("c1")]).score_version == SCORE_VERSION


# ==========================================================================
# Score spread
# ==========================================================================


def test_scores_spread_rather_than_clustering_at_one_value():
    outcome = rank(load_candidates())
    scores = [item.final_score for item in outcome.ranked]

    assert len(scores) > 5
    assert max(scores) - min(scores) > 0.2, "a ranking where everything scores alike ranks nothing"
    assert len({round(score, 2) for score in scores}) > 5


# ==========================================================================
# Configuration
# ==========================================================================


def test_a_topic_can_override_the_policy():
    config = SelectionConfig().with_overrides({"max_selected_per_run": 7, "minimum_score": 0.9})
    assert config.max_selected_per_run == 7
    assert config.minimum_score == 0.9


def test_an_unknown_config_key_is_ignored_not_applied():
    config = SelectionConfig().with_overrides({"max_selected_per_runn": 999})
    assert config.max_selected_per_run == SelectionConfig().max_selected_per_run


def test_a_malformed_config_value_falls_back_to_the_default():
    config = SelectionConfig().with_overrides({"minimum_score": "muito alto"})
    assert config.minimum_score == SelectionConfig().minimum_score


# ==========================================================================
# Baseline
# ==========================================================================


def test_the_recency_baseline_is_newest_first():
    rows = [
        candidate("old", published_at=ago(hours=40)),
        candidate("new", published_at=ago(hours=1)),
        candidate("mid", published_at=ago(hours=10)),
    ]
    assert [c.candidate_id for c in recency_baseline(rows, limit=3)] == ["new", "mid", "old"]


def test_the_recency_baseline_ignores_eligibility():
    """That is exactly what makes it a baseline worth comparing against."""
    rows = [candidate("gone", published_at=ago(hours=1), available=False)]
    assert [c.candidate_id for c in recency_baseline(rows, limit=3)] == ["gone"]
