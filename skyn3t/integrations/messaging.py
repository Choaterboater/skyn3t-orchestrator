"""Unified messaging-channel abstraction for SkyN3t.

Hermes ships 18 platforms (Telegram, WhatsApp, Signal, iMessage, Matrix,
Slack, Discord, Teams, etc.). We had four hand-rolled bots, each with
its own glue code. Adding a new platform was a 200-line task.

This module gives every platform the same shape:

  class TelegramChannel(MessagingChannel):
      platform = "telegram"
      async def handle_inbound(self, raw: dict) -> InboundMessage: ...
      async def send(self, to: str, text: str, *, thread: str | None = None) -> None: ...

The orchestrator-facing layer (``MessagingRouter``) takes any
MessagingChannel, normalizes the inbound message into a
``TASK_CREATED`` event, and posts the agent's response back via the
channel's ``send``. So a new platform = one class + one ``register()``
call, no orchestrator changes.

Two transport patterns supported:

  - **Webhook**: the platform POSTs to a FastAPI route we expose. Used
    by Telegram (webhook mode), Slack Events API, Discord interactions,
    WhatsApp Cloud API, Matrix appservices.
  - **Polling/long-running**: the channel maintains its own client
    loop (Discord WebSocket, Slack Socket Mode, IMAP). Already covered
    by the existing slack_bot/discord_bot/email_agent classes —
    they conform to the same shape but own their lifecycle.

This file ships the shared base + a Telegram webhook implementation as
the first new platform.
"""

from __future__ import annotations

import abc
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from skyn3t.core.events import Event, EventBus, EventType

logger = logging.getLogger("skyn3t.integrations.messaging")


@dataclass
class InboundMessage:
    """Platform-neutral inbound message shape.

    Every MessagingChannel.handle_inbound returns one of these (or None
    when the inbound was something we want to ignore — typing-indicator,
    delivery receipt, etc).
    """

    platform: str             # 'telegram', 'whatsapp', etc.
    channel: str              # platform-specific destination id (chat_id, channel_id)
    sender: str               # platform-specific sender id (user_id, msisdn)
    text: str                 # the actual user message
    thread: Optional[str] = None  # platform-specific thread / reply key
    raw: Dict[str, Any] = field(default_factory=dict)  # full raw payload for debugging


class MessagingChannel(abc.ABC):
    """Base for every platform integration.

    Subclasses MUST implement ``handle_inbound`` and ``send``. The base
    handles event-bus publishing — subclasses just normalize their
    platform's wire shape.
    """

    platform: str = "unknown"

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus

    @abc.abstractmethod
    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Parse a platform-native payload into a unified InboundMessage.

        Return None for events we want to ignore (read receipts, typing,
        edits if not relevant, etc).
        """

    @abc.abstractmethod
    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        """Post ``text`` to ``channel`` on this platform.

        ``thread`` is the platform's reply/thread key when applicable
        (Slack thread_ts, Telegram reply_to_message_id, etc).
        """

    async def ingest(self, raw: Dict[str, Any]) -> None:
        """Convenience: parse a raw payload + publish the TASK_CREATED
        event if the payload was a user-message we should respond to.

        The orchestrator's existing message → agent flow takes it from
        there. The channel's ``send`` gets wired by MessagingRouter to
        receive the agent's reply.
        """
        msg = await self.handle_inbound(raw)
        if msg is None:
            return
        self.event_bus.publish(
            Event(
                event_type=EventType.TASK_CREATED,
                source=f"{self.platform}_channel",
                payload={
                    "platform": msg.platform,
                    "channel": msg.channel,
                    "sender": msg.sender,
                    "message": msg.text,
                    "thread": msg.thread,
                    # Allow the orchestrator to call back via the router.
                    "reply_channel": msg.channel,
                    "reply_thread": msg.thread,
                },
            )
        )


# ── Telegram (webhook mode) ────────────────────────────────────────────


class TelegramChannel(MessagingChannel):
    """Telegram Bot API channel.

    Designed for webhook mode: the bot is configured (via
    ``setWebhook``) to POST every update to ``/webhooks/telegram`` on
    the FastAPI app, which calls ``channel.ingest(payload)``.

    Polling mode is intentionally not implemented here — the long-poll
    loop adds another async task that's prone to hangs and dropped
    updates. Webhooks are simpler, faster, and the modern default.

    Env config:
      TELEGRAM_BOT_TOKEN   — required for sending replies.
      TELEGRAM_BOT_USERNAME — optional; when set, strips '@botname'
                              from group chat messages so the agent
                              sees clean prompts.
    """

    platform = "telegram"

    def __init__(self, event_bus: EventBus, *, bot_token: Optional[str] = None):
        super().__init__(event_bus)
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "")
        # httpx client lazily; we don't want to require httpx just to
        # import this module (some test environments don't have it).
        self._http: Any = None

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a Telegram Update payload.

        Telegram Update shape:
            {
              "update_id": 123,
              "message": {
                "message_id": 456,
                "from": {"id": 789, "username": "alice", ...},
                "chat": {"id": -100123, "type": "supergroup", ...},
                "date": 1729012345,
                "text": "@skynet_bot hello",
                "reply_to_message": {...}     # optional
              }
            }
        """
        msg = raw.get("message") or raw.get("edited_message")
        if not isinstance(msg, dict):
            return None
        text = (msg.get("text") or "").strip()
        if not text:
            # Stickers / photos / docs — ignore for now; multi-modal
            # ingestion is a follow-up.
            return None
        # Strip @botname mentions in group chats so the agent sees a
        # clean prompt. Hermes / Slack / Discord do the equivalent.
        if self.bot_username:
            tagged = "@" + self.bot_username.lstrip("@")
            if tagged.lower() in text.lower():
                text = _strip_case_insensitive(text, tagged).strip()
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        return InboundMessage(
            platform=self.platform,
            channel=str(chat.get("id") or ""),
            sender=str(sender.get("id") or sender.get("username") or "unknown"),
            text=text,
            thread=str(msg.get("message_id") or "") or None,
            raw=raw,
        )

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        """Post a reply via the Telegram Bot API.

        Uses ``sendMessage``. When ``thread`` is set, passes it as
        ``reply_to_message_id`` so the reply threads cleanly in the
        sender's client.
        """
        if not self.bot_token:
            logger.warning("Telegram send skipped: TELEGRAM_BOT_TOKEN unset")
            return
        if not channel or not text:
            return
        if self._http is None:
            try:
                import httpx  # type: ignore
                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("Telegram send: httpx unavailable")
                return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload: Dict[str, Any] = {"chat_id": channel, "text": text}
        if thread:
            try:
                payload["reply_to_message_id"] = int(thread)
            except (TypeError, ValueError):
                pass
        try:
            resp = await self._http.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "Telegram send returned %d: %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception:
            logger.exception("Telegram send failed")

    async def shutdown(self) -> None:
        """Close the HTTP client. Safe to call multiple times."""
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None


def _strip_case_insensitive(text: str, needle: str) -> str:
    """Remove every case-insensitive occurrence of ``needle`` from ``text``."""
    out: List[str] = []
    i = 0
    n = len(needle)
    if not n:
        return text
    lowered = text.lower()
    needle_lower = needle.lower()
    while i < len(text):
        if lowered[i : i + n] == needle_lower:
            i += n
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


# ── Router: wire channels to the FastAPI app + orchestrator reply path ──


class MessagingRouter:
    """Holds a registry of MessagingChannels by platform name.

    The FastAPI ``/webhooks/{platform}`` route looks up the matching
    channel and calls ``channel.ingest(payload)``. The orchestrator's
    inter-agent message bus subscribes to TASK_COMPLETED events whose
    source is ``<platform>_channel`` and uses the router to deliver
    the reply back via ``channel.send``.
    """

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._channels: Dict[str, MessagingChannel] = {}

    def register(self, channel: MessagingChannel) -> None:
        self._channels[channel.platform] = channel

    def get(self, platform: str) -> Optional[MessagingChannel]:
        return self._channels.get(platform)

    def platforms(self) -> List[str]:
        return sorted(self._channels.keys())

    async def reply(
        self, platform: str, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        """Convenience: look up the channel and forward to its send()."""
        ch = self._channels.get(platform)
        if ch is None:
            logger.warning("MessagingRouter: no channel for platform=%s", platform)
            return
        await ch.send(channel, text, thread=thread)


# Module-level default router (one per process, lazy).
_default_router: Optional[MessagingRouter] = None


def get_default_router(event_bus: Optional[EventBus] = None) -> MessagingRouter:
    """Return the process-wide router. Creates one bound to ``event_bus``
    if it doesn't exist yet."""
    global _default_router
    if _default_router is None:
        _default_router = MessagingRouter(event_bus or EventBus())
    return _default_router
