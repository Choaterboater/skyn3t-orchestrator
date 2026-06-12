"""Tests for the Western Phase 5B channels: Home Assistant + SMS (Twilio).

HomeAssistantChannel pushes agent replies / briefings at a HA ``notify``
service (Companion app, persistent notification, TTS speaker, ...). The
SmsChannel sends/receives SMS via Twilio's REST API. Both are opt-in,
credential-gated, and degrade to a quiet no-op when their env creds are
absent — never a crash, never a real network call (tests inject a fake
HTTP client).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from skyn3t.core.events import EventBus, EventType
from skyn3t.integrations.channel_homeassistant import HomeAssistantChannel
from skyn3t.integrations.channel_sms import SmsChannel


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
        self.closed = False

    async def post(self, url, json=None, data=None, headers=None):
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "json": json,
                "data": data,
                "headers": headers or {},
            }
        )
        for marker, resp in self.responses.items():
            if marker in url:
                return resp
        return self.default_response

    async def aclose(self):
        self.closed = True


# ─── Home Assistant ────────────────────────────────────────────────────


def test_homeassistant_is_available_requires_url_and_token(monkeypatch):
    monkeypatch.delenv("HASS_URL", raising=False)
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    assert HomeAssistantChannel(EventBus()).is_available() is False
    assert (
        HomeAssistantChannel(EventBus(), base_url="http://ha:8123").is_available()
        is False
    )
    assert HomeAssistantChannel(EventBus(), token="t").is_available() is False
    assert (
        HomeAssistantChannel(
            EventBus(), base_url="http://ha:8123", token="t"
        ).is_available()
        is True
    )


def test_homeassistant_reads_env_creds(monkeypatch):
    monkeypatch.setenv("HASS_URL", "http://homeassistant.local:8123/")
    monkeypatch.setenv("HASS_TOKEN", "llat-token")
    ch = HomeAssistantChannel(EventBus())
    # Trailing slash stripped.
    assert ch.base_url == "http://homeassistant.local:8123"
    assert ch.token == "llat-token"
    assert ch.is_available() is True


@pytest.mark.asyncio
async def test_homeassistant_send_posts_to_notify_service():
    ch = HomeAssistantChannel(
        EventBus(), base_url="http://ha:8123", token="tok"
    )
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("mobile_app_alice", "garage is open", title="Alert")
    call = fake.calls[0]
    assert call["url"] == "http://ha:8123/api/services/notify/mobile_app_alice"
    assert call["json"] == {"message": "garage is open", "title": "Alert"}
    assert call["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_homeassistant_send_accepts_dotted_service_name():
    ch = HomeAssistantChannel(EventBus(), base_url="http://ha:8123", token="t")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("notify.persistent_notification", "hi")
    assert (
        fake.calls[0]["url"]
        == "http://ha:8123/api/services/notify/persistent_notification"
    )


@pytest.mark.asyncio
async def test_homeassistant_send_falls_back_to_default_notify(monkeypatch):
    monkeypatch.delenv("HASS_DEFAULT_NOTIFY", raising=False)
    ch = HomeAssistantChannel(EventBus(), base_url="http://ha:8123", token="t")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("", "broadcast text")
    assert (
        fake.calls[0]["url"]
        == "http://ha:8123/api/services/notify/persistent_notification"
    )


@pytest.mark.asyncio
async def test_homeassistant_send_respects_custom_default_notify():
    ch = HomeAssistantChannel(
        EventBus(),
        base_url="http://ha:8123",
        token="t",
        default_notify="all_devices",
    )
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("", "x")
    assert fake.calls[0]["url"].endswith("/notify/all_devices")


@pytest.mark.asyncio
async def test_homeassistant_send_noop_without_creds():
    ch = HomeAssistantChannel(EventBus(), base_url="", token="")
    ch._http = _FakeHttp()
    await ch.send("mobile_app_alice", "x")
    assert ch._http.calls == []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_homeassistant_send_noop_on_empty_text():
    ch = HomeAssistantChannel(EventBus(), base_url="http://ha:8123", token="t")
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("mobile_app_alice", "")
    assert fake.calls == []


@pytest.mark.asyncio
async def test_homeassistant_inbound_normalizes_automation_payload():
    ch = HomeAssistantChannel(EventBus(), base_url="http://ha:8123", token="t")
    raw = {
        "message": "front door unlocked",
        "service": "mobile_app_alice",
        "sender": "automation.door_watch",
        "context_id": "01HCTX",
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "homeassistant"
    assert msg.channel == "mobile_app_alice"
    assert msg.sender == "automation.door_watch"
    assert msg.text == "front door unlocked"
    assert msg.thread == "01HCTX"


@pytest.mark.asyncio
async def test_homeassistant_inbound_defaults_reply_service():
    ch = HomeAssistantChannel(EventBus(), base_url="http://ha:8123", token="t")
    msg = await ch.handle_inbound({"message": "ping"})
    assert msg is not None
    assert msg.channel == "persistent_notification"
    assert msg.sender == "homeassistant"
    assert msg.thread is None


@pytest.mark.asyncio
async def test_homeassistant_inbound_ignores_empty_message():
    ch = HomeAssistantChannel(EventBus(), base_url="http://ha:8123", token="t")
    assert await ch.handle_inbound({"message": ""}) is None
    assert await ch.handle_inbound({}) is None


@pytest.mark.asyncio
async def test_homeassistant_shutdown_is_idempotent():
    ch = HomeAssistantChannel(EventBus(), base_url="http://ha:8123", token="t")
    fake = _FakeHttp()
    ch._http = fake
    await ch.shutdown()
    assert fake.closed is True
    assert ch._http is None
    await ch.shutdown()  # no crash on second call


# ─── SMS (Twilio) ──────────────────────────────────────────────────────


def test_sms_is_available_requires_sid_token_and_from(monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_FROM", raising=False)
    assert SmsChannel(EventBus()).is_available() is False
    assert (
        SmsChannel(EventBus(), account_sid="AC1", auth_token="tok").is_available()
        is False
    )
    assert (
        SmsChannel(
            EventBus(), account_sid="AC1", auth_token="tok", from_number="+1555"
        ).is_available()
        is True
    )


def test_sms_reads_env_creds(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACabc")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_FROM", "+15559998888")
    ch = SmsChannel(EventBus())
    assert ch.account_sid == "ACabc"
    assert ch.auth_token == "secret"
    assert ch.from_number == "+15559998888"
    assert ch.is_available() is True


@pytest.mark.asyncio
async def test_sms_send_posts_form_encoded_with_basic_auth():
    ch = SmsChannel(
        EventBus(),
        account_sid="ACabc",
        auth_token="secret",
        from_number="+15559998888",
    )
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("+15551112222", "deploy done")
    call = fake.calls[0]
    assert (
        call["url"]
        == "https://api.twilio.com/2010-04-01/Accounts/ACabc/Messages.json"
    )
    # Form-encoded body, not JSON.
    assert call["json"] is None
    assert call["data"] == {
        "From": "+15559998888",
        "To": "+15551112222",
        "Body": "deploy done",
    }
    # HTTP Basic auth = base64(SID:TOKEN).
    import base64 as _b64

    expected = "Basic " + _b64.b64encode(b"ACabc:secret").decode("ascii")
    assert call["headers"]["Authorization"] == expected
    assert (
        call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    )


@pytest.mark.asyncio
async def test_sms_send_noop_without_creds():
    ch = SmsChannel(EventBus(), account_sid="", auth_token="", from_number="")
    ch._http = _FakeHttp()
    await ch.send("+15551112222", "x")
    assert ch._http.calls == []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_sms_send_noop_on_empty_channel_or_text():
    ch = SmsChannel(
        EventBus(), account_sid="AC", auth_token="t", from_number="+1555"
    )
    fake = _FakeHttp()
    ch._http = fake
    await ch.send("", "hi")
    await ch.send("+15551112222", "")
    assert fake.calls == []


@pytest.mark.asyncio
async def test_sms_inbound_normalizes_twilio_webhook():
    ch = SmsChannel(
        EventBus(), account_sid="AC", auth_token="t", from_number="+1555"
    )
    raw = {
        "MessageSid": "SM123",
        "From": "+15551112222",
        "To": "+15559998888",
        "Body": "deploy the bot",
        "NumMedia": "0",
    }
    msg = await ch.handle_inbound(raw)
    assert msg is not None
    assert msg.platform == "sms"
    # Reply target is the sender's number.
    assert msg.channel == "+15551112222"
    assert msg.sender == "+15551112222"
    assert msg.text == "deploy the bot"
    assert msg.thread == "SM123"


@pytest.mark.asyncio
async def test_sms_inbound_ignores_status_callback_without_body():
    ch = SmsChannel(
        EventBus(), account_sid="AC", auth_token="t", from_number="+1555"
    )
    # A delivery/status callback carries no Body.
    assert await ch.handle_inbound({"MessageSid": "SM", "From": "+1555"}) is None
    # Body present but no From => can't reply, ignore.
    assert await ch.handle_inbound({"Body": "hi"}) is None


@pytest.mark.asyncio
async def test_sms_shutdown_is_idempotent():
    ch = SmsChannel(
        EventBus(), account_sid="AC", auth_token="t", from_number="+1555"
    )
    fake = _FakeHttp()
    ch._http = fake
    await ch.shutdown()
    assert fake.closed is True
    assert ch._http is None
    await ch.shutdown()


# ─── Shared base behavior: ingest publishes TASK_CREATED ───────────────


@pytest.mark.asyncio
async def test_both_channels_publish_task_created_via_base_ingest():
    bus = EventBus()
    captured: List[Any] = []
    bus.subscribe(captured.append, EventType.TASK_CREATED)

    ha = HomeAssistantChannel(bus, base_url="http://ha:8123", token="t")
    sms = SmsChannel(bus, account_sid="AC", auth_token="t", from_number="+1555")

    await ha.ingest({"message": "ha event", "service": "mobile_app_alice"})
    await sms.ingest(
        {"Body": "sms event", "From": "+15551112222", "MessageSid": "SM"}
    )

    platforms = {e.payload["platform"] for e in captured}
    texts = {e.payload["message"] for e in captured}
    assert platforms == {"homeassistant", "sms"}
    assert texts == {"ha event", "sms event"}


# ─── Public export surface (__init__.py) ───────────────────────────────


def test_new_channels_exported_from_integrations_package():
    import skyn3t.integrations as integrations

    assert integrations.HomeAssistantChannel is HomeAssistantChannel
    assert integrations.SmsChannel is SmsChannel
    assert "HomeAssistantChannel" in integrations.__all__
    assert "SmsChannel" in integrations.__all__
