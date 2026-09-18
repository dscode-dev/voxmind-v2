"""O finalize pertence à mesma run que o prepare.

O prepare enfileirava o finalize sem `pipeline_job_id`. O worker lê esse campo para decidir
se reporta ciclo de vida, então no finalize ele não reportava nada: nem etapa, nem conclusão.
A run ficava parada no último estado que o prepare escreveu enquanto os cortes já existiam
no storage — foi exatamente o que aconteceu numa execução real, com dois clipes aprovados em
QA e a tela dizendo "Aguardando IA".
"""
from __future__ import annotations

import inspect

from app.pipeline import pipeline as pipeline_module


def test_the_finalize_payload_carries_the_run_id():
    source = inspect.getsource(pipeline_module.Pipeline._run_ai_and_enqueue)
    assert '"pipeline_job_id": self.pipeline_job_id,' in source


def test_the_finalize_payload_carries_the_chat_it_reports_to():
    """Sem isto o finalize entrega os cortes no chat padrão do deployment, e não no chat do
    pipeline que pediu o vídeo."""
    source = inspect.getsource(pipeline_module.Pipeline._run_ai_and_enqueue)
    assert '"telegram_chat_id": self.telegram_chat_id,' in source
