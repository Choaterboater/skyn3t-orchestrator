"""Tests for skyn3t.integrations.gateway (Phase 5B, owner GATEWAY).

Pins down the DeliveryGateway contract:

* deliver() routes through MessagingRouter.reply() for registered channels.
* unconfigured / unknown / unavailable channel => DeliveryResult(skipped=True),
  never an error, never a raise.
* channels() reflects router.available_platforms() when present.
* broadcast() is independent-per-channel.
* the scheduled-job event bridge extracts payload['delivery'] and delivers.

No real network: a fake MessagingChannel records sends in-process.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Tuple

import pytest

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.integrations.gateway import (
    DeliveryGateway,
    DeliveryResult,
    extract_delivery,
    get_gateway,
    make_scheduled_delivery_bridge,
    register_scheduled_delivery_bridge,
)

# ─── fakes ─────────────────────────────────────────────────────────────


class FakeChannel:
    """Minimal MessagingChannel stand-in. Records every send() call."""

    def __init__(self, platform: str, *, available: bool = True, raises: bool = False):
        self.platform = platform
        self._available = available
        self._raises = raises
        self.sends: List[Tuple[str, str, Optional[str]]] = []

    def is_available(self) -> bool:
        return self._available

    async def send(self, channel: str, text: str, *, thread: Optional[str] = None) -> None:
        if self._raises:
            raise RuntimeError("boom")
        self.sends.append((channel, text, thread))


class FakeRouter:
    """Router stand-in exposing get()/platforms()/available_platforms()/reply()."""

    def __init__(self) -> None:
        self._channels: Dict[str, FakeChannel] = {}

    def register(self, ch: FakeChannel) -> None:
        self._channels[ch.platform] = ch

    def get(self, platform: str) -> Optional[FakeChannel]:
        return self._channels.get(platform)

    def platforms(self) -> List[str]:
        return sorted(self._channels)

    def available_platforms(self) -> List[str]:
        return sorted(p for p, c in self._channels.items() if c.is_available())

    async def reply(self, platform: str, channel: str, text: str, *, thread=None) -> None:
        ch = self._channels.get(platform)
        if ch is None:
            return
        await ch.send(channel, text, thread=thread)


def _gateway_with(*channels: FakeChannel) -> Tuple[DeliveryGateway, FakeRouter]:
    router = FakeRouter()
    for ch in channels:
        router.register(ch)
    return DeliveryGateway(router=router), router  # type: ignore[arg-type]


# ─── DeliveryResult ────────────────────────────────────────────────────


def test_delivery_result_to_dict_round_trips():
    r = DeliveryResult(channel="telegram", ok=True)
    assert r.to_dict() == {
        "channel": "telegram",
        "ok": True,
        "error": None,
        "skipped": False,
    }


# ─── deliver(): happy path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_routes_through_router_reply():
    tg = FakeChannel("telegram")
    gw, _ = _gateway_with(tg)
    res = await gw.deliver(channel="telegram", to="123", text="hi", thread="t1")
    assert res.ok is True
    assert res.skipped is False
    assert res.error is None
    assert tg.sends == [("123", "hi", "t1")]


@pytest.mark.asyncio
async def test_deliver_is_case_insensitive_on_channel_name():
    tg = FakeChannel("telegram")
    gw, _ = _gateway_with(tg)
    res = await gw.deliver(channel="Telegram", to="123", text="hi")
    assert res.ok is True
    assert tg.sends == [("123", "hi", None)]


# ─── deliver(): skip semantics (never raise) ───────────────────────────


@pytest.mark.asyncio
async def test_deliver_unknown_channel_is_skipped_not_error():
    gw, _ = _gateway_with()  # empty router
    res = await gw.deliver(channel="nope", to="x", text="hi")
    assert res.skipped is True
    assert res.ok is False
    assert res.error is None


@pytest.mark.asyncio
async def test_deliver_unavailable_channel_is_skipped():
    tg = FakeChannel("telegram", available=False)
    gw, _ = _gateway_with(tg)
    res = await gw.deliver(channel="telegram", to="123", text="hi")
    assert res.skipped is True
    assert res.ok is False
    assert tg.sends == []  # never attempted


@pytest.mark.asyncio
async def test_deliver_empty_text_is_skipped():
    tg = FakeChannel("telegram")
    gw, _ = _gateway_with(tg)
    res = await gw.deliver(channel="telegram", to="123", text="")
    assert res.skipped is True
    assert tg.sends == []


@pytest.mark.asyncio
async def test_deliver_empty_channel_is_skipped():
    gw, _ = _gateway_with()
    res = await gw.deliver(channel="", to="123", text="hi")
    assert res.skipped is True
    assert res.ok is False


@pytest.mark.asyncio
async def test_deliver_send_exception_becomes_error_not_raise():
    tg = FakeChannel("telegram", raises=True)
    gw, _ = _gateway_with(tg)
    res = await gw.deliver(channel="telegram", to="123", text="hi")
    assert res.ok is False
    assert res.skipped is False
    assert res.error and "boom" in res.error


# ─── channels() ────────────────────────────────────────────────────────


def test_channels_reflects_available_platforms(monkeypatch):
    # Ensure bridge envs are off so only router channels show.
    for var in ("EMAIL_SMTP_HOST", "EMAIL_ADDRESS", "EMAIL_PASSWORD",
                "DISCORD_WEBHOOK", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)
    up = FakeChannel("telegram", available=True)
    down = FakeChannel("slack", available=False)
    gw, _ = _gateway_with(up, down)
    assert gw.channels() == ["telegram"]


def test_channels_includes_email_bridge_when_configured(monkeypatch):
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_ADDRESS", "bot@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.delenv("DISCORD_WEBHOOK", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    gw, _ = _gateway_with(FakeChannel("telegram"))
    assert "email" in gw.channels()


# ─── broadcast() ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broadcast_independent_per_channel():
    tg = FakeChannel("telegram")
    sl = FakeChannel("slack", raises=True)
    gw, _ = _gateway_with(tg, sl)
    results = await gw.broadcast(
        channels=["telegram", "slack", "missing"],
        to_map={"telegram": "1", "slack": "2"},
        text="hello",
    )
    by_chan = {r.channel: r for r in results}
    assert by_chan["telegram"].ok is True
    assert by_chan["slack"].ok is False and by_chan["slack"].error
    assert by_chan["missing"].skipped is True
    assert tg.sends == [("1", "hello", None)]


@pytest.mark.asyncio
async def test_broadcast_channel_without_destination_is_skipped():
    tg = FakeChannel("telegram")
    gw, _ = _gateway_with(tg)
    results = await gw.broadcast(channels=["telegram"], to_map={}, text="hi")
    assert results[0].skipped is True
    assert tg.sends == []


# ─── extract_delivery() ────────────────────────────────────────────────


def test_extract_delivery_from_inner_job_payload():
    payload = {
        "kind": "scheduled_job_triggered",
        "name": "daily",
        "payload": {"agent_name": "x", "delivery": {"channel": "telegram", "to": "42"}},
    }
    assert extract_delivery(payload) == {"channel": "telegram", "to": "42"}


def test_extract_delivery_from_top_level():
    payload = {"kind": "x", "delivery": {"channel": "slack", "to": "C1"}}
    assert extract_delivery(payload) == {"channel": "slack", "to": "C1"}


def test_extract_delivery_none_when_absent_or_incomplete():
    assert extract_delivery({"kind": "x"}) is None
    assert extract_delivery({"delivery": {"channel": "slack"}}) is None  # no 'to'
    assert extract_delivery({"delivery": {"to": "C1"}}) is None  # no channel
    assert extract_delivery(None) is None  # type: ignore[arg-type]


# ─── scheduled-job → delivery bridge ───────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_delivers_scheduled_job_with_delivery():
    tg = FakeChannel("telegram")
    gw, _ = _gateway_with(tg)
    bridge = make_scheduled_delivery_bridge(gw)
    event = Event(
        event_type=EventType.SYSTEM_ALERT,
        source="scheduler_agent",
        payload={
            "kind": "scheduled_job_triggered",
            "name": "morning briefing",
            "task_type": "schedule_nl",
            "payload": {
                "result": "Good morning!",
                "delivery": {"channel": "telegram", "to": "42"},
            },
        },
    )
    bridge(event)
    # Bridge schedules on the running loop; yield so the task runs.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert tg.sends == [("42", "Good morning!", None)]


@pytest.mark.asyncio
async def test_bridge_ignores_events_without_delivery():
    tg = FakeChannel("telegram")
    gw, _ = _gateway_with(tg)
    bridge = make_scheduled_delivery_bridge(gw)
    bridge(Event(
        event_type=EventType.SYSTEM_ALERT,
        source="scheduler_agent",
        payload={"kind": "scheduled_job_triggered", "name": "no-delivery"},
    ))
    await asyncio.sleep(0)
    assert tg.sends == []


@pytest.mark.asyncio
async def test_bridge_ignores_unrelated_event_kinds():
    tg = FakeChannel("telegram")
    gw, _ = _gateway_with(tg)
    bridge = make_scheduled_delivery_bridge(gw)
    bridge(Event(
        event_type=EventType.SYSTEM_ALERT,
        source="other",
        payload={"kind": "something_else", "delivery": {"channel": "telegram", "to": "x"}},
    ))
    await asyncio.sleep(0)
    assert tg.sends == []


def test_bridge_never_raises_on_garbage_event():
    gw, _ = _gateway_with(FakeChannel("telegram"))
    bridge = make_scheduled_delivery_bridge(gw)
    # Event with a non-dict payload-ish shape — must not raise.
    bridge(Event(event_type=EventType.SYSTEM_ALERT, source="x", payload={}))
    bridge(Event(event_type=EventType.TASK_CREATED, source="x", payload={"kind": "scheduled_job_triggered"}))


def test_register_bridge_subscribes_and_is_unsubscribable():
    bus = EventBus()
    gw, _ = _gateway_with(FakeChannel("telegram"))
    cb = register_scheduled_delivery_bridge(bus, gw)
    assert callable(cb)
    # Unsubscribe should not raise.
    bus.unsubscribe(cb, EventType.SYSTEM_ALERT)


def test_bridge_runs_outside_event_loop_via_asyncio_run():
    """When no loop is running, the bridge drives deliver() to completion."""
    tg = FakeChannel("telegram")
    gw, _ = _gateway_with(tg)
    bridge = make_scheduled_delivery_bridge(gw)
    bridge(Event(
        event_type=EventType.SYSTEM_ALERT,
        source="scheduler_agent",
        payload={
            "kind": "scheduled_job_triggered",
            "name": "briefing",
            "payload": {"result": "done", "delivery": {"channel": "telegram", "to": "9"}},
        },
    ))
    assert tg.sends == [("9", "done", None)]


# ─── singleton ─────────────────────────────────────────────────────────


def test_get_gateway_is_singleton():
    assert get_gateway() is get_gateway()
