"""Tests for the Signal, iMessage, and Microsoft Teams channels."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.integrations.messaging import (
    IMessageChannel,
    MSTeamsChannel,
    SignalChannel,
)


class _Resp:
    def __init__(self, status: int = 200, text: str = "", json_body: Any = None):
        self.status_code = status
        self.text = text
        self._json = json_body

    def json(self):
        return self._json or {}


class _FakeHttp:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        # Each entry can have its own canned response via .responses.
        self.responses: Dict[str, _Resp] = {}
        self.default_response = _Resp(status=200)

    async def post(self, url, json=None, data=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "data": data, "headers": headers or {}})
        for marker, resp in self.responses.items():
            if marker in url:
                return resp
        return self.default_response

    async def put(self, url, json=None, headers=None):
        self.calls.append({"method": "PUT", "url": url, "json": json, "headers": headers or {}})
        return self.default_response

    async def aclose(self):
        pass


# ─── Signal ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signal_inbound_normalizes_direct_message():
    ch = SignalChannel(EventBus(), bridge_url="http://b", number="+15559999999")
    raw = {
        "envelope": {
            "source": "+15551112222",
            "sourceName": "Alice",
            "timestamp": 1729012345000,
            "dataMessage": {"message": "hi from signal"},
        },
        "account": "+15559999999",
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "signal"
    assert msg.channel == "+15551112222"
    assert msg.sender == "+15551112222"
    assert msg.text == "hi from signal"
    assert msg.thread == "1729012345000"


@pytest.mark.asyncio
async def test_signal_inbound_uses_group_id_as_channel():
    ch = SignalChannel(EventBus(), bridge_url="http://b", number="+15559999999")
    raw = {
        "envelope": {
            "source": "+15551112222",
            "dataMessage": {
                "message": "group hi",
                "groupInfo": {"groupId": "abc=", "type": "DELIVER"},
            },
        }
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.channel == "abc="          # group id, not sender
    assert msg.sender == "+15551112222"   # sender still preserved


@pytest.mark.asyncio
async def test_signal_inbound_skips_typing_and_receipts():
    ch = SignalChannel(EventBus(), bridge_url="http://b", number="+15559999999")
    # Typing notification — no dataMessage.
    assert await ch.handle_inbound({"envelope": {"source": "+1", "typingMessage": {}}}) is None
    # Empty body.
    assert await ch.handle_inbound({"envelope": {"source": "+1", "dataMessage": {"message": ""}}}) is None


@pytest.mark.asyncio
async def test_signal_send_posts_to_v2_send():
    ch = SignalChannel(EventBus(), bridge_url="http://b", number="+15559999999")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("+15551112222", "ack")
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"].endswith("/v2/send")
    assert call["json"]["message"] == "ack"
    assert call["json"]["number"] == "+15559999999"
    assert call["json"]["recipients"] == ["+15551112222"]


@pytest.mark.asyncio
async def test_signal_send_treats_group_ids_distinctly():
    ch = SignalChannel(EventBus(), bridge_url="http://b", number="+15559999999")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("group-abc", "ack")
    body = fake.calls[0]["json"]
    # Bridge versions vary; we always populate group_id for non-E.164 targets.
    assert body.get("group_id") == "group-abc"


@pytest.mark.asyncio
async def test_signal_send_noop_without_credentials():
    ch = SignalChannel(EventBus(), bridge_url="", number="")
    ch._http = _FakeHttp()
    await ch.send("+1", "x")
    assert ch._http.calls == []  # type: ignore[union-attr]


# ─── iMessage (BlueBubbles) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_imessage_inbound_normalizes_new_message():
    ch = IMessageChannel(EventBus(), bridge_url="http://b", password="p")
    raw = {
        "type": "new-message",
        "data": {
            "guid": "iMessage;-;msg123",
            "text": "yo from imessage",
            "chats": [{"guid": "iMessage;-;chat456"}],
            "handle": {"address": "+15551234567"},
            "isFromMe": False,
        },
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "imessage"
    assert msg.channel == "iMessage;-;chat456"
    assert msg.sender == "+15551234567"
    assert msg.text == "yo from imessage"
    assert msg.thread == "iMessage;-;msg123"


@pytest.mark.asyncio
async def test_imessage_inbound_skips_echoes_from_self():
    """If the bridge forwards a message the bot itself sent, isFromMe=true
    — we must not loop on our own output."""
    ch = IMessageChannel(EventBus(), bridge_url="http://b", password="p")
    raw = {
        "type": "new-message",
        "data": {
            "guid": "g",
            "text": "echo of our own send",
            "chats": [{"guid": "c"}],
            "handle": {"address": "+1"},
            "isFromMe": True,
        },
    }
    assert await ch.handle_inbound(raw) is None


@pytest.mark.asyncio
async def test_imessage_inbound_skips_non_message_webhook_types():
    ch = IMessageChannel(EventBus(), bridge_url="http://b", password="p")
    # Typing indicator from BlueBubbles.
    assert await ch.handle_inbound({"type": "typing", "data": {}}) is None
    # Empty text.
    raw = {
        "type": "new-message",
        "data": {"guid": "g", "text": "", "chats": [{"guid": "c"}], "isFromMe": False},
    }
    assert await ch.handle_inbound(raw) is None


@pytest.mark.asyncio
async def test_imessage_send_includes_chat_guid_and_temp_guid():
    ch = IMessageChannel(EventBus(), bridge_url="http://b", password="hunter2")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("iMessage;-;chat456", "ack")
    call = fake.calls[0]
    assert "/api/v1/message/text" in call["url"]
    assert "password=hunter2" in call["url"]
    assert call["json"]["chatGuid"] == "iMessage;-;chat456"
    assert call["json"]["message"] == "ack"
    assert call["json"]["tempGuid"].startswith("temp-")
    assert len(call["json"]["tempGuid"]) > 10  # non-trivial hash


@pytest.mark.asyncio
async def test_imessage_send_temp_guid_changes_per_message():
    """Two sends to the same chat must produce different tempGuids so
    BlueBubbles doesn't dedupe them."""
    import time as _time
    ch = IMessageChannel(EventBus(), bridge_url="http://b", password="p")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("c", "one")
    _time.sleep(0.002)  # ms granularity in the seed
    await ch.send("c", "two")
    assert fake.calls[0]["json"]["tempGuid"] != fake.calls[1]["json"]["tempGuid"]


@pytest.mark.asyncio
async def test_imessage_send_noop_without_credentials():
    ch = IMessageChannel(EventBus(), bridge_url="", password="")
    ch._http = _FakeHttp()
    await ch.send("c", "x")
    assert ch._http.calls == []  # type: ignore[union-attr]


# ─── Microsoft Teams ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_teams_inbound_strips_at_mention_tags():
    ch = MSTeamsChannel(EventBus(), app_id="a", app_password="p")
    raw = {
        "type": "message",
        "id": "1700000001",
        "text": "<at>SkyN3t</at> deploy the bot",
        "from": {"id": "29:abc", "name": "Alice"},
        "conversation": {"id": "19:xyz@thread.tacv2"},
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "msteams"
    assert msg.channel == "19:xyz@thread.tacv2"
    assert msg.text == "deploy the bot"
    assert msg.thread == "1700000001"


@pytest.mark.asyncio
async def test_teams_inbound_skips_non_message_activities():
    ch = MSTeamsChannel(EventBus(), app_id="a", app_password="p")
    assert await ch.handle_inbound({"type": "conversationUpdate"}) is None
    assert await ch.handle_inbound({"type": "typing"}) is None
    # Empty text after mention strip.
    assert await ch.handle_inbound({"type": "message", "text": "<at>SkyN3t</at>"}) is None


@pytest.mark.asyncio
async def test_teams_send_acquires_token_then_posts_to_conversation():
    ch = MSTeamsChannel(EventBus(), app_id="appid", app_password="appsecret")
    fake = _FakeHttp()
    # Stub the token endpoint to return a valid access_token.
    fake.responses["oauth2/v2.0/token"] = _Resp(
        status=200, json_body={"access_token": "fake-jwt", "expires_in": 3600},
    )
    ch._http = fake

    await ch.send("19:room@thread.tacv2", "ack", thread="1700000001")
    # One POST to the token endpoint, one POST to the conversation activities.
    assert len(fake.calls) == 2
    token_call, activity_call = fake.calls
    assert "oauth2/v2.0/token" in token_call["url"]
    assert "activities" in activity_call["url"]
    assert activity_call["json"]["text"] == "ack"
    assert activity_call["json"]["replyToId"] == "1700000001"
    assert activity_call["headers"]["Authorization"] == "Bearer fake-jwt"


@pytest.mark.asyncio
async def test_teams_send_caches_token_across_calls():
    ch = MSTeamsChannel(EventBus(), app_id="appid", app_password="appsecret")
    fake = _FakeHttp()
    fake.responses["oauth2/v2.0/token"] = _Resp(
        status=200, json_body={"access_token": "tok", "expires_in": 3600},
    )
    ch._http = fake
    await ch.send("c1", "one")
    await ch.send("c2", "two")
    # Token fetched once; two activity posts.
    token_calls = [c for c in fake.calls if "oauth2/v2.0/token" in c["url"]]
    assert len(token_calls) == 1


@pytest.mark.asyncio
async def test_teams_send_noop_without_credentials():
    ch = MSTeamsChannel(EventBus(), app_id="", app_password="")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("c", "x")
    # Token request happens lazily and returns None when creds missing;
    # send must not actually POST any activity.
    activity_calls = [c for c in fake.calls if "activities" in c["url"]]
    assert activity_calls == []


# ─── Cross-channel ingest publishes TASK_CREATED ──────────────────────


@pytest.mark.asyncio
async def test_each_new_channel_publishes_task_created():
    bus = EventBus()
    captured: List[Any] = []
    bus.subscribe(captured.append, EventType.TASK_CREATED)

    signal_ch = SignalChannel(bus, bridge_url="http://b", number="+15559999999")
    imsg_ch = IMessageChannel(bus, bridge_url="http://b", password="p")
    teams_ch = MSTeamsChannel(bus, app_id="a", app_password="p")

    await signal_ch.ingest({
        "envelope": {
            "source": "+15551112222",
            "timestamp": 1,
            "dataMessage": {"message": "sig"},
        }
    })
    await imsg_ch.ingest({
        "type": "new-message",
        "data": {
            "guid": "g", "text": "imsg",
            "chats": [{"guid": "c"}],
            "handle": {"address": "+1"},
            "isFromMe": False,
        },
    })
    await teams_ch.ingest({
        "type": "message",
        "id": "1",
        "text": "teams",
        "from": {"id": "u"},
        "conversation": {"id": "c1"},
    })

    platforms = {e.payload["platform"] for e in captured}
    texts = {e.payload["message"] for e in captured}
    assert platforms == {"signal", "imessage", "msteams"}
    assert texts == {"sig", "imsg", "teams"}
