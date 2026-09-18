"""O pipeline não espera por uma pessoa.

Havia dois modos. No automático a IA decidia os cortes; no manual o worker escrevia um
`prompt.txt`, mandava por Telegram, dava ack no job e ia embora — a run ficava em
`WAITING_AI` até alguém colar a resposta de volta. O default de quem criava um vídeo pela
tela era o manual, então o caminho que o operador usava era justamente o que nunca terminava
sozinho.

Sobrou um modo. O que se afirma aqui é que não dá para voltar atrás sem quebrar um teste.
"""
from __future__ import annotations

import inspect

import pytest

from app.pipeline import pipeline as pipeline_module
from app.pipeline.presets import resolve_clip_preset, resolve_product_preset


def test_the_pipeline_takes_no_mode_switch():
    """`build_ia` era o interruptor entre decidir sozinho e esperar alguém."""
    assert "build_ia" not in inspect.signature(pipeline_module.Pipeline.__init__).parameters


def test_nothing_is_sent_to_a_person_to_answer():
    source = inspect.getsource(pipeline_module)
    assert "Envie o arquivo" not in source
    assert "Cole no ChatGPT" not in source
    assert "send_prompt" not in source


def test_prepare_reports_completion_not_waiting():
    """O nome do desfecho era `awaiting_manual_llm`, e o Studio o tratava como terminal —
    parava de atualizar a tela num job que ainda tinha metade do caminho pela frente."""
    source = inspect.getsource(pipeline_module)
    assert "awaiting_manual_llm" not in source
    assert "prepare_complete" in source


def test_the_ai_step_is_not_optional():
    """Um prepare que termina sem enfileirar o finalize é um job que morre em silêncio."""
    source = inspect.getsource(pipeline_module.Pipeline._prepare_stage)
    assert "_run_ai_and_enqueue" in source
    assert "if self.build_ia" not in source


@pytest.mark.parametrize("requested", ["raw_edit", "authorial_edit", "video_edit"])
def test_the_authorial_edit_mode_is_gone(requested):
    """Era edição autoral de um vídeo bruto, e só existia esperando um humano devolver o
    roteiro. Um apelido dele agora cai no preset padrão de cortes, em vez de resolver para
    um modo cujo caminho não existe mais."""
    assert resolve_product_preset(requested).clip_mode != "raw_edit"
    assert resolve_clip_preset(requested, "landscape").clip_mode != "raw_edit"
