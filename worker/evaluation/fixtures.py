"""Synthetic transcript builders for the evaluation dataset.

Every case in ``datasets/voxmind`` is currently ``source_type: synthetic``. The repository
contains no Voxmind media, no recorded ASR output and no human labels, so inventing "real"
cases would produce numbers that look authoritative and mean nothing. These fixtures instead
model the *shapes* the audit identified — a payoff cut just under the editorial minimum, a
transcript far longer than the prompt budget, a two-chain short_serie, a degraded diarization
run — so the metrics measure real code paths against known-shaped input.

Adding a real case later requires only dropping ``transcript_with_speakers.json`` plus a
``metadata.json`` with ``source_type: real`` into a new case directory; the runner discovers
it automatically.
"""
from __future__ import annotations

from typing import Any, Dict, List


FOOTBALL_LINES = [
    "o que aconteceu naquele jogo mudou completamente a temporada do time",
    "ninguém esperava que ele fosse assumir a responsabilidade daquele jeito",
    "a comissão técnica sabia do problema e escolheu não falar nada",
    "quando o árbitro apitou o pênalti o estádio inteiro parou de respirar",
    "ele chegou no vestiário e disse que ia sair no fim da temporada",
    "a diretoria negou tudo publicamente mas os jogadores sabiam da verdade",
    "aquele foi o momento em que a torcida virou contra o próprio ídolo",
    "o contrato tinha uma cláusula que ninguém do clube tinha lido direito",
    "ele perdeu o gol e depois disso nunca mais foi o mesmo jogador",
    "o técnico assumiu o erro na entrevista e isso salvou o grupo",
    "existe uma diferença enorme entre pressão de torcida e pressão de imprensa",
    "por isso aquela temporada é lembrada até hoje como um divisor de águas",
]


def make_segment(
    start: float,
    end: float,
    text: str,
    speaker: str = "SPEAKER_00",
) -> Dict[str, Any]:
    return {"start": round(start, 2), "end": round(end, 2), "text": text, "speaker": speaker}


def build_transcript(
    *,
    segment_count: int,
    segment_duration: float = 6.0,
    speakers: List[str] | None = None,
    start_at: float = 0.0,
) -> List[Dict[str, Any]]:
    """A transcript with real, non-overlapping ASR-shaped timestamps."""
    speakers = speakers or ["SPEAKER_00"]
    segments: List[Dict[str, Any]] = []
    cursor = start_at

    for index in range(segment_count):
        text = FOOTBALL_LINES[index % len(FOOTBALL_LINES)]
        # A terminator on most lines, so boundary metrics have something to detect.
        if index % 3 != 2:
            text = f"{text}."
        segments.append(
            make_segment(
                cursor,
                cursor + segment_duration,
                text,
                speakers[index % len(speakers)],
            )
        )
        cursor += segment_duration

    return segments


def build_dialogue_transcript(segment_count: int = 60) -> List[Dict[str, Any]]:
    """Two alternating speakers, so speaker-continuity metrics are measurable."""
    return build_transcript(
        segment_count=segment_count,
        segment_duration=5.0,
        speakers=["SPEAKER_00", "SPEAKER_01"],
    )


def build_unknown_speaker_transcript(segment_count: int = 40) -> List[Dict[str, Any]]:
    """Diarization degraded: every label UNKNOWN."""
    return build_transcript(
        segment_count=segment_count,
        segment_duration=6.0,
        speakers=["UNKNOWN"],
    )


def build_long_transcript(segment_count: int = 900) -> List[Dict[str, Any]]:
    """Long enough that the transcript cannot fit the prompt budget.

    This is the shape that produced blind span selection: the old builder collapsed it into
    head/middle/tail excerpts while still offering every span of the full video.
    """
    return build_transcript(segment_count=segment_count, segment_duration=6.0)
