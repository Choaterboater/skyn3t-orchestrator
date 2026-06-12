"""Phase 5B — CHANNELS_CORE_OWNER.

Pins the additive availability convention added to
``skyn3t.integrations.messaging``:

  * ``MessagingChannel.is_available()`` default + per-credential overrides
    on every existing channel.
  * ``MessagingRouter.available_platforms()`` returning only the channels
    whose ``is_available()`` is True (additive; ``platforms()`` untouched).

These gates are what the DeliveryGateway and the /status endpoint key off
to decide which channels can actually deliver.

The package ``skyn3t.integrations.__init__`` imports FastAPI routers that
are incompatible with the installed Starlette in this environment (a
pre-existing collection failure unrelated to this owner's files). So this
test loads ``messaging.py`` directly via importlib, bypassing the package
``__init__`` import chain, and never makes a real network call.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from skyn3t.core.events import EventBus


def _load_messaging():
    """Load messaging.py directly, sidestepping the package __init__.

    Registered in ``sys.modules`` under its load name so dataclasses can
    resolve ``cls.__module__`` during class creation.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "skyn3t", "integrations", "messaging.py")
    name = "skyn3t_messaging_phase5b_direct"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


messaging = _load_messaging()


# ─── base default ───────────────────────────────────────────────────────


def test_base_is_available_defaults_true():
    """A subclass that does not override is_available() is available by
    default — the convention is opt-out, not opt-in, for the base."""

    class _Bare(messaging.MessagingChannel):
        platform = "bare"

        async def handle_inbound(self, raw):  # pragma: no cover - trivial
            return None

        async def send(self, channel, text, *, thread=None):  # pragma: no cover
            return None

    assert _Bare(EventBus()).is_available() is True


# ─── per-credential overrides ───────────────────────────────────────────


def test_telegram_availability_tracks_bot_token():
    bus = EventBus()
    assert messaging.TelegramChannel(bus, bot_token="").is_available() is False
    assert messaging.TelegramChannel(bus, bot_token="tok").is_available() is True


def test_whatsapp_requires_both_token_and_phone_id():
    bus = EventBus()
    assert messaging.WhatsAppChannel(
        bus, access_token="", phone_number_id=""
    ).is_available() is False
    assert messaging.WhatsAppChannel(
        bus, access_token="t", phone_number_id=""
    ).is_available() is False
    assert messaging.WhatsAppChannel(
        bus, access_token="", phone_number_id="p"
    ).is_available() is False
    assert messaging.WhatsAppChannel(
        bus, access_token="t", phone_number_id="p"
    ).is_available() is True


def test_matrix_requires_homeserver_and_token():
    bus = EventBus()
    assert messaging.MatrixChannel(
        bus, homeserver_url="", access_token=""
    ).is_available() is False
    assert messaging.MatrixChannel(
        bus, homeserver_url="https://m.example.org", access_token="t"
    ).is_available() is True


def test_signal_requires_bridge_and_number():
    bus = EventBus()
    assert messaging.SignalChannel(
        bus, bridge_url="", number=""
    ).is_available() is False
    assert messaging.SignalChannel(
        bus, bridge_url="http://localhost:8080", number="+15551234567"
    ).is_available() is True


def test_imessage_requires_bridge_and_password():
    bus = EventBus()
    assert messaging.IMessageChannel(
        bus, bridge_url="", password=""
    ).is_available() is False
    assert messaging.IMessageChannel(
        bus, bridge_url="http://192.168.1.50:1234", password="pw"
    ).is_available() is True


def test_msteams_requires_app_id_and_password():
    bus = EventBus()
    assert messaging.MSTeamsChannel(
        bus, app_id="", app_password=""
    ).is_available() is False
    assert messaging.MSTeamsChannel(
        bus, app_id="id", app_password="pw"
    ).is_available() is True


def test_mattermost_requires_incoming_webhook_url():
    bus = EventBus()
    assert messaging.MattermostChannel(
        bus, incoming_webhook_url=""
    ).is_available() is False
    assert messaging.MattermostChannel(
        bus, incoming_webhook_url="https://mm.example.org/hooks/abc"
    ).is_available() is True


def test_feishu_requires_app_id_and_secret():
    bus = EventBus()
    assert messaging.FeishuChannel(
        bus, app_id="", app_secret=""
    ).is_available() is False
    assert messaging.FeishuChannel(
        bus, app_id="id", app_secret="secret"
    ).is_available() is True


def test_generic_webhook_requires_outbound_url():
    bus = EventBus()
    assert messaging.GenericWebhookChannel(
        bus, platform_name="sms", outbound_url=""
    ).is_available() is False
    assert messaging.GenericWebhookChannel(
        bus, platform_name="sms", outbound_url="https://gw.example.org/out"
    ).is_available() is True


# ─── router.available_platforms() ───────────────────────────────────────


def test_available_platforms_filters_to_configured_channels():
    bus = EventBus()
    router = messaging.MessagingRouter(bus)
    router.register(messaging.TelegramChannel(bus, bot_token="tok"))  # available
    router.register(messaging.SignalChannel(bus, bridge_url="", number=""))  # not
    router.register(
        messaging.FeishuChannel(bus, app_id="id", app_secret="sec")
    )  # available

    # platforms() lists everything registered, unchanged behaviour.
    assert router.platforms() == ["feishu", "signal", "telegram"]
    # available_platforms() filters to those whose creds are present.
    assert router.available_platforms() == ["feishu", "telegram"]


def test_available_platforms_empty_when_nothing_configured():
    bus = EventBus()
    router = messaging.MessagingRouter(bus)
    router.register(messaging.TelegramChannel(bus, bot_token=""))
    router.register(messaging.WhatsAppChannel(bus, access_token="", phone_number_id=""))
    assert router.platforms() == ["telegram", "whatsapp"]
    assert router.available_platforms() == []


def test_available_platforms_never_raises_on_bad_channel():
    """A channel whose is_available() blows up is treated as unavailable,
    not propagated — the gateway/status endpoint poll this freely."""
    bus = EventBus()
    router = messaging.MessagingRouter(bus)

    class _Boom(messaging.MessagingChannel):
        platform = "boom"

        async def handle_inbound(self, raw):  # pragma: no cover - trivial
            return None

        async def send(self, channel, text, *, thread=None):  # pragma: no cover
            return None

        def is_available(self) -> bool:
            raise RuntimeError("creds backend offline")

    router.register(_Boom(bus))
    router.register(messaging.TelegramChannel(bus, bot_token="tok"))
    # Does not raise; the broken channel is simply excluded.
    assert router.available_platforms() == ["telegram"]


def test_default_router_singleton_has_available_platforms():
    """The process-wide router exposes the new method too."""
    router = messaging.get_default_router(EventBus())
    assert hasattr(router, "available_platforms")
    assert isinstance(router.available_platforms(), list)


# ─── env-driven default construction stays unavailable when unset ───────


def test_env_unset_channels_report_unavailable(monkeypatch):
    """Constructed with no kwargs and no env vars, credential-gated
    channels report unavailable rather than crashing on construction."""
    for var in (
        "TELEGRAM_BOT_TOKEN",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "MATRIX_HOMESERVER_URL",
        "MATRIX_ACCESS_TOKEN",
        "SIGNAL_BRIDGE_URL",
        "SIGNAL_NUMBER",
        "BLUEBUBBLES_URL",
        "BLUEBUBBLES_PASSWORD",
        "MSTEAMS_APP_ID",
        "MSTEAMS_APP_PASSWORD",
        "MATTERMOST_INCOMING_WEBHOOK_URL",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    bus = EventBus()
    assert messaging.TelegramChannel(bus).is_available() is False
    assert messaging.WhatsAppChannel(bus).is_available() is False
    assert messaging.MatrixChannel(bus).is_available() is False
    assert messaging.SignalChannel(bus).is_available() is False
    assert messaging.IMessageChannel(bus).is_available() is False
    assert messaging.MSTeamsChannel(bus).is_available() is False
    assert messaging.MattermostChannel(bus).is_available() is False
    assert messaging.FeishuChannel(bus).is_available() is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
