"""O Telegram não pode cobrar o tempo do pipeline.

Num job real cada passo perdia ~7 segundos: `_log` chamava o envio com retry, o destino
estava mal configurado, e um 400 "chat not found" conta como ``RequestException`` — então a
configuração errada pagava o backoff inteiro, em toda mensagem, dezenas de vezes por job.

O que se afirma aqui: o que não melhora tentando de novo não é tentado de novo, e um destino
recusado desliga a entrega uma vez em vez de ser redescoberto a cada aviso.
"""
from __future__ import annotations

import requests

from app.integrations import telegram_sender as ts


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = "{}"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


def _sender(monkeypatch, status_code):
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        return _Response(status_code)

    monkeypatch.setattr(ts.requests, "post", fake_post)
    monkeypatch.setattr(ts.settings, "telegram_disable_notifications", False)
    monkeypatch.setattr(ts.settings, "telegram_bot_token", "token")
    monkeypatch.setattr(ts.settings, "telegram_chat_id", "42")
    ts.TelegramSender._disabled_reason = None
    return ts.TelegramSender(), calls


def test_a_rejected_chat_is_not_retried(monkeypatch):
    """400 é configuração. Tentar de novo dá o mesmo 400, mais tarde."""
    sender, calls = _sender(monkeypatch, 400)

    assert sender.send_message_safe("oi") is False

    assert calls["n"] == 1


def test_a_server_error_is_retried(monkeypatch):
    """503 é azar, e vale a segunda tentativa — o controle para o teste acima."""
    sender, calls = _sender(monkeypatch, 503)

    assert sender.send_message_safe("oi") is False

    assert calls["n"] > 1


def test_a_rejected_destination_stops_being_tried(monkeypatch):
    """O segundo aviso do mesmo job não deve nem chegar à rede."""
    sender, calls = _sender(monkeypatch, 400)
    sender.send_message_safe("primeiro")
    before = calls["n"]

    sender.send_message_safe("segundo")
    sender.send_message_safe("terceiro")

    assert calls["n"] == before


def test_delivery_failure_is_never_the_job_failing(monkeypatch):
    """O vídeo renderizado não vira falha porque uma mensagem não foi entregue."""
    sender, _ = _sender(monkeypatch, 400)

    assert sender.send_message_safe("oi") is False  # retorna, não levanta
