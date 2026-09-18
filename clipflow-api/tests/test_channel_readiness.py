"""O aviso do canal diz qual é o problema, não "conecte um canal".

Um operador conectou o canal pelo OAuth, escolheu-o no pipeline, e a tela mostrou ao mesmo
tempo "CANAL: VoxMind" num cartão e "Sem canal publicável — Conectar um canal em Publicação"
no outro. As duas frases eram verdadeiras e nenhuma era útil: o canal estava conectado e
apenas **pausado**, porque o OAuth o cria assim de propósito, e nada dizia isso.

Quatro situações diferentes viravam a mesma frase. Aqui cada uma tem a sua.
"""
from __future__ import annotations

import pytest

from app.models.enums import (
    PublishPlatform,
    PublishTargetConnectionStatus,
)
from app.models.pipeline import Pipeline
from app.models.publish_target import PublishTarget
from app.services.pipeline_readiness_service import PipelineReadinessService


@pytest.fixture()
def service():
    return PipelineReadinessService()


def _target(db, **overrides):
    fields = {
        "platform": PublishPlatform.YOUTUBE,
        "name": "VoxMind",
        "channel_title": "VoxMind",
        "is_active": True,
        "connection_status": PublishTargetConnectionStatus.CONNECTED,
        "refresh_token_encrypted": "cifrado",
        "config_json": {},
    }
    fields.update(overrides)
    row = PublishTarget(**fields)
    db.add(row)
    db.flush()
    return row


def _pipeline(db, target=None):
    automation = {"enabled": True}
    if target is not None:
        automation["publish_target_id"] = str(target.id)
    row = Pipeline(
        name="PipeOne",
        keywords_json=["futebol"],
        is_active=True,
        metadata_json={"automation": automation},
    )
    db.add(row)
    db.flush()
    return row


def _check(service, db, pipeline):
    report = service.evaluate(db, pipeline)
    return next(c for c in report.checks if c.code == "channel")


def test_a_paused_channel_says_it_is_paused(db, service):
    """O caso real. Mandar "conectar" quem já conectou é o que travou o operador."""
    target = _target(db, is_active=False)
    check = _check(service, db, _pipeline(db, target))

    assert check.ok is False
    assert "pausado" in check.detail
    assert "VoxMind" in check.detail
    assert "Retomar" in (check.action or "")
    # A frase antiga mandava conectar um canal que já estava conectado.
    assert "Conectar um canal" not in (check.action or "")


def test_no_channel_chosen_points_at_the_pipeline(db, service):
    """Sem canal escolhido o problema é no pipeline, não na tela de Publicação."""
    pipeline = _pipeline(db, None)
    check = _check(service, db, pipeline)

    assert "Nenhum canal escolhido" in check.detail
    assert str(pipeline.id) in (check.href or "")


def test_a_disconnected_channel_says_to_reconnect(db, service):
    target = _target(
        db,
        connection_status=PublishTargetConnectionStatus.DISCONNECTED,
    )
    check = _check(service, db, _pipeline(db, target))

    assert "desconectado" in check.detail
    assert "Reconectar" in (check.action or "")


def test_a_channel_without_a_credential_says_so(db, service):
    target = _target(db, refresh_token_encrypted=None)
    check = _check(service, db, _pipeline(db, target))

    assert "perdeu a credencial" in check.detail


def test_a_working_channel_names_it(db, service):
    check = _check(service, db, _pipeline(db, _target(db)))

    assert check.ok is True
    assert check.detail == "Publica em VoxMind."
    assert check.action is None
