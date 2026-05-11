"""Tests for the WhatsApp + Matrix MessagingChannels.

Same pattern as the Telegram tests: stub out httpx, exercise the
inbound parser + outbound payload shape against the real provider
schemas.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.integrations.messaging import MatrixChannel, WhatsAppChannel


class _FakeResp:
    def __init__(self, status: int = 200, text: str = ""):
        self.status_code = status
        self.text = text


class _FakeHttp:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.status = 200

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers or {}})
        return _FakeResp(status=self.status)

    async def put(self, url: str, json=None, headers=None):
        self.calls.append({"method": "PUT", "url": url, "json": json, "headers": headers or {}})
        return _FakeResp(status=self.status)

    async def aclose(self):
        pass


# ─── WhatsApp ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whatsapp_inbound_normalizes_text_message():
    ch = WhatsAppChannel(EventBus(), access_token="t", phone_number_id="123")
    raw = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": "15555550100",
                        "id": "wamid.abc",
                        "text": {"body": "build me a thing"},
                    }]
                }
            }]
        }]
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "whatsapp"
    assert msg.channel == "15555550100"
    assert msg.sender == "15555550100"
    assert msg.text == "build me a thing"
    assert msg.thread == "wamid.abc"


@pytest.mark.asyncio
async def test_whatsapp_inbound_skips_non_text_messages():
    ch = WhatsAppChannel(EventBus(), access_token="t", phone_number_id="123")
    raw = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{"from": "1", "id": "wamid.x", "image": {"id": "img"}}]
                }
            }]
        }]
    }
    assert await ch.handle_inbound(raw) is None


@pytest.mark.asyncio
async def test_whatsapp_inbound_handles_empty_envelope():
    ch = WhatsAppChannel(EventBus(), access_token="t", phone_number_id="123")
    assert await ch.handle_inbound({}) is None
    assert await ch.handle_inbound({"entry": []}) is None


@pytest.mark.asyncio
async def test_whatsapp_send_posts_to_graph_api():
    ch = WhatsAppChannel(EventBus(), access_token="TOKEN", phone_number_id="555")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("15555550100", "yo", thread="wamid.original")
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert "graph.facebook.com" in call["url"]
    assert "/555/messages" in call["url"]
    assert call["json"]["to"] == "15555550100"
    assert call["json"]["type"] == "text"
    assert call["json"]["text"]["body"] == "yo"
    assert call["json"]["messaging_product"] == "whatsapp"
    assert call["json"]["context"]["message_id"] == "wamid.original"
    assert call["headers"]["Authorization"] == "Bearer TOKEN"


@pytest.mark.asyncio
async def test_whatsapp_send_noop_without_credentials():
    ch = WhatsAppChannel(EventBus(), access_token="", phone_number_id="")
    ch._http = _FakeHttp()
    await ch.send("15555550100", "yo")
    assert ch._http.calls == []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_whatsapp_send_drops_thread_field_when_absent():
    ch = WhatsAppChannel(EventBus(), access_token="T", phone_number_id="5")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("15555550100", "hi")
    assert "context" not in fake.calls[0]["json"]


# ─── Matrix ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_matrix_inbound_normalizes_room_message():
    ch = MatrixChannel(
        EventBus(),
        homeserver_url="https://matrix.example.org",
        access_token="t",
    )
    raw = {
        "event_id": "$xyz",
        "room_id": "!abc:example.org",
        "sender": "@alice:example.org",
        "type": "m.room.message",
        "content": {"msgtype": "m.text", "body": "deploy the bot"},
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "matrix"
    assert msg.channel == "!abc:example.org"
    assert msg.sender == "@alice:example.org"
    assert msg.text == "deploy the bot"
    assert msg.thread == "$xyz"


@pytest.mark.asyncio
async def test_matrix_inbound_skips_non_message_events():
    ch = MatrixChannel(EventBus(), homeserver_url="https://m", access_token="t")
    # Membership change — not a message we should respond to.
    assert await ch.handle_inbound({"type": "m.room.member"}) is None


@pytest.mark.asyncio
async def test_matrix_inbound_skips_non_text_message_types():
    """Notice events, images, files — all ignored for now."""
    ch = MatrixChannel(EventBus(), homeserver_url="https://m", access_token="t")
    raw = {
        "type": "m.room.message",
        "content": {"msgtype": "m.image", "body": "screenshot.png", "url": "mxc://x"},
    }
    assert await ch.handle_inbound(raw) is None


@pytest.mark.asyncio
async def test_matrix_send_puts_with_unique_transaction_id():
    ch = MatrixChannel(
        EventBus(),
        homeserver_url="https://matrix.example.org",
        access_token="TOKEN",
    )
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("!room:example.org", "ack")
    await ch.send("!room:example.org", "ack again")
    assert len(fake.calls) == 2
    # Two distinct txn ids → two distinct URLs.
    assert fake.calls[0]["url"] != fake.calls[1]["url"]
    # Method is PUT (per the Matrix spec).
    assert fake.calls[0]["method"] == "PUT"
    # Body shape.
    assert fake.calls[0]["json"]["msgtype"] == "m.text"
    assert fake.calls[0]["json"]["body"] == "ack"


@pytest.mark.asyncio
async def test_matrix_send_threads_via_m_relates_to():
    ch = MatrixChannel(
        EventBus(),
        homeserver_url="https://matrix.example.org",
        access_token="TOKEN",
    )
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("!room:example.org", "reply", thread="$root_event")
    rel = fake.calls[0]["json"].get("m.relates_to") or {}
    assert rel.get("rel_type") == "m.thread"
    assert rel.get("event_id") == "$root_event"


@pytest.mark.asyncio
async def test_matrix_send_noop_without_credentials():
    ch = MatrixChannel(EventBus(), homeserver_url="", access_token="")
    ch._http = _FakeHttp()
    await ch.send("!room:example.org", "yo")
    assert ch._http.calls == []  # type: ignore[union-attr]


# ─── Cross-channel: every new channel still publishes TASK_CREATED ────


@pytest.mark.asyncio
async def test_every_channel_routes_through_ingest_to_event_bus():
    """The whole point of MessagingChannel.ingest is that any platform
    pushes the same TASK_CREATED shape onto the bus. Pin that down
    across all three new channels."""
    bus = EventBus()
    captured: List[Event] = []
    bus.subscribe(captured.append, EventType.TASK_CREATED)
    wa = WhatsAppChannel(bus, access_token="t", phone_number_id="5")
    mx = MatrixChannel(bus, homeserver_url="https://m", access_token="t")
    await wa.ingest({
        "entry": [{"changes": [{"value": {"messages": [{
            "from": "1", "id": "wamid.x", "text": {"body": "wa msg"},
        }]}}]}]
    })
    await mx.ingest({
        "type": "m.room.message",
        "event_id": "$1",
        "room_id": "!r:e",
        "sender": "@a:e",
        "content": {"msgtype": "m.text", "body": "mx msg"},
    })
    platforms = [e.payload.get("platform") for e in captured]
    texts = [e.payload.get("message") for e in captured]
    assert set(platforms) == {"whatsapp", "matrix"}
    assert "wa msg" in texts
    assert "mx msg" in texts
