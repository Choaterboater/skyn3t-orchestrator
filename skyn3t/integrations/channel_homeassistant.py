"""Home Assistant notify-service channel for SkyN3t.

Home Assistant (HA) is the dominant open-source home-automation hub.
It exposes a REST API; the canonical way to push a text message at a
user is to call a ``notify`` service — e.g. ``notify.mobile_app_alice``
(the HA Companion app), ``notify.persistent_notification`` (the
dashboard banner), or any notify integration the operator has wired
(SMTP, Telegram-via-HA, TTS speakers, etc).

This channel is **outbound-first**: the primary parity feature is
"send my agent's reply / a scheduled briefing to my house". HA can
also push inbound events (automations firing a webhook at us), so we
normalize a simple inbound shape too, but the bread-and-butter is
``send()`` -> ``POST /api/services/notify/<service>``.

Wire shape (outbound):
    POST {HASS_URL}/api/services/notify/<service>
    Authorization: Bearer {HASS_TOKEN}
    {"message": "<text>", "title": "<optional>"}

The ``channel`` argument selects the notify service. Operators pass
either the bare service name (``mobile_app_alice``) or the dotted form
(``notify.mobile_app_alice``); both are accepted. When ``channel`` is
empty we fall back to ``HASS_DEFAULT_NOTIFY`` (default
``persistent_notification``) so a no-target broadcast still lands
somewhere visible.

Env config:
  HASS_URL             — base URL, e.g. http://homeassistant.local:8123
  HASS_TOKEN           — long-lived access token (Profile -> Long-Lived
                         Access Tokens in the HA UI). Required to send.
  HASS_DEFAULT_NOTIFY  — optional; notify service used when no channel
                         is given (default 'persistent_notification').

is_available() is the additive gate the gateway + status endpoint key
off: True only when both HASS_URL and HASS_TOKEN are present. Missing
creds => adapter disabled, send() is a quiet no-op, never a crash.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from skyn3t.core.events import EventBus
from skyn3t.integrations.messaging import InboundMessage, MessagingChannel

logger = logging.getLogger("skyn3t.integrations.channel_homeassistant")


class HomeAssistantChannel(MessagingChannel):
    """Home Assistant ``notify`` service channel."""

    platform = "homeassistant"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        default_notify: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.base_url = (base_url or os.getenv("HASS_URL", "") or "").rstrip("/")
        self.token = token or os.getenv("HASS_TOKEN", "")
        self.default_notify = (
            default_notify
            or os.getenv("HASS_DEFAULT_NOTIFY", "")
            or "persistent_notification"
        )
        # Lazy httpx — importing this module must never require httpx.
        self._http: Any = None

    # ── availability gate ──────────────────────────────────────────────

    def is_available(self) -> bool:
        """True when HA base URL + token are both configured.

        Pure / non-raising: the gateway and status endpoint call this to
        decide whether to surface the channel. No creds => disabled.
        """
        return bool(self.base_url and self.token)

    # ── inbound (HA automation -> our webhook) ─────────────────────────

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize an inbound payload pushed by a Home Assistant
        automation (HA's ``rest_command`` / webhook actions).

        HA gives operators full control over the body, so we accept a
        small, forgiving shape. The conventional fields:

            {
              "message": "garage door left open",   # the user/automation text
              "service": "mobile_app_alice",         # optional: where to reply
              "sender": "automation.garage_watch",   # optional: who fired it
              "context_id": "01H..."                 # optional: HA context id
            }

        ``message`` is required; everything else degrades gracefully.
        Returns None (ignore) when there's no text.
        """
        if not isinstance(raw, dict):
            return None
        text = str(raw.get("message") or raw.get("text") or "").strip()
        if not text:
            return None
        # Reply target: the notify service to answer on. Falls back to
        # the default so a reply still lands somewhere.
        channel = str(raw.get("service") or raw.get("channel") or "").strip()
        if not channel:
            channel = self.default_notify
        sender = str(raw.get("sender") or raw.get("entity_id") or "homeassistant")
        thread = raw.get("context_id") or raw.get("thread")
        return InboundMessage(
            platform=self.platform,
            channel=channel,
            sender=sender,
            text=text,
            thread=str(thread) if thread else None,
            raw=raw,
        )

    # ── outbound (notify service call) ─────────────────────────────────

    @staticmethod
    def _notify_service(channel: str) -> str:
        """Normalize a channel id into a bare notify service name.

        Accepts 'mobile_app_alice' and 'notify.mobile_app_alice'
        identically; the REST path always uses the bare name.
        """
        service = (channel or "").strip()
        if service.startswith("notify."):
            service = service[len("notify.") :]
        return service

    async def send(
        self,
        channel: str,
        text: str,
        *,
        thread: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        """Call a Home Assistant notify service with ``text``.

        ``channel`` selects the notify service (bare or dotted form);
        empty falls back to ``HASS_DEFAULT_NOTIFY``. ``thread`` is
        unused by HA notify (no native threading) and accepted for
        interface symmetry. ``title`` maps to the notify title field.
        """
        if not self.is_available():
            logger.warning(
                "HomeAssistant send skipped: HASS_URL / HASS_TOKEN unset"
            )
            return
        if not text:
            return
        service = self._notify_service(channel) or self.default_notify
        if not service:
            logger.warning("HomeAssistant send skipped: no notify service")
            return
        if self._http is None:
            try:
                import httpx  # type: ignore

                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("HomeAssistant send: httpx unavailable")
                return
        url = f"{self.base_url}/api/services/notify/{service}"
        body: Dict[str, Any] = {"message": text}
        if title:
            body["title"] = title
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            resp = await self._http.post(url, json=body, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "HomeAssistant send %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.exception("HomeAssistant send failed")

    async def shutdown(self) -> None:
        """Close the HTTP client. Idempotent / safe to call repeatedly."""
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
