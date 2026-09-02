"""Checkpoints, cache identity, language aggregation and text policy (PR-ASR-01).

The Transcriber is built without touching a model: `_ensure_model` is never called on these
paths, so no weights are loaded and nothing reaches the network.
"""

import json
from types import SimpleNamespace

import pytest

from app.media.asr_windows import AsrWindow, build_windows
from app.media.seam_reconciler import SeamPolicy
from app.media.transcriber import ASR_PIPELINE_VERSION, Transcriber


def make_transcriber(**overrides) -> Transcriber:
    kwargs = dict(
        model_size="small",
        device="cpu",
        compute_type="int8",
        cpu_threads=1,
        language="auto",
        beam_size=1,
        vad_filter=True,
        segment_duration_sec=900,
        window_overlap_sec=5.0,
    )
    kwargs.update(overrides)
    return Transcriber(**kwargs)


def info(language, probability):
    return SimpleNamespace(language=language, language_probability=probability)


# ==========================================================================
# Checkpoints
# ==========================================================================


def test_checkpoint_records_window_metadata_and_config(tmp_path):
    transcriber = make_transcriber()
    window = AsrWindow(index=1, start=895.0, end=1795.0, overlap_sec=5.0)
    part = tmp_path / "part_001.json"

    transcriber._write_checkpoint(part, window, transcriber.config_hash(), [{"start": 900.0, "end": 905.0, "text": "x"}])

    payload = json.loads(part.read_text(encoding="utf-8"))
    assert payload["asr_pipeline_version"] == ASR_PIPELINE_VERSION
    assert payload["config_hash"] == transcriber.config_hash()
    assert payload["window"]["start"] == 895.0
    assert payload["window"]["end"] == 1795.0
    assert payload["window"]["overlap_sec"] == 5.0
    assert payload["segments"] == [{"start": 900.0, "end": 905.0, "text": "x"}]


def test_matching_checkpoint_is_reused(tmp_path):
    transcriber = make_transcriber()
    window = AsrWindow(index=0, start=0.0, end=900.0, overlap_sec=0.0)
    part = tmp_path / "part_000.json"
    transcriber._write_checkpoint(part, window, transcriber.config_hash(), [{"start": 1.0, "end": 2.0, "text": "a"}])

    loaded = transcriber._load_checkpoint(part, window, transcriber.config_hash())

    assert loaded is not None
    assert loaded["segments"][0]["text"] == "a"


def test_legacy_checkpoint_is_recognised_and_rejected(tmp_path):
    """Pre-PR checkpoints are bare lists from a non-overlapping layout: their offsets do not
    match the current windows, so reusing them would corrupt the timeline."""
    part = tmp_path / "part_000.json"
    part.write_text(json.dumps([{"start": 0.0, "end": 5.0, "text": "legado"}]), encoding="utf-8")

    transcriber = make_transcriber()
    window = AsrWindow(index=0, start=0.0, end=900.0, overlap_sec=0.0)

    assert transcriber._load_checkpoint(part, window, transcriber.config_hash()) is None


def test_checkpoint_from_a_different_configuration_is_rejected(tmp_path):
    window = AsrWindow(index=0, start=0.0, end=900.0, overlap_sec=0.0)
    part = tmp_path / "part_000.json"

    old = make_transcriber(window_overlap_sec=0.0)
    old._write_checkpoint(part, window, old.config_hash(), [{"start": 1.0, "end": 2.0, "text": "a"}])

    new = make_transcriber(window_overlap_sec=5.0)
    assert new._load_checkpoint(part, window, new.config_hash()) is None


def test_checkpoint_for_a_different_window_range_is_rejected(tmp_path):
    transcriber = make_transcriber()
    part = tmp_path / "part_001.json"
    stored = AsrWindow(index=1, start=900.0, end=1800.0, overlap_sec=0.0)
    transcriber._write_checkpoint(part, stored, transcriber.config_hash(), [])

    moved = AsrWindow(index=1, start=895.0, end=1795.0, overlap_sec=5.0)
    assert transcriber._load_checkpoint(part, moved, transcriber.config_hash()) is None


def test_unreadable_checkpoint_is_ignored(tmp_path):
    part = tmp_path / "part_000.json"
    part.write_text("{ not json", encoding="utf-8")

    transcriber = make_transcriber()
    window = AsrWindow(index=0, start=0.0, end=900.0, overlap_sec=0.0)
    assert transcriber._load_checkpoint(part, window, transcriber.config_hash()) is None


def test_resume_is_consistent(tmp_path):
    """Reprocessing from checkpoints must produce the same transcript."""
    transcriber = make_transcriber()
    windows = build_windows(1800, 900, 5)
    segments_by_window = [
        [{"start": w.start + 1, "end": w.start + 4, "text": f"janela {w.index}"}] for w in windows
    ]

    for window, segments in zip(windows, segments_by_window):
        part = tmp_path / f"part_{window.index:03d}.json"
        transcriber._write_checkpoint(part, window, transcriber.config_hash(), segments)

    first = [
        transcriber._load_checkpoint(tmp_path / f"part_{w.index:03d}.json", w, transcriber.config_hash())["segments"]
        for w in windows
    ]
    second = [
        transcriber._load_checkpoint(tmp_path / f"part_{w.index:03d}.json", w, transcriber.config_hash())["segments"]
        for w in windows
    ]
    assert first == second == segments_by_window


# ==========================================================================
# Cache identity
# ==========================================================================


def test_config_hash_changes_with_the_overlap_policy():
    assert make_transcriber(window_overlap_sec=0.0).config_hash() != make_transcriber(
        window_overlap_sec=5.0
    ).config_hash()


@pytest.mark.parametrize(
    "field,value",
    [
        ("segment_duration_sec", 600),
        ("word_timestamps", True),
        ("strip_fillers", True),
        ("model_size", "large-v3"),
        ("beam_size", 5),
        ("vad_filter", False),
    ],
)
def test_config_hash_changes_with_each_policy_input(field, value):
    assert make_transcriber().config_hash() != make_transcriber(**{field: value}).config_hash()


def test_config_hash_changes_with_the_seam_policy():
    strict = make_transcriber(seam_policy=SeamPolicy(min_text_similarity=0.95))
    assert make_transcriber().config_hash() != strict.config_hash()


def test_config_hash_is_stable_for_the_same_configuration():
    assert make_transcriber().config_hash() == make_transcriber().config_hash()


def test_transcript_cache_key_includes_the_new_policy():
    """The pipeline's MinIO transcript cache must not serve a pre-overlap transcript."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "app/pipeline/pipeline.py"
    body = source.read_text(encoding="utf-8")
    cache_block = body.split("def _transcript_cache_key")[1].split("def ")[0]

    assert '"cache_version": 2' in cache_block
    assert "asr_window_overlap_sec" in cache_block
    assert "asr_pipeline_version" in cache_block
    assert "asr_seam_policy" in cache_block


# ==========================================================================
# Language aggregation
# ==========================================================================


def test_language_is_aggregated_across_windows_not_taken_from_the_last():
    """The regression: `last_transcription_info` was overwritten per window, so one
    misdetected closing window relabelled the whole video."""
    transcriber = make_transcriber()
    transcriber._language_observations = [
        {"window_index": 0, "duration_sec": 900.0, "language": "pt", "probability": 0.98},
        {"window_index": 1, "duration_sec": 900.0, "language": "pt", "probability": 0.97},
        {"window_index": 2, "duration_sec": 40.0, "language": "es", "probability": 0.55},
    ]

    metadata = transcriber._aggregate_language_metadata()

    assert metadata["detected_language"] == "pt"
    assert metadata["language_agreement"] > 0.9


def test_a_short_uncertain_window_cannot_outvote_long_confident_ones():
    transcriber = make_transcriber()
    transcriber._language_observations = [
        {"window_index": 0, "duration_sec": 900.0, "language": "pt", "probability": 0.9},
        {"window_index": 1, "duration_sec": 5.0, "language": "en", "probability": 0.99},
    ]
    assert transcriber._aggregate_language_metadata()["detected_language"] == "pt"


def test_missing_confidence_degrades_to_duration_weighting():
    transcriber = make_transcriber()
    transcriber._language_observations = [
        {"window_index": 0, "duration_sec": 900.0, "language": "pt", "probability": None},
        {"window_index": 1, "duration_sec": 100.0, "language": "en", "probability": None},
    ]
    assert transcriber._aggregate_language_metadata()["detected_language"] == "pt"


def test_no_observations_yields_no_detected_language():
    transcriber = make_transcriber()
    transcriber._language_observations = []
    assert transcriber._aggregate_language_metadata()["detected_language"] is None


def test_observations_are_retained_for_inspection():
    transcriber = make_transcriber()
    transcriber._language_observations = [
        {"window_index": 0, "duration_sec": 900.0, "language": "pt", "probability": 0.9},
    ]
    assert len(transcriber._aggregate_language_metadata()["language_observations"]) == 1


def test_language_observation_records_window_duration():
    transcriber = make_transcriber()
    window = AsrWindow(index=3, start=100.0, end=190.0, overlap_sec=5.0)

    observation = transcriber._language_observation(info("pt", 0.91), window)

    assert observation == {
        "window_index": 3,
        "duration_sec": 90.0,
        "language": "pt",
        "probability": 0.91,
    }


# ==========================================================================
# Text policy
# ==========================================================================


def test_cleanup_preserves_hesitation_and_repetition():
    """ClipFlow cuts real speech: hesitation and repetition carry editorial meaning.

    The old `_normalize_text` deleted filler words and collapsed repeats without adjusting
    timestamps, so the stored text no longer matched the audio.
    """
    transcriber = make_transcriber()
    cleaned = transcriber._clean_text("eh  o  Palmeiras,  o Palmeiras nao   conseguiu!!!")

    assert "eh" in cleaned
    assert cleaned.count("Palmeiras") == 2
    assert "  " not in cleaned
    assert "!!!" not in cleaned


def test_filler_stripping_is_available_separately():
    transcriber = make_transcriber()
    stripped = transcriber._strip_fillers("eh o o Palmeiras nao nao conseguiu")

    assert not stripped.startswith("eh")
    # Collapses an immediately repeated *word*. Multi-word repeats ("o Palmeiras o
    # Palmeiras") are left alone — the same scope the original had, and deliberately not
    # widened: aggressive rewriting of real speech is what this PR moved away from.
    assert stripped == "o Palmeiras nao conseguiu"


def test_stripped_form_is_stored_alongside_not_instead_of(tmp_path):
    """When enabled, the aggressive form is an extra field; `text` stays faithful."""
    transcriber = make_transcriber(strip_fillers=True)
    assert transcriber.strip_fillers is True
    assert transcriber._clean_text("eh o time") == "eh o time"
    assert transcriber._strip_fillers("eh o time") == "o time"


def test_empty_text_is_handled():
    transcriber = make_transcriber()
    assert transcriber._clean_text("") == ""
    assert transcriber._strip_fillers("") == ""
