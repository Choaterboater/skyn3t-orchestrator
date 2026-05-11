"""Tests for the Mattermost, Feishu, and GenericWebhook channels.

These three round out the long-tail platforms — Mattermost is the
Slack-shaped self-host alternative, Feishu represents the Tencent /
Lark / WeCom family, and GenericWebhookChannel is the catch-all
adapter for SMS gateways, Home Assistant, and anything else that
talks JSON over HTTP.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.integrations.messaging import (
    FeishuChannel,
    GenericWebhookChannel,
    MattermostChannel,
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
        self.responses: Dict[str, _Resp] = {}
        self.default_response = _Resp(status=200)

    async def post(self, url, json=None, data=None, headers=None):
        self.calls.append({
            "method": "POST", "url": url, "json": json, "data": data,
            "headers": headers or {},
        })
        for marker, resp in self.responses.items():
            if marker in url:
                return resp
        return self.default_response

    async def aclose(self):
        pass


# ─── Mattermost ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mattermost_inbound_normalizes_outgoing_webhook():
    ch = MattermostChannel(EventBus(), incoming_webhook_url="http://mm/hook")
    raw = {
        "channel_id": "c123",
        "user_id": "u456",
        "user_name": "alice",
        "text": "build me a thing",
        "post_id": "p789",
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "mattermost"
    assert msg.channel == "c123"
    assert msg.sender == "u456"
    assert msg.text == "build me a thing"
    assert msg.thread == "p789"


@pytest.mark.asyncio
async def test_mattermost_inbound_strips_trigger_word():
    """When a trigger_word fired the outgoing webhook, strip it so the
    agent sees a clean prompt."""
    ch = MattermostChannel(EventBus(), incoming_webhook_url="http://mm")
    raw = {
        "channel_id": "c",
        "user_id": "u",
        "text": "skyn3t deploy the app",
        "trigger_word": "skyn3t",
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.text == "deploy the app"


@pytest.mark.asyncio
async def test_mattermost_inbound_skips_empty_after_trigger_strip():
    ch = MattermostChannel(EventBus(), incoming_webhook_url="http://mm")
    raw = {"channel_id": "c", "user_id": "u", "text": "skyn3t", "trigger_word": "skyn3t"}
    assert await ch.handle_inbound(raw) is None


@pytest.mark.asyncio
async def test_mattermost_send_posts_to_incoming_webhook():
    ch = MattermostChannel(
        EventBus(),
        incoming_webhook_url="http://mm/hooks/abc",
        username="skyn3t",
    )
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("c123", "ack")
    call = fake.calls[0]
    assert call["url"] == "http://mm/hooks/abc"
    assert call["json"]["text"] == "ack"
    assert call["json"]["channel"] == "c123"
    assert call["json"]["username"] == "skyn3t"


@pytest.mark.asyncio
async def test_mattermost_send_noop_without_webhook_url():
    ch = MattermostChannel(EventBus(), incoming_webhook_url="")
    ch._http = _FakeHttp()
    await ch.send("c", "x")
    assert ch._http.calls == []  # type: ignore[union-attr]


# ─── Feishu / Lark ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feishu_inbound_normalizes_text_message_event():
    ch = FeishuChannel(EventBus(), app_id="a", app_secret="b")
    raw = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {
                "message_id": "om_xyz",
                "chat_id": "oc_chat",
                "content": '{"text": "build me a thing"}',
                "message_type": "text",
            },
        },
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "feishu"
    assert msg.channel == "oc_chat"
    assert msg.sender == "ou_abc"
    assert msg.text == "build me a thing"
    assert msg.thread == "om_xyz"


@pytest.mark.asyncio
async def test_feishu_inbound_skips_wrong_event_type():
    ch = FeishuChannel(EventBus(), app_id="a", app_secret="b")
    raw = {
        "header": {"event_type": "im.chat.member.user.added_v1"},  # not a message
        "event": {},
    }
    assert await ch.handle_inbound(raw) is None


@pytest.mark.asyncio
async def test_feishu_inbound_skips_non_text_messages():
    ch = FeishuChannel(EventBus(), app_id="a", app_secret="b")
    raw = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"message": {"message_type": "image", "content": "{}"}},
    }
    assert await ch.handle_inbound(raw) is None


@pytest.mark.asyncio
async def test_feishu_inbound_handles_malformed_content_json():
    """If content isn't JSON-parseable, drop the event quietly — never raise."""
    ch = FeishuChannel(EventBus(), app_id="a", app_secret="b")
    raw = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"message": {"message_type": "text", "content": "{not json"}},
    }
    assert await ch.handle_inbound(raw) is None


@pytest.mark.asyncio
async def test_feishu_send_acquires_tenant_token_then_posts():
    ch = FeishuChannel(EventBus(), app_id="appid", app_secret="appsecret")
    fake = _FakeHttp()
    fake.responses["tenant_access_token/internal"] = _Resp(
        json_body={"code": 0, "tenant_access_token": "fake-token", "expire": 7200},
    )
    ch._http = fake
    await ch.send("oc_chat", "ack")
    assert len(fake.calls) == 2  # token, then message
    token_call, msg_call = fake.calls
    assert "tenant_access_token" in token_call["url"]
    assert "im/v1/messages" in msg_call["url"]
    body = msg_call["json"]
    assert body["receive_id"] == "oc_chat"
    assert body["msg_type"] == "text"
    # Content is JSON-encoded per Feishu spec.
    import json as _json
    assert _json.loads(body["content"]) == {"text": "ack"}
    assert msg_call["headers"]["Authorization"] == "Bearer fake-token"


@pytest.mark.asyncio
async def test_feishu_send_caches_token_across_sends():
    ch = FeishuChannel(EventBus(), app_id="a", app_secret="b")
    fake = _FakeHttp()
    fake.responses["tenant_access_token/internal"] = _Resp(
        json_body={"code": 0, "tenant_access_token": "t", "expire": 3600},
    )
    ch._http = fake
    await ch.send("oc1", "one")
    await ch.send("oc2", "two")
    token_calls = [c for c in fake.calls if "tenant_access_token" in c["url"]]
    assert len(token_calls) == 1


@pytest.mark.asyncio
async def test_feishu_send_handles_token_error_code():
    """Feishu wraps errors in {code: !=0, msg: ...}. Treat that as no token."""
    ch = FeishuChannel(EventBus(), app_id="a", app_secret="b")
    fake = _FakeHttp()
    fake.responses["tenant_access_token/internal"] = _Resp(
        json_body={"code": 99991663, "msg": "app not found"},
    )
    ch._http = fake
    await ch.send("oc", "x")
    # No message POST should have happened — only the token fetch.
    msg_calls = [c for c in fake.calls if "im/v1/messages" in c["url"]]
    assert msg_calls == []


# ─── GenericWebhookChannel ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generic_webhook_inbound_uses_configured_field_names():
    ch = GenericWebhookChannel(
        EventBus(),
        platform_name="twilio_sms",
        outbound_url="http://twilio/api/messages",
        text_field="Body",
        channel_field="From",
        sender_field="From",
        thread_field="MessageSid",
    )
    raw = {
        "Body": "deploy the bot",
        "From": "+15551112222",
        "MessageSid": "SM123",
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "twilio_sms"
    assert msg.channel == "+15551112222"
    assert msg.sender == "+15551112222"
    assert msg.text == "deploy the bot"
    assert msg.thread == "SM123"


@pytest.mark.asyncio
async def test_generic_webhook_inbound_returns_none_on_empty_text():
    ch = GenericWebhookChannel(
        EventBus(), platform_name="x", outbound_url="http://o",
    )
    assert await ch.handle_inbound({"text": "", "channel": "c"}) is None
    assert await ch.handle_inbound({}) is None


@pytest.mark.asyncio
async def test_generic_webhook_send_interpolates_template():
    """Operators can shape the outbound body to whatever the bridge wants."""
    ch = GenericWebhookChannel(
        EventBus(),
        platform_name="custom",
        outbound_url="http://bridge",
        outbound_template={
            "to": "{channel}",
            "body": "{text}",
            "reply_to": "{thread}",
            "from": "skyn3t",  # literal — no placeholder
        },
        auth_headers={"X-Api-Key": "secret"},
    )
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("+15551112222", "hi", thread="msg-1")
    body = fake.calls[0]["json"]
    assert body["to"] == "+15551112222"
    assert body["body"] == "hi"
    assert body["reply_to"] == "msg-1"
    assert body["from"] == "skyn3t"
    assert fake.calls[0]["headers"]["X-Api-Key"] == "secret"


@pytest.mark.asyncio
async def test_generic_webhook_send_noop_without_outbound_url():
    ch = GenericWebhookChannel(
        EventBus(), platform_name="x", outbound_url="",
    )
    ch._http = _FakeHttp()
    await ch.send("c", "x")
    assert ch._http.calls == []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_generic_webhook_send_empty_thread_substitutes_blank():
    ch = GenericWebhookChannel(
        EventBus(),
        platform_name="custom",
        outbound_url="http://b",
        outbound_template={"text": "{text}", "thread": "{thread}"},
    )
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("c", "hi")  # thread=None
    assert fake.calls[0]["json"]["thread"] == ""


# ─── Cross-channel publish ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_channel_publishes_task_created():
    bus = EventBus()
    captured: List[Any] = []
    bus.subscribe(captured.append, EventType.TASK_CREATED)

    mm = MattermostChannel(bus, incoming_webhook_url="http://mm")
    fs = FeishuChannel(bus, app_id="a", app_secret="b")
    gw = GenericWebhookChannel(bus, platform_name="custom", outbound_url="http://o")

    await mm.ingest({
        "channel_id": "c", "user_id": "u", "text": "mm msg",
    })
    await fs.ingest({
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou"}},
            "message": {"chat_id": "oc", "content": '{"text":"fs msg"}', "message_type": "text"},
        },
    })
    await gw.ingest({"text": "gw msg", "channel": "c", "sender": "u"})

    platforms = {e.payload["platform"] for e in captured}
    texts = {e.payload["message"] for e in captured}
    assert platforms == {"mattermost", "feishu", "custom"}
    assert texts == {"mm msg", "fs msg", "gw msg"}
