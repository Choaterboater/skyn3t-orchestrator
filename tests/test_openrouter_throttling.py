from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from skyn3t.adapters import openrouter
from skyn3t.adapters.llm_client import LLMRequest, TransientLLMError
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
    openrouter._throttle_floor = 0
    openrouter._throttle_until = 0.0
    yield
    get_settings.cache_clear()
    openrouter._request_semaphore = None
    openrouter._request_semaphore_limit = None
    openrouter._request_semaphore_loop_id = None
    openrouter._throttle_floor = 0
    openrouter._throttle_until = 0.0


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


# ── PHASE 2: typed transient error on exhausted retries ──────────────────────


class _Resp:
    def __init__(self, status_code, *, headers=None, body=None, raise_exc=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_exhausted_429_raises_transient_not_sentinel(monkeypatch):
    monkeypatch.setenv("SKYN3T_OPENROUTER_MAX_RETRIES", "2")
    backend = openrouter.OpenRouterBackend(api_key="sk-or-test")

    class _AlwaysThrottled:
        async def post(self, *_a, **_k):
            # retry-after 0 keeps the test fast.
            return _Resp(429, headers={"retry-after": "0"})

        async def aclose(self):
            return None

    backend._client = _AlwaysThrottled()
    with pytest.raises(TransientLLMError):
        await backend.complete(LLMRequest(prompt="x", max_tokens=4))


@pytest.mark.asyncio
async def test_exhausted_5xx_raises_transient(monkeypatch):
    monkeypatch.setenv("SKYN3T_OPENROUTER_MAX_RETRIES", "2")
    backend = openrouter.OpenRouterBackend(api_key="sk-or-test")

    class _AlwaysOverloaded:
        async def post(self, *_a, **_k):
            return _Resp(503, headers={"retry-after": "0"})

        async def aclose(self):
            return None

    backend._client = _AlwaysOverloaded()
    with pytest.raises(TransientLLMError):
        await backend.complete(LLMRequest(prompt="x", max_tokens=4))


@pytest.mark.asyncio
async def test_permanent_403_raises_non_transient(monkeypatch):
    monkeypatch.setenv("SKYN3T_OPENROUTER_MAX_RETRIES", "2")
    backend = openrouter.OpenRouterBackend(api_key="sk-or-test")

    import httpx

    def _make_403():
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        response = httpx.Response(403, request=request)
        return _Resp(
            403,
            raise_exc=httpx.HTTPStatusError(
                "Key limit exceeded", request=request, response=response
            ),
        )

    class _KeyExhausted:
        async def post(self, *_a, **_k):
            return _make_403()

        async def aclose(self):
            return None

    backend._client = _KeyExhausted()
    # 403 is PERMANENT — it must NOT become a TransientLLMError (so llm_client
    # keeps returning the deterministic-stub sentinel for genuine no-key).
    with pytest.raises(httpx.HTTPStatusError):
        await backend.complete(LLMRequest(prompt="x", max_tokens=4))


@pytest.mark.asyncio
async def test_transient_429_then_success_returns_content(monkeypatch):
    monkeypatch.setenv("SKYN3T_OPENROUTER_MAX_RETRIES", "4")
    backend = openrouter.OpenRouterBackend(api_key="sk-or-test")

    class _ThrottleThenOk:
        def __init__(self):
            self.calls = 0

        async def post(self, *_a, **_k):
            self.calls += 1
            if self.calls < 3:
                return _Resp(429, headers={"retry-after": "0"})
            return _Resp(
                200,
                body={
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "choices": [{"message": {"content": "recovered"}}],
                },
            )

        async def aclose(self):
            return None

    backend._client = _ThrottleThenOk()
    out = await backend.complete(LLMRequest(prompt="x", max_tokens=4))
    assert out == "recovered"


# ── PHASE 3: adaptive 429-aware concurrency ──────────────────────────────────


@pytest.mark.asyncio
async def test_429_cooldown_shrinks_then_restores(monkeypatch):
    monkeypatch.setenv("SKYN3T_OPENROUTER_MAX_CONCURRENCY", "4")
    monkeypatch.setattr(openrouter, "_THROTTLE_COOLDOWN_SECONDS", 60.0)
    get_settings.cache_clear()

    assert openrouter._effective_concurrency() == 4

    # First 429 halves 4 -> 2 (floor 1).
    openrouter._note_throttle_if_429(429)
    assert openrouter._effective_concurrency() == 2

    # The lazy semaphore rebinds to the reduced cap.
    sem = openrouter._get_request_semaphore()
    assert sem is not None
    assert openrouter._request_semaphore_limit == 2

    # A second 429 halves again 2 -> 1 (floor).
    openrouter._note_throttle_if_429(429)
    assert openrouter._effective_concurrency() == 1

    # A non-429 status is a no-op for the cooldown.
    openrouter._note_throttle_if_429(503)
    assert openrouter._effective_concurrency() == 1

    # Cooldown expiry restores the full configured cap.
    openrouter._throttle_until = openrouter._now() - 1.0
    assert openrouter._effective_concurrency() == 4
    assert openrouter.openrouter_runtime_status()["throttle_floor"] is None


@pytest.mark.asyncio
async def test_cooldown_disabled_keeps_configured_cap(monkeypatch):
    monkeypatch.setenv("SKYN3T_OPENROUTER_MAX_CONCURRENCY", "4")
    monkeypatch.setattr(openrouter, "_THROTTLE_COOLDOWN_SECONDS", 0.0)
    get_settings.cache_clear()

    openrouter._note_throttle_if_429(429)
    # Cooldown disabled -> no shrink.
    assert openrouter._effective_concurrency() == 4
