"""Exact-match LLM response cache: deterministic (temp==0) calls are replayed
from cache; sampled (temp>0) calls always hit the backend."""

import asyncio

from skyn3t.adapters import llm_client
from skyn3t.adapters.llm_client import LLMClient


class _CountingBackend:
    def __init__(self):
        self.calls = 0
        self._last_usage = {}

    async def complete(self, req):
        self.calls += 1
        return "RESULT"


def _client_with(monkeypatch, backend):
    monkeypatch.setenv("SKYN3T_LLM_RESPONSE_CACHE", "1")
    llm_client._RESPONSE_CACHE.clear()
    llm_client._RESPONSE_CACHE_ORDER.clear()
    client = LLMClient(backend="openrouter")

    async def _fake_get_impl():
        return backend

    monkeypatch.setattr(client, "_get_impl", _fake_get_impl)
    return client


def test_response_cache_short_circuits_temp0(monkeypatch):
    backend = _CountingBackend()
    client = _client_with(monkeypatch, backend)

    async def run():
        a = await client.complete("hello", system="sys", temperature=0)
        b = await client.complete("hello", system="sys", temperature=0)
        return a, b

    a, b = asyncio.run(run())
    assert a == b == "RESULT"
    assert backend.calls == 1  # second call served from cache


def test_response_cache_skips_sampled_calls(monkeypatch):
    backend = _CountingBackend()
    client = _client_with(monkeypatch, backend)

    async def run():
        await client.complete("hi", temperature=0.4)
        await client.complete("hi", temperature=0.4)

    asyncio.run(run())
    assert backend.calls == 2  # non-deterministic results are never cached


def test_response_cache_can_be_disabled(monkeypatch):
    backend = _CountingBackend()
    client = _client_with(monkeypatch, backend)
    monkeypatch.setenv("SKYN3T_LLM_RESPONSE_CACHE", "0")

    async def run():
        await client.complete("x", temperature=0)
        await client.complete("x", temperature=0)

    asyncio.run(run())
    assert backend.calls == 2
