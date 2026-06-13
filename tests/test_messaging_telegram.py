"""Tests for skyn3t.integrations.messaging.

Pin down the InboundMessage normalization, the Telegram payload shape,
the router lookup, and the FastAPI webhook glue.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.integrations.messaging import (
    MessagingRouter,
    TelegramChannel,
    _strip_case_insensitive,
)

# ─── helpers ───────────────────────────────────────────────────────────


def _capture_task_created(bus: EventBus) -> List[Event]:
    out: List[Event] = []
    bus.subscribe(out.append, EventType.TASK_CREATED)
    return out


# ─── _strip_case_insensitive ───────────────────────────────────────────


def test_strip_case_insensitive_removes_every_occurrence():
    assert _strip_case_insensitive("@Bot hello @bot world", "@bot") == " hello  world"


def test_strip_case_insensitive_with_empty_needle_is_a_noop():
    assert _strip_case_insensitive("hello", "") == "hello"


def test_strip_case_insensitive_no_match():
    assert _strip_case_insensitive("hello world", "@bot") == "hello world"


# ─── TelegramChannel.handle_inbound ────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_basic_text_message_normalizes():
    ch = TelegramChannel(EventBus(), bot_token="t")
    raw = {
        "update_id": 1,
        "message": {
            "message_id": 42,
            "from": {"id": 99, "username": "alice"},
            "chat": {"id": -100123, "type": "supergroup"},
            "date": 1729012345,
            "text": "build me a todo app",
        },
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "telegram"
    assert msg.channel == "-100123"
    assert msg.sender == "99"
    assert msg.text == "build me a todo app"
    assert msg.thread == "42"


@pytest.mark.asyncio
async def test_inbound_strips_bot_mention_in_group(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "skynet_bot")
    ch = TelegramChannel(EventBus(), bot_token="t")
    raw = {
        "message": {
            "message_id": 1,
            "chat": {"id": -1},
            "from": {"id": 1},
            "text": "@skynet_bot hello there",
        }
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.text.strip() == "hello there"


@pytest.mark.asyncio
async def test_inbound_returns_none_for_non_text_payload():
    ch = TelegramChannel(EventBus(), bot_token="t")
    # Sticker update — no text.
    raw = {"message": {"sticker": {"file_id": "abc"}}}
    assert await ch.handle_inbound(raw) is None


@pytest.mark.asyncio
async def test_inbound_returns_none_for_garbage_payload():
    ch = TelegramChannel(EventBus(), bot_token="t")
    assert await ch.handle_inbound({}) is None
    assert await ch.handle_inbound({"update_id": 1}) is None


@pytest.mark.asyncio
async def test_inbound_handles_edited_message_too():
    ch = TelegramChannel(EventBus(), bot_token="t")
    raw = {
        "edited_message": {
            "message_id": 9,
            "chat": {"id": 7},
            "from": {"id": 5},
            "text": "edited text",
        }
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.text == "edited text"


# ─── ingest → TASK_CREATED publish ────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_publishes_task_created_event():
    bus = EventBus()
    captured = _capture_task_created(bus)
    ch = TelegramChannel(bus, bot_token="t")
    await ch.ingest({
        "message": {
            "message_id": 1,
            "chat": {"id": 100},
            "from": {"id": 5, "username": "alice"},
            "text": "do the thing",
        }
    })
    assert len(captured) == 1
    payload = captured[0].payload
    assert payload["platform"] == "telegram"
    assert payload["channel"] == "100"
    assert payload["sender"] == "5"
    assert payload["message"] == "do the thing"
    assert payload["reply_channel"] == "100"


@pytest.mark.asyncio
async def test_ingest_ignored_payload_does_not_publish():
    bus = EventBus()
    captured = _capture_task_created(bus)
    ch = TelegramChannel(bus, bot_token="t")
    await ch.ingest({"message": {"sticker": {}}})  # no text
    assert captured == []


# ─── send (with stubbed httpx) ─────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int = 200, text: str = ""):
        self.status_code = status
        self.text = text


class _FakeHttp:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.status = 200

    async def post(self, url: str, json: Dict[str, Any]):
        self.calls.append({"url": url, "json": json})
        return _FakeResp(status=self.status)

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_send_posts_to_telegram_api():
    ch = TelegramChannel(EventBus(), bot_token="TOKEN")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("100", "hello back", thread="42")
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert "bot" in call["url"] and "TOKEN" in call["url"]
    assert call["json"]["chat_id"] == "100"
    assert call["json"]["text"] == "hello back"
    assert call["json"]["reply_to_message_id"] == 42


@pytest.mark.asyncio
async def test_send_without_token_is_a_noop():
    ch = TelegramChannel(EventBus(), bot_token="")
    ch._http = _FakeHttp()  # type: ignore[assignment]
    await ch.send("100", "hi")
    assert ch._http.calls == []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_send_empty_text_is_a_noop():
    ch = TelegramChannel(EventBus(), bot_token="TOKEN")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("100", "")
    assert fake.calls == []


@pytest.mark.asyncio
async def test_send_drops_thread_when_not_an_int():
    ch = TelegramChannel(EventBus(), bot_token="TOKEN")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("100", "hi", thread="not-a-number")
    assert "reply_to_message_id" not in fake.calls[0]["json"]


# ─── MessagingRouter ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_register_and_lookup():
    rtr = MessagingRouter(EventBus())
    ch = TelegramChannel(rtr.event_bus, bot_token="t")
    rtr.register(ch)
    assert rtr.get("telegram") is ch
    assert rtr.platforms() == ["telegram"]


@pytest.mark.asyncio
async def test_router_reply_routes_to_channel_send():
    rtr = MessagingRouter(EventBus())
    ch = TelegramChannel(rtr.event_bus, bot_token="TOKEN")
    fake = _FakeHttp()
    ch._http = fake
    rtr.register(ch)
    await rtr.reply("telegram", "100", "yo", thread="3")
    assert fake.calls
    assert fake.calls[0]["json"]["chat_id"] == "100"


@pytest.mark.asyncio
async def test_router_reply_on_unknown_platform_is_safe():
    rtr = MessagingRouter(EventBus())
    # No registrations — must not raise.
    await rtr.reply("whatsapp", "100", "yo")


# ─── FastAPI webhook ──────────────────────────────────────────────────


def test_telegram_webhook_route_is_mounted():
    """Just confirm the route appears in the app's routes list. The full
    auth + ingest flow is exercised by direct calls in dedicated tests
    so we don't have to spin up a real httpx mock."""
    from skyn3t.web.app import app
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/webhooks/telegram" in paths


@pytest.mark.parametrize("secret_set", [True, False])
def test_telegram_webhook_fail_closed(secret_set: bool, monkeypatch: pytest.MonkeyPatch):
    """H10: the webhook must reject all traffic when TELEGRAM_WEBHOOK_SECRET is unset,
    and verify the header when it is set."""
    import os

    from fastapi.testclient import TestClient

    from skyn3t.web.app import app

    expected = "super-secret"
    if secret_set:
        monkeypatch.setitem(os.environ, "TELEGRAM_WEBHOOK_SECRET", expected)
    else:
        monkeypatch.setitem(os.environ, "TELEGRAM_WEBHOOK_SECRET", "")

    client = TestClient(app)
    payload = {"update_id": 1, "message": {"message_id": 1, "text": "hi"}}

    if not secret_set:
        resp = client.post("/webhooks/telegram", json=payload)
        assert resp.status_code == 503
        return

    # Wrong secret header → 401.
    resp = client.post(
        "/webhooks/telegram",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 401

    # Correct secret header → accepted (ingest will return 200 even if no channel registered).
    resp = client.post(
        "/webhooks/telegram",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": expected},
    )
    assert resp.status_code in (200, 202)
