"""Não ter medido não é ter medido mal.

Uma execução real produziu quatro clipes com 94/100, decisão "approved" e zero problemas. O
ciclo mesmo assim parou esperando uma pessoa: o único aviso era
`speaker_continuity_unmeasurable`, que a política contava como defeito estrutural, e quatro
clipes "em revisão" passam do limite de um.

Esse aviso não descreve o corte — descreve a diarização não ter conseguido separar as vozes,
o que acontece em qualquer vídeo de um locutor só. Enquanto contasse como defeito, *todo*
ciclo com diarização degradada exigiria um humano, e a autonomia seria impossível por
construção.
"""
from __future__ import annotations

from app.pipeline.auto_review import AutoReviewPolicy


def _clip(warnings=None, issues=None, score=94):
    return {
        "clip_index": 1,
        "file_name": "cut_01.mp4",
        "score": score,
        "decision": "approved",
        "warnings": warnings or [],
        "issues": issues or [],
    }


def _media(status="auto_ready"):
    return {"status": status, "gate": "pass", "issues": [], "warnings": [], "summary": {}}


def _evaluate(clips, policy=None):
    return (policy or AutoReviewPolicy()).evaluate(
        qa_report={"clips": clips},
        cuts=[{} for _ in clips],
        final_media_report=_media(),
    )


def test_a_run_whose_only_warning_is_unmeasurable_speech_publishes_itself():
    """O caso exato que parou na vida real: quatro clipes, 94/100, nenhum problema."""
    result = _evaluate([_clip(["speaker_continuity_unmeasurable"]) for _ in range(4)])

    assert result["status"] == "auto_ready"
    assert result["publication_eligibility"]["eligible"] is True


def test_the_warning_is_still_reported():
    """Deixar de bloquear não é deixar de contar: o operador ainda vê que não foi medido."""
    result = _evaluate([_clip(["speaker_continuity_unmeasurable"])])

    assert "speaker_continuity_unmeasurable" in result["clips"][0]["reasons"]


def test_a_measured_defect_still_stops_the_run():
    """O controle. Um corte que começa no meio de uma frase foi medido, e saiu errado."""
    result = _evaluate([_clip(["starts_mid_segment"]) for _ in range(4)])

    assert result["status"] == "needs_human_review"
    assert result["publication_eligibility"]["eligible"] is False


def test_a_weak_hook_still_stops_the_run():
    result = _evaluate([_clip(["weak_hook"]) for _ in range(4)])

    assert result["status"] == "needs_human_review"


def test_a_technical_failure_still_stops_the_run():
    """A camada técnica nunca é afrouxada por nada disto."""
    result = AutoReviewPolicy().evaluate(
        qa_report={"clips": [_clip(["speaker_continuity_unmeasurable"])]},
        cuts=[{}],
        final_media_report={"status": "blocked", "gate": "fail",
                            "issues": ["video_frozen"], "warnings": [], "summary": {}},
    )

    assert result["publication_eligibility"]["eligible"] is False
