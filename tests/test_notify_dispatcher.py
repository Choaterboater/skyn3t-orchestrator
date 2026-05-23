"""Tests for the approval-gate notification dispatcher (Discord only in v1)."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from skyn3t.studio import notify_dispatcher


class _FakeResponse:
    def __init__(self, status_code: int = 204):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, posts: list[dict[str, Any]], raise_error: bool = False):
        self._posts = posts
        self._raise = raise_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url: str, json: Dict[str, Any]):
        if self._raise:
            raise RuntimeError("network down")
        self._posts.append({"url": url, "json": json})
        return _FakeResponse(204)


@pytest.fixture(autouse=True)
def _reset_throttle():
    notify_dispatcher._last_dispatch.clear()
    yield
    notify_dispatcher._last_dispatch.clear()


@pytest.mark.asyncio
async def test_discord_dispatch_posts_payload(monkeypatch):
    posts: list[dict[str, Any]] = []

    def fake_client(*args, **kwargs):
        return _FakeAsyncClient(posts)

    monkeypatch.setattr(notify_dispatcher.httpx, "AsyncClient", fake_client)
    cfg = {"notify": {"discord_webhook": "https://example.test/hook/abc"}}
    result = await notify_dispatcher.dispatch(
        "gate-1", "ArchitectAgent", "/studio/gate-1", cfg
    )
    assert result == {"discord": True}
    assert len(posts) == 1
    sent = posts[0]
    assert sent["url"] == "https://example.test/hook/abc"
    assert "gate-1" in sent["json"]["content"]
    assert "ArchitectAgent" in sent["json"]["content"]
    assert sent["json"]["embeds"][0]["url"] == "/studio/gate-1"


@pytest.mark.asyncio
async def test_empty_webhook_silent_no_op(monkeypatch):
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notify_dispatcher.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(posts)
    )
    cfg = {"notify": {"discord_webhook": ""}}
    result = await notify_dispatcher.dispatch(
        "gate-1", "ArchitectAgent", "/studio/gate-1", cfg
    )
    assert result == {}
    assert posts == []


@pytest.mark.asyncio
async def test_throttle_blocks_duplicate_within_60s(monkeypatch):
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notify_dispatcher.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(posts)
    )
    cfg = {"notify": {"discord_webhook": "https://example.test/hook"}}
    first = await notify_dispatcher.dispatch(
        "gate-1", "ArchitectAgent", "/studio/gate-1", cfg
    )
    second = await notify_dispatcher.dispatch(
        "gate-1", "ArchitectAgent", "/studio/gate-1", cfg
    )
    assert first == {"discord": True}
    assert second.get("throttled") is True
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_http_exception_suppressed(monkeypatch):
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notify_dispatcher.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeAsyncClient(posts, raise_error=True),
    )
    cfg = {"notify": {"discord_webhook": "https://example.test/hook"}}
    result = await notify_dispatcher.dispatch(
        "gate-1", "ArchitectAgent", "/studio/gate-1", cfg
    )
    assert result == {"discord": False}


@pytest.mark.asyncio
async def test_clarification_dispatch_posts_payload(monkeypatch):
    posts: list[dict[str, Any]] = []

    def fake_client(*args, **kwargs):
        return _FakeAsyncClient(posts)

    monkeypatch.setattr(notify_dispatcher.httpx, "AsyncClient", fake_client)
    cfg = {"notify": {"discord_webhook": "https://example.test/hook/clarify"}}
    result = await notify_dispatcher.dispatch_clarification(
        "clarify-1",
        "BrainstormAgent",
        "/studio/clarify-1",
        ["Who is this for?", "Do you need accounts?"],
        cfg,
    )
    assert result == {"discord": True}
    assert len(posts) == 1
    sent = posts[0]
    assert sent["url"] == "https://example.test/hook/clarify"
    assert "clarify-1" in sent["json"]["content"]
    assert "needs clarification" in sent["json"]["content"]
    assert "Who is this for?" in sent["json"]["embeds"][0]["description"]
