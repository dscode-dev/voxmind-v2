"""As duas portas do fluxo manual estão fechadas.

A API oferecia `/jobs/{id}/prompt/download` e `/jobs/{id}/submit-ai-response`: baixar o
prompt para colar num LLM, e devolver a resposta à mão. Enquanto existirem, existe um
caminho em que o pipeline depende de alguém — e era o caminho que a tela de Produção usava
por padrão, porque `build_ia` nascia `false`.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.api.router import api_router
from app.models.enums import JobStatus


@pytest.fixture()
def routes():
    app = FastAPI()
    app.include_router(api_router)
    return {getattr(route, "path", "") for route in app.routes}


def test_the_prompt_cannot_be_downloaded_to_be_pasted_elsewhere(routes):
    # Controle positivo: uma asserção de ausência sobre um conjunto vazio passa sozinha.
    assert "/jobs/{job_id}" in routes
    assert "/jobs/{job_id}/prompt/download" not in routes


def test_no_one_can_hand_the_pipeline_an_answer(routes):
    assert "/jobs/{job_id}/submit-ai-response" not in routes


def test_the_waiting_state_no_longer_names_a_person():
    """`awaiting_manual_llm` aparecia na tela. Com o fluxo manual fora, o nome era mentira:
    quem está trabalhando ali é a IA."""
    values = {status.value for status in JobStatus}
    assert "awaiting_manual_llm" not in values
    assert "processing_ai" in values


def test_creating_a_job_does_not_offer_a_mode(monkeypatch):
    """`build_ia` era escolha de quem criava o vídeo, e o default levava ao fluxo manual."""
    from app.api import jobs

    assert not hasattr(jobs, "SubmitAiResponseInput")
    fields = {name for name in jobs.CreateJobInput.model_fields}
    assert "build_ia" not in fields
