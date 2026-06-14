from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from skyn3t.adapters import openrouter
from skyn3t.adapters.llm_client import LLMRequest
from skyn3t.config.settings import get_settings


class _FakeResponse:
    status_code = 200
    headers = {}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "choices": [{"message": {"content": "ok"}}],
        }


class _SlowClient:
    def __init__(self):
        self.in_flight = 0
        self.max_seen = 0

    async def post(self, *_args, **_kwargs):
        self.in_flight += 1
        self.max_seen = max(self.max_seen, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        return _FakeResponse()

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_openrouter_state(monkeypatch):
    monkeypatch.delenv("SKYN3T_OPENROUTER_MAX_CONCURRENCY", raising=False)
    get_settings.cache_clear()
    openrouter._request_semaphore = None
    openrouter._request_semaphore_limit = None
    openrouter._request_semaphore_loop_id = None
    yield
    get_settings.cache_clear()
    openrouter._request_semaphore = None
    openrouter._request_semaphore_limit = None
    openrouter._request_semaphore_loop_id = None


@pytest.mark.asyncio
async def test_openrouter_honors_max_concurrency_setting(monkeypatch):
    monkeypatch.setenv("SKYN3T_OPENROUTER_MAX_CONCURRENCY", "2")
    get_settings.cache_clear()
    backend = openrouter.OpenRouterBackend(api_key="sk-or-test")
    fake_client = _SlowClient()
    backend._client = fake_client

    req = LLMRequest(prompt="hello", max_tokens=4)
    results = await asyncio.gather(*(backend.complete(req) for _ in range(5)))

    assert results == ["ok"] * 5
    assert fake_client.max_seen == 2
    status = openrouter.openrouter_runtime_status()
    assert status["max_concurrency"] == 2
    assert status["active_semaphore_limit"] == 2


def test_openrouter_runtime_status_clamps_invalid_values(monkeypatch):
    monkeypatch.setenv("SKYN3T_OPENROUTER_MAX_CONCURRENCY", "0")
    get_settings.cache_clear()

    status = openrouter.openrouter_runtime_status()

    assert status["max_concurrency"] == 1
    assert status["setting"] == "SKYN3T_OPENROUTER_MAX_CONCURRENCY"


def test_openrouter_reads_max_concurrency_from_settings(monkeypatch):
    import skyn3t.config.settings as settings_module

    monkeypatch.delenv("SKYN3T_OPENROUTER_MAX_CONCURRENCY", raising=False)
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(openrouter_max_concurrency=3),
    )

    assert openrouter.openrouter_max_concurrency() == 3
    assert openrouter.openrouter_runtime_status()["source"] == "settings"
