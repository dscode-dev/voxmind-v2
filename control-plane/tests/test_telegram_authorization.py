"""Telegram authorization is deny-by-default (PR-BOOT-01).

Before this change every handler was reachable by any Telegram user who found the bot, so
`/new <url>` from a stranger enqueued a GPU job. These tests pin the new behaviour at both
levels: the allowlist resolution in settings, and the handlers refusing to act.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app import bot as bot_module
from app.bot import VoxmindBot, is_authorized
from app.settings import settings


AUTHORIZED_CHAT = "-1001111111111"
STRANGER_CHAT = "-1009999999999"
AUTHORIZED_USER = "424242"
STRANGER_USER = "777777"


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []
        self.document = None

    async def reply_text(self, text: str, *args, **kwargs):
        self.replies.append(text)


def make_update(chat_id: str | None, user_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=None if chat_id is None else SimpleNamespace(id=chat_id),
        effective_user=None if user_id is None else SimpleNamespace(id=user_id),
        message=FakeMessage(),
    )


@pytest.fixture
def allowlist(monkeypatch):
    """Configure an explicit chat allowlist and capture every publish attempt."""
    monkeypatch.setattr(
        settings, "telegram_allowed_chat_ids", AUTHORIZED_CHAT, raising=False
    )
    monkeypatch.setattr(
        settings, "telegram_allowed_user_ids", AUTHORIZED_USER, raising=False
    )

    published: list[dict] = []
    monkeypatch.setattr(
        bot_module.publisher,
        "publish",
        lambda **kwargs: published.append(kwargs),
    )
    monkeypatch.setattr(bot_module.registry, "register", lambda *a, **k: None)
    return published


# ==========================================================================
# Allowlist resolution
# ==========================================================================


def test_explicit_chat_allowlist_is_used(monkeypatch):
    monkeypatch.setattr(
        settings, "telegram_allowed_chat_ids", "111, 222 ,333", raising=False
    )
    assert settings.allowed_chat_ids == {"111", "222", "333"}


def test_allowlist_falls_back_to_the_operational_chat(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "-100555", raising=False)
    assert settings.allowed_chat_ids == {"-100555"}


def test_authorized_chat_is_allowed(allowlist):
    assert is_authorized(make_update(AUTHORIZED_CHAT)) is True


def test_authorized_user_is_allowed_from_any_chat(allowlist):
    assert is_authorized(make_update(STRANGER_CHAT, AUTHORIZED_USER)) is True


def test_stranger_is_denied(allowlist):
    assert is_authorized(make_update(STRANGER_CHAT, STRANGER_USER)) is False


def test_empty_allowlists_deny_everyone(monkeypatch):
    """A missing allowlist must never mean "allow everyone"."""
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "", raising=False)
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "", raising=False)

    assert is_authorized(make_update(AUTHORIZED_CHAT, AUTHORIZED_USER)) is False
    assert is_authorized(make_update(STRANGER_CHAT)) is False


def test_update_without_chat_or_user_is_denied(allowlist):
    assert is_authorized(make_update(None, None)) is False


# ==========================================================================
# Handlers — no job is created for an unauthorized sender
# ==========================================================================


def _run(coro):
    return asyncio.run(coro)


def test_new_from_authorized_chat_enqueues_a_job(allowlist):
    update = make_update(AUTHORIZED_CHAT)
    context = SimpleNamespace(args=["https://youtube.com/watch?v=abc"])

    _run(VoxmindBot.handle_new(None, update, context))

    assert len(allowlist) == 1
    assert allowlist[0]["video_url"] == "https://youtube.com/watch?v=abc"
    assert allowlist[0]["pipeline_stage"] == "prepare"


def test_new_from_unauthorized_chat_creates_no_job(allowlist):
    update = make_update(STRANGER_CHAT, STRANGER_USER)
    context = SimpleNamespace(args=["https://youtube.com/watch?v=abc"])

    _run(VoxmindBot.handle_new(None, update, context))

    assert allowlist == []
    assert update.message.replies == ["Não autorizado."]


def test_finalize_from_unauthorized_chat_creates_no_job(allowlist):
    update = make_update(STRANGER_CHAT, STRANGER_USER)

    _run(VoxmindBot.handle_finalize(None, update, SimpleNamespace()))

    assert allowlist == []
    assert update.message.replies == ["Não autorizado."]


def test_json_document_from_unauthorized_chat_is_ignored(allowlist, monkeypatch):
    processed: list[str] = []
    monkeypatch.setattr(
        VoxmindBot,
        "_process_json_document",
        lambda self, u, c, d: processed.append("called"),
    )

    update = make_update(STRANGER_CHAT, STRANGER_USER)
    _run(VoxmindBot.handle_document(None, update, SimpleNamespace()))

    assert processed == []
    assert allowlist == []


def test_text_payload_from_unauthorized_chat_is_ignored(allowlist):
    update = make_update(STRANGER_CHAT, STRANGER_USER)
    update.message.text = '{"job_id": "x", "shorts_content": []}'

    _run(VoxmindBot.handle_text(None, update, SimpleNamespace()))

    assert allowlist == []
    assert update.message.replies == ["Não autorizado."]
