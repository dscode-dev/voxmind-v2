"""A controlled dataset for the selection engine.

Every case is **synthetic and labelled as such**. The metadata shapes are modelled on what
PR-DISCOVERY-01's real smoke returned (YouTube search results with counts, RSS items without
them), but the numbers are constructed so each case isolates one behaviour.

What this dataset does **not** contain is editorial ground truth. Nobody has labelled these
videos as "should have been selected", so the harness reports behaviour — ordering, coverage,
diversity, eligibility — and never accuracy. Inventing labels to compute a precision figure
would produce a number that measures agreement with my own guesses.

``expectation`` on each case is a *characterisation*: what the engine must do for reasons that
can be argued from the case itself ("an unavailable video is never selected"), not what a
human editor would have chosen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.selection.engine import CandidateView, TopicView

# Fixed clock: freshness is a function of age, so a moving "now" would make every run
# different and the dataset unreproducible.
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

TOPIC = TopicView(
    topic_id="topic-futebol",
    name="Futebol brasileiro",
    description="Noticias, entrevistas e polemicas do futebol brasileiro",
    keywords=["futebol", "entrevista", "coletiva", "polemica", "arbitragem"],
)


def ago(**kwargs) -> datetime:
    return NOW - timedelta(**kwargs)


@dataclass
class SelectionCase:
    case_id: str
    description: str
    candidate: CandidateView
    expectation: str
    source_type: str = "synthetic"


def _candidate(case_id: str, **overrides: Any) -> CandidateView:
    fields: dict[str, Any] = {
        "candidate_id": case_id,
        "status": "discovered",
        "title": "Video de futebol",
        "description": "Cobertura do futebol brasileiro",
        "channel": "Canal Esportivo",
        "channel_id": "UC_default",
        "source_id": "src-youtube",
        "source_config": {},
        "published_at": ago(hours=6),
        "duration_sec": 900,
        "view_count": None,
        "like_count": None,
        "comment_count": None,
        "live_status": "none",
        "available": True,
        "discovery_query": "futebol entrevista",
    }
    fields.update(overrides)
    return CandidateView(**fields)


CASES: list[SelectionCase] = [
    # ---------------------------------------------------------------- ranking
    SelectionCase(
        case_id="fresh_high_engagement",
        description="Published 3h ago, climbing fast, squarely on topic.",
        expectation="ranks at or near the top",
        candidate=_candidate(
            "fresh_high_engagement",
            title="Entrevista completa: tecnico rebate criticas da arbitragem",
            description="O tecnico falou sobre a polemica da arbitragem na coletiva.",
            published_at=ago(hours=3),
            view_count=90_000,
            like_count=6_200,
            comment_count=1_400,
            channel_id="UC_espn",
            channel="Canal A",
        ),
    ),
    SelectionCase(
        case_id="fresh_low_engagement",
        description="Equally fresh and on topic, but almost nobody is watching.",
        expectation="ranks below the fresh high-engagement case",
        candidate=_candidate(
            "fresh_low_engagement",
            title="Entrevista com o preparador fisico sobre a semana de treinos",
            description="Bastidores do treino de futebol.",
            published_at=ago(hours=3),
            view_count=140,
            like_count=4,
            comment_count=0,
            channel_id="UC_small",
            channel="Canal B",
        ),
    ),
    SelectionCase(
        case_id="old_viral",
        description="1.8M views — accumulated over two years.",
        expectation="ineligible on freshness; raw view count must not rescue it",
        candidate=_candidate(
            "old_viral",
            title="A maior polemica da arbitragem no futebol brasileiro",
            description="Retrospectiva da polemica.",
            published_at=ago(days=730),
            view_count=1_800_000,
            like_count=95_000,
            comment_count=22_000,
            channel_id="UC_big",
            channel="Canal Gigante",
        ),
    ),
    SelectionCase(
        case_id="new_low_initial_views",
        description="Published 40 minutes ago; too early for counts to mean anything.",
        expectation="not buried for having few views yet",
        candidate=_candidate(
            "new_low_initial_views",
            title="Coletiva ao vivo: tecnico anuncia saida apos polemica",
            description="Coletiva completa de futebol.",
            published_at=ago(minutes=40),
            view_count=320,
            like_count=45,
            comment_count=12,
            channel_id="UC_news",
            channel="Canal C",
        ),
    ),

    # ---------------------------------------------------------------- missing data
    SelectionCase(
        case_id="rss_no_metrics",
        description="An RSS item: on topic and fresh, but the feed publishes no counts.",
        expectation="scores on the signals it has; not zeroed for missing metrics",
        candidate=_candidate(
            "rss_no_metrics",
            title="Polemica da arbitragem domina o futebol brasileiro",
            description="Analise da polemica da arbitragem.",
            published_at=ago(hours=5),
            view_count=None,
            like_count=None,
            comment_count=None,
            duration_sec=None,
            source_id="src-rss",
            channel_id="UC_portal",
            channel="Portal Noticias",
        ),
    ),

    # ---------------------------------------------------------------- relevance
    SelectionCase(
        case_id="viral_off_topic",
        description="Enormous velocity, nothing to do with the topic.",
        expectation="must not outrank on-topic candidates on virality alone",
        candidate=_candidate(
            "viral_off_topic",
            title="Receita de bolo de chocolate em 10 minutos",
            description="Aprenda a fazer um bolo caseiro.",
            published_at=ago(hours=2),
            view_count=400_000,
            like_count=38_000,
            comment_count=9_000,
            channel_id="UC_food",
            channel="Canal Culinaria",
            discovery_query=None,
        ),
    ),

    # ---------------------------------------------------------------- eligibility
    SelectionCase(
        case_id="unavailable_but_hot",
        description="Would rank top, but the video is private or deleted.",
        expectation="never selected, at any score",
        candidate=_candidate(
            "unavailable_but_hot",
            title="Entrevista exclusiva: polemica da arbitragem",
            published_at=ago(hours=1),
            view_count=200_000,
            like_count=18_000,
            comment_count=5_000,
            available=False,
            channel_id="UC_gone",
            channel="Canal D",
        ),
    ),
    SelectionCase(
        case_id="upcoming_premiere",
        description="Scheduled, not yet broadcast.",
        expectation="ineligible: there is nothing to cut yet",
        candidate=_candidate(
            "upcoming_premiere",
            title="AO VIVO: coletiva de imprensa apos o jogo",
            published_at=ago(hours=1),
            live_status="upcoming",
            channel_id="UC_live",
            channel="Canal E",
        ),
    ),
    SelectionCase(
        case_id="currently_live",
        description="Streaming right now.",
        expectation="ineligible: PR-ASR-01 explicitly does not process live streams",
        candidate=_candidate(
            "currently_live",
            title="AO VIVO agora: debate sobre a polemica",
            published_at=ago(hours=2),
            live_status="live",
            channel_id="UC_live2",
            channel="Canal F",
        ),
    ),
    SelectionCase(
        case_id="too_short",
        description="A 35-second Short.",
        expectation="ineligible: nothing to recut",
        candidate=_candidate(
            "too_short",
            title="Golaco na polemica do futebol",
            published_at=ago(hours=2),
            duration_sec=35,
            view_count=50_000,
            channel_id="UC_shorts",
            channel="Canal G",
        ),
    ),
    SelectionCase(
        case_id="long_interview",
        description="A 52-minute interview.",
        expectation="eligible: long form is what the cutter is for",
        candidate=_candidate(
            "long_interview",
            title="Entrevista completa de 1 hora sobre a polemica no futebol",
            published_at=ago(hours=8),
            duration_sec=3_120,
            view_count=25_000,
            like_count=1_800,
            comment_count=400,
            channel_id="UC_pod",
            channel="Canal H",
        ),
    ),
    SelectionCase(
        case_id="stream_archive",
        description="A six-hour stream archive.",
        expectation="ineligible: beyond the usable source length",
        candidate=_candidate(
            "stream_archive",
            title="Live completa: futebol e polemica por 6 horas",
            published_at=ago(hours=10),
            duration_sec=21_600,
            view_count=12_000,
            channel_id="UC_stream",
            channel="Canal I",
        ),
    ),
    SelectionCase(
        case_id="no_published_at",
        description="A feed item with no usable date.",
        expectation="ineligible: freshness cannot be established",
        candidate=_candidate(
            "no_published_at",
            title="Entrevista sobre a polemica do futebol",
            published_at=None,
            source_id="src-rss",
            channel_id="UC_undated",
            channel="Canal J",
        ),
    ),

    # ---------------------------------------------------------------- history
    SelectionCase(
        case_id="already_selected",
        description="Already chosen by a previous run.",
        expectation="never re-selected",
        candidate=_candidate(
            "already_selected",
            title="Entrevista com polemica da arbitragem no futebol",
            status="selected",
            published_at=ago(hours=4),
            view_count=80_000,
            channel_id="UC_prev",
            channel="Canal K",
        ),
    ),
    SelectionCase(
        case_id="already_consumed",
        description="Already produced.",
        expectation="never re-selected",
        candidate=_candidate(
            "already_consumed",
            title="Coletiva sobre a polemica no futebol brasileiro",
            status="consumed",
            published_at=ago(hours=5),
            view_count=60_000,
            channel_id="UC_done",
            channel="Canal L",
        ),
    ),
    SelectionCase(
        case_id="previously_rejected",
        description="Rejected earlier.",
        expectation="a rediscovery does not resurrect it",
        candidate=_candidate(
            "previously_rejected",
            title="Entrevista polemica sobre arbitragem",
            status="rejected",
            published_at=ago(hours=6),
            channel_id="UC_rej",
            channel="Canal M",
        ),
    ),

    # ---------------------------------------------------------------- diversity
    SelectionCase(
        case_id="same_channel_1",
        description="Strong candidate from a channel that publishes constantly.",
        expectation="one of these is selected, not all",
        candidate=_candidate(
            "same_channel_1",
            title="Entrevista 1: polemica da arbitragem no futebol",
            published_at=ago(hours=2),
            view_count=70_000,
            like_count=5_000,
            comment_count=900,
            channel_id="UC_prolific",
            channel="Canal Prolifico",
        ),
    ),
    SelectionCase(
        case_id="same_channel_2",
        description="Second strong candidate from the same channel.",
        expectation="blocked by the channel cap, not rejected",
        candidate=_candidate(
            "same_channel_2",
            title="Entrevista 2: coletiva sobre a polemica no futebol",
            published_at=ago(hours=3),
            view_count=65_000,
            like_count=4_600,
            comment_count=850,
            channel_id="UC_prolific",
            channel="Canal Prolifico",
        ),
    ),
    SelectionCase(
        case_id="same_channel_3",
        description="Third strong candidate from the same channel.",
        expectation="blocked by the channel cap, not rejected",
        candidate=_candidate(
            "same_channel_3",
            title="Entrevista 3: analise da polemica no futebol",
            published_at=ago(hours=4),
            view_count=61_000,
            like_count=4_200,
            comment_count=800,
            channel_id="UC_prolific",
            channel="Canal Prolifico",
        ),
    ),

    # ---------------------------------------------------------------- cross-source
    SelectionCase(
        case_id="cross_source_rss",
        description="A topic surfaced by RSS, and separately by YouTube search.",
        expectation="both remain distinct candidates; near-duplicate detection is out of scope",
        candidate=_candidate(
            "cross_source_rss",
            title="Coletiva do tecnico gera polemica no futebol",
            description="Reportagem sobre a coletiva.",
            published_at=ago(hours=7),
            source_id="src-rss",
            channel_id="UC_portal2",
            channel="Portal Esporte",
        ),
    ),
    SelectionCase(
        case_id="cross_source_youtube",
        description="The same story, found on YouTube.",
        expectation="ranked on its own merits",
        candidate=_candidate(
            "cross_source_youtube",
            title="Coletiva do tecnico gera polemica no futebol brasileiro",
            published_at=ago(hours=7),
            view_count=30_000,
            like_count=2_100,
            comment_count=380,
            channel_id="UC_yt2",
            channel="Canal Esporte TV",
        ),
    ),

    # ---------------------------------------------------------------- source config
    SelectionCase(
        case_id="prioritised_source",
        description="A source an operator marked as high priority in its config.",
        expectation="a small nudge only; the preference must not decide the ranking",
        candidate=_candidate(
            "prioritised_source",
            title="Entrevista sobre polemica no futebol",
            published_at=ago(hours=9),
            view_count=8_000,
            like_count=300,
            comment_count=50,
            source_id="src-priority",
            source_config={"priority": 1.0},
            channel_id="UC_prio",
            channel="Canal N",
        ),
    ),
]


def load_cases() -> list[SelectionCase]:
    return list(CASES)


def load_candidates() -> list[CandidateView]:
    return [case.candidate for case in CASES]


def case_by_id(case_id: str) -> SelectionCase:
    return next(case for case in CASES if case.case_id == case_id)
