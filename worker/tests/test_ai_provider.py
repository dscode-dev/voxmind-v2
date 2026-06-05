"""Tests for the AI provider router decision/fallback logic and response validation.

Uses in-memory fake providers — no network, no real OpenAI/local node.
"""
import pytest

from app.ai import events as ai_events
from app.ai.providers.base_provider import AIProvider
from app.ai.provider_router import ProviderRouter
from app.ai.validation import AIResponseValidationError, validate_cuts_response


class FakeProvider(AIProvider):
    def __init__(self, name, *, healthy=True, result=None, raise_exc=None):
        self.name = name
        self.model = f"{name}-model"
        self._healthy = healthy
        self._result = result or {"final_videos": [{"video_index": 1}]}
        self._raise = raise_exc
        self.calls = 0

    def healthcheck(self):
        return self._healthy

    def generate_json(self, system_prompt, user_prompt, schema=None):
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return self._result


def _router(local_enabled, local, openai):
    captured = []
    router = ProviderRouter(
        openai_provider=openai,
        local_provider=local,
        local_enabled=local_enabled,
        emitter=lambda name, **kw: captured.append((name, kw)),
    )
    return router, captured


def _names(captured):
    return [name for name, _ in captured]


def test_local_disabled_uses_openai():
    openai = FakeProvider("openai", result={"final_videos": [{"video_index": 7}]})
    local = FakeProvider("local")
    router, captured = _router(False, local, openai)

    result = router.generate_json("sys", "user")

    assert result == {"final_videos": [{"video_index": 7}]}
    assert openai.calls == 1
    assert local.calls == 0
    assert router.last_provider == "openai"
    assert ai_events.AI_PROVIDER_SELECTED in _names(captured)
    assert ai_events.AI_REQUEST_FINISHED in _names(captured)


def test_local_online_uses_local():
    openai = FakeProvider("openai")
    local = FakeProvider("local", healthy=True, result={"final_videos": [{"video_index": 1}]})
    router, captured = _router(True, local, openai)

    result = router.generate_json("sys", "user")

    assert local.calls == 1
    assert openai.calls == 0
    assert router.last_provider == "local"
    names = _names(captured)
    assert ai_events.LOCAL_PROVIDER_ONLINE in names
    assert ai_events.AI_REQUEST_FINISHED in names


def test_local_offline_falls_back_to_openai():
    openai = FakeProvider("openai", result={"final_videos": [{"video_index": 2}]})
    local = FakeProvider("local", healthy=False)
    router, captured = _router(True, local, openai)

    result = router.generate_json("sys", "user")

    assert result == {"final_videos": [{"video_index": 2}]}
    assert local.calls == 0
    assert openai.calls == 1
    names = _names(captured)
    assert ai_events.LOCAL_PROVIDER_OFFLINE in names
    assert ai_events.AI_FALLBACK in names


def test_local_timeout_falls_back_to_openai():
    openai = FakeProvider("openai", result={"final_videos": [{"video_index": 3}]})
    local = FakeProvider("local", healthy=True, raise_exc=TimeoutError("connection timeout"))
    router, captured = _router(True, local, openai)

    result = router.generate_json("sys", "user")

    assert result == {"final_videos": [{"video_index": 3}]}
    assert local.calls == 1
    assert openai.calls == 1
    names = _names(captured)
    assert ai_events.AI_PROVIDER_FAILED in names
    assert ai_events.AI_FALLBACK in names
    assert router.last_provider == "openai"


def test_openai_failure_propagates():
    openai = FakeProvider("openai", raise_exc=RuntimeError("boom"))
    local = FakeProvider("local")
    router, captured = _router(False, local, openai)

    with pytest.raises(RuntimeError):
        router.generate_json("sys", "user")
    assert ai_events.AI_PROVIDER_FAILED in _names(captured)


def test_validate_accepts_final_videos():
    data = {"job_id": "x", "final_videos": [{"video_index": 1}]}
    assert validate_cuts_response(data) is data


def test_validate_accepts_shorts_content():
    data = {"shorts_content": [{"start": 1, "end": 2}]}
    assert validate_cuts_response(data) is data


def test_validate_rejects_empty():
    with pytest.raises(AIResponseValidationError):
        validate_cuts_response({"job_id": "x"})


def test_validate_rejects_non_dict():
    with pytest.raises(AIResponseValidationError):
        validate_cuts_response("not a dict")
