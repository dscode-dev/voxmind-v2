"""O dono governa os interruptores; o ambiente continua sendo o teto.

Antes isto vivia só no `.env`, e a tela dizia "definido no servidor — somente leitura". Para
ligar a publicação automática era preciso editar um arquivo na máquina e reiniciar a API, o
que não é "acompanhar e configurar preferências".
"""
from __future__ import annotations

import pytest

from app.core.settings import settings
from app.services import autonomy_preferences


@pytest.fixture(autouse=True)
def _permissive_env(monkeypatch):
    """Ambiente permissivo por padrão, para cada teste dizer o que está exercendo."""
    monkeypatch.setattr(settings, "autopublish_enabled", True)
    monkeypatch.setattr(settings, "autopublish_public_enabled", True)
    monkeypatch.setattr(settings, "autopublish_default_privacy", "private")
    monkeypatch.setattr(settings, "autopublish_max_per_day", 3)
    monkeypatch.setattr(settings, "metrics_collection_enabled", False)


def test_without_a_preference_the_environment_answers(db):
    """Nunca ter mexido não é o mesmo que ter desligado."""
    assert autonomy_preferences.load(db).autopublish_enabled is True


def test_the_owner_can_turn_it_on_from_the_panel(db):
    autonomy_preferences.update(db, {"metrics_collection_enabled": True})

    assert autonomy_preferences.load(db).metrics_collection_enabled is True


def test_the_environment_is_a_ceiling_not_a_default(db, monkeypatch):
    """Desligar no ambiente é a alavanca de quem opera a máquina. Uma tela capaz de
    contrariá-la tornaria o `.env` decorativo."""
    monkeypatch.setattr(settings, "autopublish_enabled", False)
    autonomy_preferences.update(db, {"autopublish_enabled": True})

    assert autonomy_preferences.load(db).autopublish_enabled is False


def test_a_cap_above_the_ceiling_is_reduced_to_it(db, monkeypatch):
    monkeypatch.setattr(settings, "autopublish_max_per_day", 3)
    autonomy_preferences.update(db, {"max_per_day": 999})

    assert autonomy_preferences.load(db).max_per_day == 3


def test_intent_is_stored_uncut_so_a_raised_ceiling_restores_it(db, monkeypatch):
    """Guardar já cortado perderia a escolha do dono no dia em que o teto subisse, e ele
    teria de reconfigurar sem saber que precisava."""
    monkeypatch.setattr(settings, "autopublish_max_per_day", 3)
    autonomy_preferences.update(db, {"max_per_day": 20})
    assert autonomy_preferences.load(db).max_per_day == 3

    monkeypatch.setattr(settings, "autopublish_max_per_day", 50)
    assert autonomy_preferences.load(db).max_per_day == 20


def test_public_privacy_cannot_outlive_the_public_switch(db, monkeypatch):
    """Um padrão que o interruptor não autoriza seria uma recusa em cada publicação."""
    monkeypatch.setattr(settings, "autopublish_public_enabled", False)
    autonomy_preferences.update(db, {"default_privacy": "public"})

    autonomy = autonomy_preferences.load(db)

    assert autonomy.autopublish_public_enabled is False
    assert autonomy.default_privacy == "unlisted"


def test_an_unset_field_is_left_alone(db):
    """Um PATCH que omite um interruptor não deve ler o silêncio como "desligue"."""
    autonomy_preferences.update(db, {"autopublish_enabled": False})
    autonomy_preferences.update(db, {"max_per_day": 2})

    assert autonomy_preferences.load(db).autopublish_enabled is False


def test_the_ceiling_is_reported_so_the_panel_can_explain_itself(db, monkeypatch):
    monkeypatch.setattr(settings, "autopublish_public_enabled", False)

    payload = autonomy_preferences.load(db).as_dict()

    assert payload["ceiling"]["autopublish_public_enabled"] is False
