"""Seam fixtures for ASR reconciliation.

Real audio is not required to test the reconciliation layer: it consumes ASR *segments*, so
the cases here are synthetic segment streams shaped exactly like what Whisper emits for two
overlapping windows. Each fixture models one seam situation the audit identified.

Every fixture returns ``(window_segments, expectation)`` where ``window_segments`` is the
``[(AsrWindow, segments)]`` input the reconciler takes, with **absolute** timestamps
already applied.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from app.media.asr_windows import AsrWindow


WINDOW = 60.0
OVERLAP = 5.0


def _windows() -> Tuple[AsrWindow, AsrWindow]:
    """Two windows sharing [55, 60): window 0 = [0,60), window 1 = [55,115)."""
    return (
        AsrWindow(index=0, start=0.0, end=WINDOW, overlap_sec=0.0),
        AsrWindow(index=1, start=WINDOW - OVERLAP, end=WINDOW - OVERLAP + WINDOW,
                  overlap_sec=OVERLAP),
    )


def seg(start: float, end: float, text: str, speaker: str = "SPEAKER_00", **extra) -> Dict[str, Any]:
    return {"start": start, "end": end, "text": text, "speaker": speaker, **extra}


@dataclass
class SeamCase:
    case_id: str
    description: str
    window_segments: List[Tuple[AsrWindow, List[Dict[str, Any]]]]
    expected_texts: List[str]
    expect_duplicates_removed: int
    notes: str = ""


def case_sentence_crosses_boundary() -> SeamCase:
    """A sentence spoken across the seam.

    Window 0 hears it truncated at its right edge; window 1 has the whole thing because its
    audio starts 5s earlier. The complete version must win.
    """
    w0, w1 = _windows()
    return SeamCase(
        case_id="sentence_crosses_boundary",
        description="Sentence spans the window boundary; window 0's copy is cut off.",
        window_segments=[
            (w0, [
                seg(50.0, 56.0, "o tecnico assumiu o erro na entrevista."),
                seg(56.2, 60.0, "e o problema foi que o Palmeiras"),  # truncated at edge
            ]),
            (w1, [
                seg(56.2, 63.5, "e o problema foi que o Palmeiras nao conseguiu vencer."),
                seg(64.0, 70.0, "a torcida virou contra o proprio idolo."),
            ]),
        ],
        expected_texts=[
            "o tecnico assumiu o erro na entrevista.",
            "e o problema foi que o Palmeiras nao conseguiu vencer.",
            "a torcida virou contra o proprio idolo.",
        ],
        expect_duplicates_removed=1,
        notes="The truncated fragment must be replaced by the complete transcription.",
    )


def case_word_crosses_boundary() -> SeamCase:
    """A single word split by the extraction; window 1 has it whole."""
    w0, w1 = _windows()
    return SeamCase(
        case_id="word_crosses_boundary",
        description="A word is cut in half by the window edge.",
        window_segments=[
            (w0, [seg(57.0, 60.0, "a comissao tec")]),
            (w1, [seg(57.0, 61.5, "a comissao tecnica sabia do problema.")]),
        ],
        expected_texts=["a comissao tecnica sabia do problema."],
        expect_duplicates_removed=1,
    )


def case_exact_duplicate() -> SeamCase:
    """Both windows transcribe the shared region identically. Keep exactly one."""
    w0, w1 = _windows()
    text = "quando o arbitro apitou o penalti o estadio parou."
    return SeamCase(
        case_id="exact_duplicate_overlap",
        description="Identical transcription of the overlap region from both windows.",
        window_segments=[
            (w0, [seg(50.0, 55.5, "ele chegou no vestiario."), seg(55.6, 59.0, text)]),
            (w1, [seg(55.6, 59.0, text), seg(60.0, 66.0, "a diretoria negou tudo.")]),
        ],
        expected_texts=[
            "ele chegou no vestiario.",
            text,
            "a diretoria negou tudo.",
        ],
        expect_duplicates_removed=1,
    )


def case_fuzzy_duplicate() -> SeamCase:
    """Same speech, slightly different transcription and timing between windows."""
    w0, w1 = _windows()
    return SeamCase(
        case_id="fuzzy_duplicate_overlap",
        description="Same utterance, small transcription and timing differences.",
        window_segments=[
            (w0, [seg(55.8, 59.2, "o contrato tinha uma clausula que ninguem leu")]),
            (w1, [seg(55.5, 59.4, "o contrato tinha uma clausula que ninguem tinha lido direito.")]),
        ],
        expected_texts=["o contrato tinha uma clausula que ninguem tinha lido direito."],
        expect_duplicates_removed=1,
        notes="The more complete transcription wins; neither is truncated by an edge here.",
    )


def case_speaker_change_near_boundary() -> SeamCase:
    """A speaker transition inside the overlap must not corrupt the turn structure."""
    w0, w1 = _windows()
    return SeamCase(
        case_id="speaker_change_near_boundary",
        description="Speaker turn changes inside the shared region.",
        window_segments=[
            (w0, [
                seg(52.0, 56.0, "existe diferenca entre pressao de torcida.", "SPEAKER_00"),
                seg(56.5, 60.0, "mas a imprensa cobra de outro jeito", "SPEAKER_01"),
            ]),
            (w1, [
                seg(56.5, 61.2, "mas a imprensa cobra de outro jeito completamente.", "SPEAKER_01"),
                seg(62.0, 68.0, "por isso aquela temporada e lembrada.", "SPEAKER_00"),
            ]),
        ],
        expected_texts=[
            "existe diferenca entre pressao de torcida.",
            "mas a imprensa cobra de outro jeito completamente.",
            "por isso aquela temporada e lembrada.",
        ],
        expect_duplicates_removed=1,
        notes="Speaker labels must survive; ordering must stay monotonic for the merger.",
    )


def case_silence_at_boundary() -> SeamCase:
    """Nothing is spoken across the seam. Nothing to deduplicate, nothing to lose."""
    w0, w1 = _windows()
    return SeamCase(
        case_id="silence_at_boundary",
        description="Silence spans the window boundary.",
        window_segments=[
            (w0, [seg(40.0, 48.0, "ele perdeu o gol e nunca mais foi o mesmo.")]),
            (w1, [seg(66.0, 72.0, "a diretoria assumiu o erro publicamente.")]),
        ],
        expected_texts=[
            "ele perdeu o gol e nunca mais foi o mesmo.",
            "a diretoria assumiu o erro publicamente.",
        ],
        expect_duplicates_removed=0,
    )


def case_short_final_window() -> SeamCase:
    """The last window is much shorter than the rest."""
    w0 = AsrWindow(index=0, start=0.0, end=60.0, overlap_sec=0.0)
    w1 = AsrWindow(index=1, start=55.0, end=62.0, overlap_sec=5.0)
    return SeamCase(
        case_id="short_final_window",
        description="Final window is a 7s remainder.",
        window_segments=[
            (w0, [seg(50.0, 58.0, "aquele foi o momento decisivo da temporada.")]),
            (w1, [
                seg(50.0, 58.0, "aquele foi o momento decisivo da temporada."),
                seg(58.5, 61.8, "e ninguem esqueceu."),
            ]),
        ],
        expected_texts=[
            "aquele foi o momento decisivo da temporada.",
            "e ninguem esqueceu.",
        ],
        expect_duplicates_removed=1,
    )


def case_partial_complementary() -> SeamCase:
    """Two halves of one utterance, with no overlap in content.

    Neither is a duplicate of the other: dropping either loses speech. Both must survive.
    """
    w0, w1 = _windows()
    return SeamCase(
        case_id="partial_complementary",
        description="Complementary halves of an utterance; neither may be discarded.",
        window_segments=[
            (w0, [seg(57.0, 60.0, "o resultado daquela partida")]),
            (w1, [seg(60.1, 64.0, "mudou a temporada inteira do clube.")]),
        ],
        expected_texts=[
            "o resultado daquela partida",
            "mudou a temporada inteira do clube.",
        ],
        expect_duplicates_removed=0,
        notes="Low similarity, adjacent in time: complementary, not duplicated.",
    )


ALL_SEAM_CASES = [
    case_sentence_crosses_boundary,
    case_word_crosses_boundary,
    case_exact_duplicate,
    case_fuzzy_duplicate,
    case_speaker_change_near_boundary,
    case_silence_at_boundary,
    case_short_final_window,
    case_partial_complementary,
]


def load_seam_cases() -> List[SeamCase]:
    return [factory() for factory in ALL_SEAM_CASES]


def concatenate_without_reconciliation(
    window_segments: List[Tuple[AsrWindow, List[Dict[str, Any]]]]
) -> List[Dict[str, Any]]:
    """The BEFORE behaviour: naive concatenation, then sort by start.

    This is what the pipeline did with non-overlapping windows, and what overlapping windows
    would produce if nothing reconciled them.
    """
    merged: List[Dict[str, Any]] = []
    for window, segments in window_segments:
        for segment in segments:
            item = dict(segment)
            item.setdefault("window_index", window.index)
            merged.append(item)
    return sorted(merged, key=lambda s: (float(s["start"]), float(s["end"])))
