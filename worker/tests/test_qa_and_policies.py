"""QA contract, short_serie chain policy, diarization honesty, topic neutrality (PR-CUT-01)."""

from pathlib import Path
from unittest import mock

import pytest

from app.pipeline.auto_review import AutoReviewPolicy
from app.video.qa import ClipQA


class FakeRendered:
    def __init__(self, name="final_clip_01.mp4", duration=40.0):
        self.name = name
        self.duration = duration

    def exists(self):
        return True


def transcript(count=30, duration=6.0, speakers=("SPEAKER_00", "SPEAKER_01")):
    return [
        {
            "start": i * duration,
            "end": i * duration + duration,
            "text": f"linha {i} sobre o jogo de ontem.",
            "speaker": speakers[i % len(speakers)],
        }
        for i in range(count)
    ]


def cut(start=0.0, end=42.0, **extra):
    return {"start": start, "end": end, "safe_start": start, "safe_end": end, **extra}


GOOD_POST = {
    "title": "O erro que custou o titulo",
    "hook": "ninguem no vestiario esperava aquela decisao do tecnico",
    "description": "Analise completa do momento decisivo da temporada.",
    "hashtags": ["#futebol", "#analise", "#voxmind"],
}


@pytest.fixture
def qa():
    return ClipQA(min_duration_sec=12, max_duration_sec=120, max_speakers_per_clip=3)


def evaluate(qa, cuts, post, transcript_segments, diarization_status="available", durations=None):
    rendered = [
        FakeRendered(f"final_clip_{i:02d}.mp4", (durations or [c["end"] - c["start"] for c in cuts])[i - 1])
        for i, c in enumerate(cuts, start=1)
    ]
    with mock.patch.object(ClipQA, "_probe_duration", lambda self, p: p.duration):
        return qa.evaluate(
            requested_cuts=cuts,
            rendered_files=rendered,
            transcript_segments=transcript_segments,
            post_metadata=post,
            diarization_status=diarization_status,
        )


# ==========================================================================
# Case F — QA reads metadata from the right structure
# ==========================================================================


def test_qa_reads_metadata_from_the_post_not_the_cut(qa):
    """The regression: QA read title/hook/description/hashtags off each cut, where the
    schema never puts them, charging every clip ~13 points for metadata that was present."""
    report = evaluate(qa, [cut(0.0, 42.0)], GOOD_POST, transcript(30))
    warnings = report["clips"][0]["warnings"]

    assert "missing_hook" not in warnings
    assert "missing_title" not in warnings
    assert "missing_description" not in warnings
    assert "sparse_hashtags" not in warnings


def test_absent_post_metadata_is_still_reported(qa):
    report = evaluate(qa, [cut(0.0, 42.0)], {}, transcript(30))
    warnings = report["clips"][0]["warnings"]

    assert "missing_hook" in warnings
    assert "missing_title" in warnings
    assert "missing_description" in warnings
    assert "sparse_hashtags" in warnings


def test_metadata_on_a_cut_is_not_mistaken_for_post_metadata(qa):
    report = evaluate(qa, [cut(0.0, 42.0, title="on the cut")], {}, transcript(30))
    assert "missing_title" in report["clips"][0]["warnings"]


def test_qa_declares_its_scope(qa):
    """source-cut QA, not final-output QA — the renderer changes speed and adds transitions."""
    report = evaluate(qa, [cut(0.0, 42.0)], GOOD_POST, transcript(30))
    assert report["qa_scope"] == "source_cut"


# ==========================================================================
# The three QA states are all reachable
# ==========================================================================


def test_auto_ready_is_reachable(qa):
    policy = AutoReviewPolicy(enabled=True, ready_score_threshold=85,
                              blocked_score_threshold=45, max_review_clips=1)
    segments = transcript(30)
    # A cut on exact segment boundaries with complete post metadata.
    report = evaluate(qa, [cut(0.0, 42.0)], GOOD_POST, segments)
    decision = policy.evaluate(
        qa_report=report,
        cuts=[cut(0.0, 42.0)],
        # Since PR-QA-01 the editorial layer alone cannot reach auto_ready: the rendered
        # artefact has to have passed its own technical gate as well.
        final_media_report={"status": "auto_ready", "reasons": [], "blocking_reasons": []},
    )

    assert report["clips"][0]["score"] >= 85
    assert decision["status"] == "auto_ready"
    assert decision["editorial_status"] == "auto_ready"


def test_needs_review_is_reachable(qa):
    policy = AutoReviewPolicy(enabled=True, ready_score_threshold=85,
                              blocked_score_threshold=45, max_review_clips=1)
    segments = transcript(30)
    # Cut boundaries fall inside segments, so structural warnings appear.
    report = evaluate(qa, [cut(3.0, 45.0)], {}, segments)
    decision = policy.evaluate(qa_report=report, cuts=[cut(3.0, 45.0)])

    assert decision["status"] == "needs_human_review"


def test_blocked_is_reachable(qa):
    policy = AutoReviewPolicy(enabled=True, ready_score_threshold=85,
                              blocked_score_threshold=45, max_review_clips=1)
    segments = transcript(30)
    report = evaluate(qa, [cut(0.0, 5.0)], GOOD_POST, segments)  # below the editorial minimum
    decision = policy.evaluate(qa_report=report, cuts=[cut(0.0, 5.0)])

    assert report["clips"][0]["decision"] == "blocked"
    assert decision["status"] == "blocked"


def test_qa_min_duration_follows_the_preset_not_a_separate_setting():
    """QA used its own 25s while short presets accept 12s, blocking valid cuts."""
    strict = ClipQA(min_duration_sec=25, max_duration_sec=120)
    aligned = ClipQA(min_duration_sec=12, max_duration_sec=120)
    segments = transcript(30)

    blocked = evaluate(strict, [cut(0.0, 18.0)], GOOD_POST, segments)
    allowed = evaluate(aligned, [cut(0.0, 18.0)], GOOD_POST, segments)

    assert blocked["clips"][0]["decision"] == "blocked"
    assert allowed["clips"][0]["decision"] != "blocked"


# ==========================================================================
# Degraded diarization
# ==========================================================================


def test_degraded_diarization_is_reported_as_unmeasurable(qa):
    segments = transcript(30, speakers=("UNKNOWN",))
    report = evaluate(qa, [cut(0.0, 42.0)], GOOD_POST, segments, diarization_status="degraded")

    clip = report["clips"][0]
    assert report["diarization_status"] == "degraded"
    assert clip["speaker_measurable"] is False
    assert "speaker_continuity_unmeasurable" in clip["warnings"]


def test_degraded_diarization_does_not_score_as_perfect(qa):
    segments = transcript(30, speakers=("UNKNOWN",))
    degraded = evaluate(qa, [cut(0.0, 42.0)], GOOD_POST, segments, diarization_status="degraded")
    available = evaluate(qa, [cut(0.0, 42.0)], GOOD_POST, transcript(30))

    assert degraded["clips"][0]["score"] < available["clips"][0]["score"]


def test_degraded_diarization_blocks_auto_ready():
    policy = AutoReviewPolicy(enabled=True, ready_score_threshold=85,
                              blocked_score_threshold=45, max_review_clips=1)
    qa_report = {
        "clips": [{"clip_index": 1, "decision": "approved", "score": 94,
                   "warnings": ["speaker_continuity_unmeasurable"], "issues": []}]
    }
    decision = policy.evaluate(qa_report=qa_report, cuts=[cut()])
    assert decision["status"] != "auto_ready"


# ==========================================================================
# Case G — short_serie chain policy
# ==========================================================================


@pytest.fixture
def chain_pipeline():
    """A Pipeline with IO stubbed, used only for its chain-selection method."""
    with mock.patch("app.pipeline.pipeline.MinioStorage"), \
         mock.patch("app.pipeline.pipeline.TelegramSender"), \
         mock.patch("app.pipeline.pipeline.ClipFlowApiClient"):
        from app.pipeline.pipeline import Pipeline

        return Pipeline(
            video_url="https://example/v",
            job_id="chain-test",
            clip_mode="short_serie",
            video_ratio="portrait",
            job_preset="short_series",
        )


def connected(start, end, reason):
    return {"start": start, "end": end, "safe_start": start, "safe_end": end, "reason": reason}


def test_a_disconnect_no_longer_discards_everything_after_it(chain_pipeline):
    """The regression: `break` on the first disconnect threw away every remaining cut."""
    cuts = [
        connected(0.0, 30.0, "tecnico vestiario decisao titulo"),
        connected(30.0, 60.0, "tecnico vestiario decisao titulo"),
        connected(300.0, 340.0, "arbitragem penalti polemica estadio"),
        connected(340.0, 380.0, "arbitragem penalti polemica estadio"),
        connected(380.0, 420.0, "arbitragem penalti polemica estadio"),
    ]
    selected = chain_pipeline._prune_disconnected_short_serie_cuts(cuts)

    # The longer, stronger second chain wins instead of being discarded.
    assert len(selected) == 3
    assert selected[0]["start"] == 300.0


def test_a_single_coherent_chain_is_returned_intact(chain_pipeline):
    cuts = [connected(i * 30.0, i * 30.0 + 30.0, "mesmo assunto tecnico vestiario") for i in range(4)]
    assert len(chain_pipeline._prune_disconnected_short_serie_cuts(cuts)) == 4


def test_chain_selection_is_deterministic(chain_pipeline):
    cuts = [
        connected(0.0, 30.0, "alpha beta gama delta"),
        connected(400.0, 430.0, "omega sigma lambda kappa"),
        connected(430.0, 465.0, "omega sigma lambda kappa"),
    ]
    first = chain_pipeline._prune_disconnected_short_serie_cuts(cuts)
    second = chain_pipeline._prune_disconnected_short_serie_cuts(cuts)
    assert [c["start"] for c in first] == [c["start"] for c in second]


def test_policy_only_applies_to_short_serie(chain_pipeline):
    chain_pipeline.clip_mode = "short"
    cuts = [connected(0.0, 30.0, "a b c"), connected(900.0, 930.0, "x y z")]
    assert len(chain_pipeline._prune_disconnected_short_serie_cuts(cuts)) == 2


# ==========================================================================
# §21 — legacy geopolitics signals are gone from the decision path
# ==========================================================================


LEGACY_TOKENS = [
    "blackrock", "deep state", "trump", "wall street",
    "o jogo por trás", "quem realmente manda", "o objetivo final",
    "o tamanho do poder", "dinheiro = poder",
]

DECISION_MODULES = [
    "app/pipeline/pipeline.py",
    "app/video/qa.py",
    "app/pipeline/soundtrack_selector.py",
    "app/pipeline/candidate_builder.py",
    "app/pipeline/hook_detector.py",
    "app/pipeline/story_shift_detector.py",
]


def _decision_literals(source: str):
    """String constants that participate in logic, excluding docstrings and comments.

    Checked with the AST rather than by scanning lines: a docstring that *explains* why the
    literals were removed is documentation, while a literal in a comparison is a decision.
    """
    import ast

    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


@pytest.mark.parametrize("module", DECISION_MODULES)
def test_no_legacy_topic_literals_in_executable_code(module):
    """Docstrings may record the history; executable literals may not key off it."""
    path = Path(__file__).resolve().parents[1] / module
    literals = [value.lower() for value in _decision_literals(path.read_text(encoding="utf-8"))]

    for literal in literals:
        for token in LEGACY_TOKENS:
            assert token not in literal, f"{module} still keys off {token!r} in a literal"


def test_soundtrack_theme_comes_from_the_model_suggestion():
    from app.pipeline.soundtrack_selector import SoundtrackSelector

    selector = SoundtrackSelector()
    assert selector._detect_theme([], {"soundtrack_suggestion": "mystery_tension"}) == "mystery_tension"
    assert selector._detect_theme([], {}) == "generic"


def test_soundtrack_no_longer_infers_a_theme_from_content():
    from app.pipeline.soundtrack_selector import SoundtrackSelector

    selector = SoundtrackSelector()
    cuts = [{"title": "o poder do governo", "hook": "dinheiro e economia", "description": "wall street"}]
    assert selector._detect_theme(cuts, {}) == "generic"
