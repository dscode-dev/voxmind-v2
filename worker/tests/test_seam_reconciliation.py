"""Seam reconciliation, transcript invariants and language aggregation (PR-ASR-01)."""

import json
from pathlib import Path
from unittest import mock

import pytest

from app.media.asr_windows import AsrWindow
from app.media.seam_reconciler import (
    SeamPolicy,
    containment,
    count_duplicate_pairs,
    normalize_for_match,
    reconcile_windows,
    temporal_iou,
    text_similarity,
)
from evaluation.asr_fixtures import (
    concatenate_without_reconciliation,
    load_seam_cases,
)


def seg(start, end, text, speaker="SPEAKER_00", **extra):
    return {"start": start, "end": end, "text": text, "speaker": speaker, **extra}


W0 = AsrWindow(index=0, start=0.0, end=60.0, overlap_sec=0.0)
W1 = AsrWindow(index=1, start=55.0, end=115.0, overlap_sec=5.0)


# ==========================================================================
# The fixture suite (one test per seam situation)
# ==========================================================================


@pytest.mark.parametrize("case", load_seam_cases(), ids=lambda c: c.case_id)
def test_seam_case_produces_the_expected_transcript(case):
    segments, stats = reconcile_windows(case.window_segments)

    produced = [normalize_for_match(s["text"]) for s in segments]
    expected = [normalize_for_match(t) for t in case.expected_texts]

    assert produced == expected, case.description
    assert stats.duplicates_removed == case.expect_duplicates_removed


@pytest.mark.parametrize("case", load_seam_cases(), ids=lambda c: c.case_id)
def test_no_duplicates_survive_reconciliation(case):
    segments, _ = reconcile_windows(case.window_segments)
    assert count_duplicate_pairs(segments) == 0


@pytest.mark.parametrize("case", load_seam_cases(), ids=lambda c: c.case_id)
def test_naive_concatenation_is_the_regression(case):
    """Documents what happens without reconciliation."""
    naive = concatenate_without_reconciliation(case.window_segments)
    reconciled, _ = reconcile_windows(case.window_segments)
    assert len(reconciled) <= len(naive)


# ==========================================================================
# Invariants
# ==========================================================================


@pytest.mark.parametrize("case", load_seam_cases(), ids=lambda c: c.case_id)
def test_temporal_ordering_invariant(case):
    segments, _ = reconcile_windows(case.window_segments)

    for segment in segments:
        assert segment["start"] <= segment["end"]
    for previous, current in zip(segments, segments[1:]):
        assert previous["start"] <= current["start"]


@pytest.mark.parametrize("case", load_seam_cases(), ids=lambda c: c.case_id)
def test_no_missing_seam_content(case):
    """Every expected utterance survives: reconciliation removes copies, never speech."""
    segments, _ = reconcile_windows(case.window_segments)
    produced = " | ".join(normalize_for_match(s["text"]) for s in segments)

    for expected in case.expected_texts:
        assert normalize_for_match(expected) in produced


def test_absolute_timestamps_are_preserved():
    segments, _ = reconcile_windows(
        [(W0, [seg(10.0, 20.0, "primeiro trecho.")]),
         (W1, [seg(70.0, 80.0, "segundo trecho.")])]
    )
    assert [(s["start"], s["end"]) for s in segments] == [(10.0, 20.0), (70.0, 80.0)]


def test_inverted_timestamps_are_corrected_and_counted():
    segments, stats = reconcile_windows([(W0, [seg(30.0, 20.0, "invertido.")])])
    assert stats.ordering_failures == 1
    assert segments[0]["start"] <= segments[0]["end"]


def test_small_residual_overlap_is_clamped():
    segments, stats = reconcile_windows(
        [(W0, [seg(10.0, 20.2, "primeiro."), seg(20.0, 30.0, "segundo distinto aqui.")])]
    )
    assert stats.clamped_overlaps == 1
    assert segments[1]["start"] >= segments[0]["end"]


def test_large_genuine_overlap_is_not_rewritten():
    """Crosstalk is real; only seam-sized artifacts are nudged."""
    segments, stats = reconcile_windows(
        [(W0, [seg(10.0, 30.0, "fala longa do primeiro participante."),
               seg(15.0, 35.0, "interrupcao completamente diferente aqui.", "SPEAKER_01")])]
    )
    assert stats.clamped_overlaps == 0
    assert len(segments) == 2


# ==========================================================================
# Winner selection
# ==========================================================================


def test_segment_truncated_at_the_window_end_loses_to_the_complete_version():
    segments, _ = reconcile_windows([
        (W0, [seg(56.0, 60.0, "o problema foi que o time")]),
        (W1, [seg(56.0, 63.0, "o problema foi que o time nao conseguiu vencer.")]),
    ])
    assert len(segments) == 1
    assert "nao conseguiu vencer" in segments[0]["text"]


def test_segment_truncated_at_the_next_window_start_loses_to_the_earlier_one():
    segments, _ = reconcile_windows([
        (W0, [seg(52.0, 58.0, "a diretoria negou tudo publicamente ontem.")]),
        (W1, [seg(55.0, 58.0, "negou tudo publicamente ontem.")]),
    ])
    assert len(segments) == 1
    assert segments[0]["text"].startswith("a diretoria")


def test_when_neither_is_truncated_the_more_complete_text_wins():
    segments, _ = reconcile_windows([
        (W0, [seg(56.5, 58.5, "o contrato tinha clausula")]),
        (W1, [seg(56.5, 58.5, "o contrato tinha uma clausula estranha.")]),
    ])
    assert len(segments) == 1
    assert "estranha" in segments[0]["text"]


def test_selection_is_deterministic():
    case = load_seam_cases()[0]
    first, _ = reconcile_windows(case.window_segments)
    second, _ = reconcile_windows(case.window_segments)
    assert [s["text"] for s in first] == [s["text"] for s in second]


def test_word_timestamps_break_ties_when_present():
    words = [{"start": 56.5, "end": 57.0, "word": "o"}]
    segments, _ = reconcile_windows([
        (W0, [seg(56.5, 58.5, "texto identico aqui")]),
        (W1, [seg(56.5, 58.5, "texto identico aqui", words=words)]),
    ])
    assert len(segments) == 1
    assert segments[0].get("words") == words


def test_speaker_labels_survive_reconciliation():
    case = next(c for c in load_seam_cases() if c.case_id == "speaker_change_near_boundary")
    segments, _ = reconcile_windows(case.window_segments)
    assert [s["speaker"] for s in segments] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]


def test_reconciled_transcript_feeds_the_diarization_merger():
    """The merger requires monotonic, non-inverted segments."""
    from app.media.transcript_merger import TranscriptSpeakerMerger

    case = next(c for c in load_seam_cases() if c.case_id == "speaker_change_near_boundary")
    segments, _ = reconcile_windows(case.window_segments)

    turns = [
        {"speaker": "SPEAKER_00", "start": 50.0, "end": 56.2},
        {"speaker": "SPEAKER_01", "start": 56.2, "end": 61.5},
        {"speaker": "SPEAKER_00", "start": 61.5, "end": 70.0},
    ]
    merged = TranscriptSpeakerMerger().merge(segments, turns)

    assert len(merged) == len(segments)
    assert all(m["speaker"] != "UNKNOWN" for m in merged)


# ==========================================================================
# Similarity primitives
# ==========================================================================


def test_normalization_is_accent_and_case_insensitive():
    assert normalize_for_match("O Palmeiras, NÃO!") == "o palmeiras nao"


def test_containment_detects_a_fragment_of_a_longer_utterance():
    assert containment("o problema foi", "o problema foi que o time perdeu") == 1.0


def test_identical_text_scores_one():
    assert text_similarity("mesmo texto", "mesmo texto") == 1.0


def test_disjoint_text_scores_zero():
    assert text_similarity("alpha beta", "gamma delta") == 0.0


def test_temporal_iou_is_zero_for_disjoint_ranges():
    assert temporal_iou(seg(0, 10, "a"), seg(20, 30, "b")) == 0.0


def test_empty_input_is_handled():
    segments, stats = reconcile_windows([])
    assert segments == []
    assert stats.windows == 0


# ==========================================================================
# Policy
# ==========================================================================


def test_thresholds_live_in_one_policy_object():
    policy = SeamPolicy()
    for field in (
        "min_temporal_iou",
        "min_text_similarity",
        "strong_text_similarity",
        "edge_tolerance_sec",
        "max_clamp_sec",
        "same_span_temporal_iou",
    ):
        assert isinstance(getattr(policy, field), float)


def test_a_stricter_policy_deduplicates_less():
    """A borderline pair: partial time overlap, moderate wording agreement, no containment.

    The default policy calls it the same speech; a policy that demands near-identical text
    keeps both. An *identical* span with one text contained in the other is deduplicated
    under any sane policy, so it would not distinguish them.
    """
    pair = [
        (W0, [seg(56.0, 58.4, "o time jogou mal ontem no estadio")]),
        (W1, [seg(57.0, 59.2, "o time jogou mal ontem no jogo")]),
    ]
    lenient, _ = reconcile_windows(pair, SeamPolicy())
    strict, _ = reconcile_windows(
        pair,
        SeamPolicy(
            min_text_similarity=0.99,
            strong_text_similarity=0.99,
            min_containment=0.99,
            same_span_temporal_iou=0.99,
        ),
    )
    assert len(lenient) == 1
    assert len(strict) == 2


def test_every_threshold_is_named_in_the_policy():
    """No magic numbers scattered through the matcher (the mistake CUT-01 inherited)."""
    import inspect

    from app.media import seam_reconciler

    source = inspect.getsource(seam_reconciler._duplicate_score)
    for literal in ("0.9", "0.6", "0.85", "0.5", "0.3"):
        assert literal not in source, f"threshold {literal} is hardcoded, not in SeamPolicy"
