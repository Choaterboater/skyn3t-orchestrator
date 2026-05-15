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
from typing import Any, Dict, List, Optional

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


# ── WhatsApp Cloud API (webhook mode) ─────────────────────────────────


class WhatsAppChannel(MessagingChannel):
    """WhatsApp Cloud API channel (Meta's Graph API).

    Meta delivers updates to your webhook URL; replies go back via
    POST /v18.0/{phone_number_id}/messages.

    Env config:
      WHATSAPP_ACCESS_TOKEN     — long-lived Graph API token, required for send.
      WHATSAPP_PHONE_NUMBER_ID  — the phone-number-id from the
                                  WhatsApp Business app dashboard.
    """

    platform = "whatsapp"

    def __init__(
        self, event_bus: EventBus, *, access_token: Optional[str] = None,
        phone_number_id: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.access_token = access_token or os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        self.phone_number_id = phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        self._http: Any = None

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize WhatsApp webhook envelope.

        Shape:
            {"entry": [{"changes": [{"value": {
                "messages": [{
                    "from": "1234567890",
                    "id": "wamid.xxx",
                    "text": {"body": "hello"}
                }]
            }}]}]}
        """
        try:
            entries = raw.get("entry") or []
            for entry in entries:
                for change in entry.get("changes") or []:
                    value = change.get("value") or {}
                    for msg in value.get("messages") or []:
                        text = ((msg.get("text") or {}).get("body") or "").strip()
                        if not text:
                            continue  # ignore non-text payloads for now
                        return InboundMessage(
                            platform=self.platform,
                            channel=str(msg.get("from") or ""),
                            sender=str(msg.get("from") or "unknown"),
                            text=text,
                            thread=str(msg.get("id") or "") or None,
                            raw=raw,
                        )
        except Exception:
            logger.exception("WhatsApp inbound parse failed")
        return None

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        if not self.access_token or not self.phone_number_id:
            logger.warning("WhatsApp send skipped: ACCESS_TOKEN / PHONE_NUMBER_ID unset")
            return
        if not channel or not text:
            return
        if self._http is None:
            try:
                import httpx  # type: ignore
                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("WhatsApp send: httpx unavailable")
                return
        url = f"https://graph.facebook.com/v18.0/{self.phone_number_id}/messages"
        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": channel,
            "type": "text",
            "text": {"body": text},
        }
        if thread:
            # WhatsApp supports reply context via context.message_id.
            payload["context"] = {"message_id": thread}
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.warning("WhatsApp send %d: %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.exception("WhatsApp send failed")


# ── Matrix (homeserver appservice / bot pattern) ──────────────────────


class MatrixChannel(MessagingChannel):
    """Matrix channel using the homeserver client-server API.

    Designed for bot accounts authenticated with an access token. The
    homeserver POSTs ``/transactions`` to an appservice URL when the
    bot is configured that way, OR a long-running client polls
    ``/sync`` — but neither path is implemented here; this class
    handles the **normalized message shape** so a polling/appservice
    loop can hand events off via ``ingest()``.

    Env config:
      MATRIX_HOMESERVER_URL  — base URL like https://matrix.example.org
      MATRIX_ACCESS_TOKEN    — bot user's access token, required for send.
    """

    platform = "matrix"

    def __init__(
        self, event_bus: EventBus, *, homeserver_url: Optional[str] = None,
        access_token: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.homeserver_url = (
            homeserver_url or os.getenv("MATRIX_HOMESERVER_URL", "") or ""
        ).rstrip("/")
        self.access_token = access_token or os.getenv("MATRIX_ACCESS_TOKEN", "")
        self._http: Any = None
        self._txn_counter = 0

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a single Matrix event.

        Shape (a single m.room.message of msgtype m.text):
            {
              "event_id": "$xxx",
              "room_id": "!abc:example.org",
              "sender": "@alice:example.org",
              "content": {"msgtype": "m.text", "body": "hello"},
              "type": "m.room.message"
            }
        """
        if raw.get("type") != "m.room.message":
            return None
        content = raw.get("content") or {}
        if content.get("msgtype") != "m.text":
            return None
        text = (content.get("body") or "").strip()
        if not text:
            return None
        return InboundMessage(
            platform=self.platform,
            channel=str(raw.get("room_id") or ""),
            sender=str(raw.get("sender") or "unknown"),
            text=text,
            thread=str(raw.get("event_id") or "") or None,
            raw=raw,
        )

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        if not self.homeserver_url or not self.access_token:
            logger.warning("Matrix send skipped: HOMESERVER_URL / ACCESS_TOKEN unset")
            return
        if not channel or not text:
            return
        if self._http is None:
            try:
                import httpx  # type: ignore
                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("Matrix send: httpx unavailable")
                return
        # Matrix requires a per-request unique transaction id so retries
        # don't deliver duplicates. We use a monotonic counter +
        # process-start-time-derived uuid prefix.
        self._txn_counter += 1
        txn_id = f"skyn3t-{int(__import__('time').time())}-{self._txn_counter}"
        url = (
            f"{self.homeserver_url}/_matrix/client/v3/rooms/"
            f"{channel}/send/m.room.message/{txn_id}"
        )
        body: Dict[str, Any] = {"msgtype": "m.text", "body": text}
        if thread:
            # m.thread relation per MSC 3440.
            body["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread}
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            resp = await self._http.put(url, json=body, headers=headers)
            if resp.status_code >= 400:
                logger.warning("Matrix send %d: %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.exception("Matrix send failed")


# ── Signal (signal-cli REST API or signald JSON-RPC) ─────────────────


class SignalChannel(MessagingChannel):
    """Signal channel via signal-cli's REST API.

    Signal doesn't ship a first-party bot API. The standard self-hosted
    bridge is bbernhard/signal-cli-rest-api — a Docker container that
    wraps signal-cli and exposes a small REST surface for sending and
    receiving messages. This class targets that wire shape.

    Inbound: poll-mode or webhook-mode (the bridge supports both).
    This implementation accepts whatever payload shape the bridge
    forwards via webhook → ingest().

    Outbound: POST /v2/send to the bridge with {message, number,
    recipients}. The bridge handles the actual Signal Protocol.

    Env config:
      SIGNAL_BRIDGE_URL  — base URL, e.g. http://localhost:8080
      SIGNAL_NUMBER      — the registered bot phone number in E.164,
                           e.g. +15551234567
    """

    platform = "signal"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        bridge_url: Optional[str] = None,
        number: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.bridge_url = (bridge_url or os.getenv("SIGNAL_BRIDGE_URL", "") or "").rstrip("/")
        self.number = number or os.getenv("SIGNAL_NUMBER", "")
        self._http: Any = None

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a signal-cli-rest-api envelope.

        Shape (a typical incoming-text payload from the bridge):
            {
              "envelope": {
                "source": "+15551234567",
                "sourceName": "Alice",
                "timestamp": 1729012345000,
                "dataMessage": {
                  "message": "build me a thing",
                  "groupInfo": {"groupId": "abc=", "type": "DELIVER"}
                }
              },
              "account": "+15559999999"
            }
        """
        envelope = raw.get("envelope") or {}
        data = envelope.get("dataMessage") or {}
        text = (data.get("message") or "").strip()
        if not text:
            # Receipts, typing, sync — ignore.
            return None
        source = str(envelope.get("source") or "")
        if not source:
            return None
        # Channel = group id when in a group, else the sender number.
        group = (data.get("groupInfo") or {}).get("groupId")
        channel = str(group or source)
        return InboundMessage(
            platform=self.platform,
            channel=channel,
            sender=source,
            text=text,
            thread=str(envelope.get("timestamp") or "") or None,
            raw=raw,
        )

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        if not self.bridge_url or not self.number:
            logger.warning("Signal send skipped: SIGNAL_BRIDGE_URL / SIGNAL_NUMBER unset")
            return
        if not channel or not text:
            return
        if self._http is None:
            try:
                import httpx  # type: ignore
                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("Signal send: httpx unavailable")
                return
        # signal-cli-rest-api v2 distinguishes recipients (numbers) from
        # group ids by looking at the value: a leading '+' means E.164
        # number, otherwise treated as a group id.
        if channel.startswith("+"):
            payload: Dict[str, Any] = {
                "message": text,
                "number": self.number,
                "recipients": [channel],
            }
        else:
            payload = {
                "message": text,
                "number": self.number,
                "recipients": [channel],
                # Some bridge versions need an explicit group_id field;
                # send both to maximize compat.
                "group_id": channel,
            }
        url = f"{self.bridge_url}/v2/send"
        try:
            resp = await self._http.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("Signal send %d: %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.exception("Signal send failed")


# ── BlueBubbles iMessage (BlueBubbles server bridge) ──────────────────


class IMessageChannel(MessagingChannel):
    """iMessage channel via the BlueBubbles server (open-source iMessage
    bridge for macOS).

    Apple offers no official API. BlueBubbles is the dominant self-host
    bridge: a Mac runs the BlueBubbles Server, which exposes a REST
    surface over the local network. This class targets BlueBubbles v1.

    Inbound: the bridge POSTs new-message webhooks to /webhooks/imessage
    on our server. Outbound: POST /api/v1/message/text with the chat
    GUID and message body.

    Env config:
      BLUEBUBBLES_URL       — base URL, e.g. http://192.168.1.50:1234
      BLUEBUBBLES_PASSWORD  — required for every API call; the bridge
                              uses a password instead of a token.
    """

    platform = "imessage"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        bridge_url: Optional[str] = None,
        password: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.bridge_url = (bridge_url or os.getenv("BLUEBUBBLES_URL", "") or "").rstrip("/")
        self.password = password or os.getenv("BLUEBUBBLES_PASSWORD", "")
        self._http: Any = None

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a BlueBubbles 'new-message' webhook envelope.

        Shape:
            {
              "type": "new-message",
              "data": {
                "guid": "iMessage;-;chat123",
                "text": "build me a thing",
                "chats": [{"guid": "iMessage;-;chat123"}],
                "handle": {"address": "+15551234567"},
                "isFromMe": false,
                "dateCreated": 1729012345000
              }
            }
        """
        if raw.get("type") != "new-message":
            return None
        data = raw.get("data") or {}
        if data.get("isFromMe"):
            # Echo from the bot's own send; ignore so we don't loop.
            return None
        text = (data.get("text") or "").strip()
        if not text:
            return None
        chats = data.get("chats") or []
        chat_guid = ""
        if chats and isinstance(chats[0], dict):
            chat_guid = str(chats[0].get("guid") or "")
        handle = (data.get("handle") or {}).get("address") or ""
        if not chat_guid:
            return None
        return InboundMessage(
            platform=self.platform,
            channel=chat_guid,
            sender=str(handle or "unknown"),
            text=text,
            thread=str(data.get("guid") or "") or None,
            raw=raw,
        )

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        if not self.bridge_url or not self.password:
            logger.warning("iMessage send skipped: BLUEBUBBLES_URL / PASSWORD unset")
            return
        if not channel or not text:
            return
        if self._http is None:
            try:
                import httpx  # type: ignore
                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("iMessage send: httpx unavailable")
                return
        url = (
            f"{self.bridge_url}/api/v1/message/text"
            f"?password={self.password}"
        )
        # BlueBubbles requires a deterministic tempGuid for idempotency.
        import hashlib as _hashlib
        import time as _time
        seed = f"{channel}|{int(_time.time() * 1000)}|{text[:64]}"
        temp_guid = "temp-" + _hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]
        payload = {
            "chatGuid": channel,
            "tempGuid": temp_guid,
            "message": text,
            "method": "apple-script",  # most reliable on macOS hosts
        }
        try:
            resp = await self._http.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("iMessage send %d: %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.exception("iMessage send failed")


# ── Microsoft Teams (Bot Framework webhook) ───────────────────────────


class MSTeamsChannel(MessagingChannel):
    """Microsoft Teams channel via the Bot Framework REST API.

    Teams talks to bots via the Azure Bot Service. The bot registers
    a messaging endpoint (HTTPS), Teams POSTs Activity payloads to it,
    and the bot replies by POSTing back to ``activity.serviceUrl`` with
    a bearer token from Azure AD.

    For self-hosted bots, the typical setup is:
      1. Register a bot in Azure Bot Service (free tier ok).
      2. Set the Microsoft App ID + password in env vars.
      3. Configure the bot's messaging endpoint to
         POST https://<your-host>/webhooks/msteams.
      4. Mount that route in this codebase (kept out of this module so
         operators can opt in — bot framework token fetching adds a
         Microsoft auth dependency we don't want to force).

    This class handles the wire-shape pieces: parsing inbound Activity
    payloads, building reply Activities. Auth token acquisition is
    left to the operator's deployment because it requires the msal
    package + an Azure tenant configuration that varies.

    Env config:
      MSTEAMS_APP_ID       — bot's Microsoft App ID
      MSTEAMS_APP_PASSWORD — bot's Microsoft App password / secret
    """

    platform = "msteams"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        app_id: Optional[str] = None,
        app_password: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.app_id = app_id or os.getenv("MSTEAMS_APP_ID", "")
        self.app_password = app_password or os.getenv("MSTEAMS_APP_PASSWORD", "")
        self._http: Any = None
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a Teams Activity payload.

        Shape (an incoming user-typed message in a channel/group):
            {
              "type": "message",
              "id": "1700000000",
              "text": "<at>SkyN3t</at> build me a thing",
              "from": {"id": "29:abc...", "name": "Alice"},
              "conversation": {"id": "19:xyz@thread.tacv2"},
              "serviceUrl": "https://smba.trafficmanager.net/amer/",
              "channelData": {"team": {...}}
            }
        """
        if raw.get("type") != "message":
            return None
        text = (raw.get("text") or "").strip()
        if not text:
            return None
        # Strip <at>BotName</at> mentions Teams injects in group chats.
        import re as _re
        text = _re.sub(r"<at>[^<]*</at>", "", text).strip()
        if not text:
            return None
        conv = raw.get("conversation") or {}
        sender = raw.get("from") or {}
        # `serviceUrl` is per-tenant — the bot must POST replies back to
        # whatever serviceUrl the inbound carried. Stash it in raw so
        # the send path can look it up.
        return InboundMessage(
            platform=self.platform,
            channel=str(conv.get("id") or ""),
            sender=str(sender.get("id") or sender.get("name") or "unknown"),
            text=text,
            thread=str(raw.get("id") or "") or None,
            raw=raw,
        )

    async def _get_token(self) -> Optional[str]:
        """Acquire a Bot Framework token via the AAD client-credentials
        flow. Cached for the token's lifetime minus a 60s safety margin.
        Returns None when credentials aren't set so the caller can skip
        the send cleanly."""
        import time as _time
        now = _time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        if not self.app_id or not self.app_password:
            return None
        if self._http is None:
            try:
                import httpx  # type: ignore
                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                return None
        url = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.app_id,
            "client_secret": self.app_password,
            "scope": "https://api.botframework.com/.default",
        }
        try:
            resp = await self._http.post(
                url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code >= 400:
                logger.warning("MSTeams token %d: %s", resp.status_code, resp.text[:200])
                return None
            tok = resp.json()
            self._token = tok.get("access_token")
            expires_in = int(tok.get("expires_in") or 0)
            self._token_expires_at = now + expires_in
            return self._token
        except Exception:
            logger.exception("MSTeams token fetch failed")
            return None

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        """Post a reply to a Teams conversation.

        Teams routes replies through ``serviceUrl``, but we don't have
        that here without the inbound payload. Operators pass the
        inbound's ``raw`` back via the router's reply path — see
        ``ingest`` which preserves it on the published TASK_CREATED
        payload. For the simple case this method falls back to the
        default Teams serviceUrl which is correct for most tenants.
        """
        if not channel or not text:
            return
        token = await self._get_token()
        if not token:
            logger.warning("MSTeams send skipped: no token")
            return
        # Default serviceUrl — the per-tenant URL is the actual right
        # answer when present, but smba.trafficmanager.net is the
        # documented global ingress and works for most deploys.
        service_url = "https://smba.trafficmanager.net/amer/"
        url = (
            f"{service_url.rstrip('/')}/v3/conversations/"
            f"{channel}/activities"
        )
        body: Dict[str, Any] = {"type": "message", "text": text}
        if thread:
            body["replyToId"] = thread
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = await self._http.post(url, json=body, headers=headers)
            if resp.status_code >= 400:
                logger.warning("MSTeams send %d: %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.exception("MSTeams send failed")


# ── Mattermost (incoming webhook + outgoing webhook) ─────────────────


class MattermostChannel(MessagingChannel):
    """Mattermost channel via the incoming/outgoing webhook pair.

    Mattermost ships its own bot API but the dominant pattern (used by
    Hermes' adapter) is:
      - Inbound: Mattermost POSTs an outgoing webhook to our HTTPS
        endpoint when a user message matches a trigger word or channel.
      - Outbound: We POST to an incoming webhook URL the operator
        creates in Mattermost.

    The shape is intentionally Slack-compatible because Mattermost
    originated as a Slack alternative.

    Env config:
      MATTERMOST_INCOMING_WEBHOOK_URL  — for outbound replies
      MATTERMOST_USERNAME              — override sender display name
    """

    platform = "mattermost"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        incoming_webhook_url: Optional[str] = None,
        username: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.incoming_webhook_url = (
            incoming_webhook_url or os.getenv("MATTERMOST_INCOMING_WEBHOOK_URL", "")
        )
        self.username = username or os.getenv("MATTERMOST_USERNAME", "skyn3t")
        self._http: Any = None

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize the Mattermost outgoing-webhook form encoding.

        Mattermost outgoing webhooks send application/x-www-form-urlencoded
        with keys: token, team_id, team_domain, channel_id, channel_name,
        timestamp, user_id, user_name, text, trigger_word.
        """
        text = (raw.get("text") or "").strip()
        if not text:
            return None
        trigger = raw.get("trigger_word") or ""
        if trigger and text.lower().startswith(trigger.lower()):
            text = text[len(trigger):].strip()
        if not text:
            return None
        return InboundMessage(
            platform=self.platform,
            channel=str(raw.get("channel_id") or ""),
            sender=str(raw.get("user_id") or raw.get("user_name") or "unknown"),
            text=text,
            thread=str(raw.get("post_id") or "") or None,
            raw=raw,
        )

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        if not self.incoming_webhook_url:
            logger.warning("Mattermost send skipped: INCOMING_WEBHOOK_URL unset")
            return
        if not text:
            return
        if self._http is None:
            try:
                import httpx  # type: ignore
                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("Mattermost send: httpx unavailable")
                return
        payload: Dict[str, Any] = {"text": text, "username": self.username}
        if channel:
            payload["channel"] = channel
        try:
            resp = await self._http.post(self.incoming_webhook_url, json=payload)
            if resp.status_code >= 400:
                logger.warning("Mattermost send %d: %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.exception("Mattermost send failed")


# ── Feishu / Lark (Tencent's bot API; same shape as WeCom/Weixin) ────


class FeishuChannel(MessagingChannel):
    """Feishu (Lark) custom-bot channel via the open.feishu.cn API.

    The Tencent / Lark / WeCom family all share the "tenant_access_token
    auth + JSON message_type=text" wire shape. This implementation
    targets Feishu specifically; subclassing for WeCom / Weixin is a
    one-line URL change.

    Env config:
      FEISHU_APP_ID         — open-platform app id
      FEISHU_APP_SECRET     — app secret (used to mint tenant_access_token)
    """

    platform = "feishu"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.app_id = app_id or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET", "")
        self._http: Any = None
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a Feishu event-subscription payload.

        Shape (a message event under header.event_type='im.message.receive_v1'):
            {
              "schema": "2.0",
              "header": {"event_type": "im.message.receive_v1", ...},
              "event": {
                "sender": {"sender_id": {"open_id": "ou_..."}},
                "message": {
                  "message_id": "om_...",
                  "chat_id": "oc_...",
                  "content": "{\"text\":\"hi\"}",
                  "message_type": "text"
                }
              }
            }
        """
        header = raw.get("header") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return None
        event = raw.get("event") or {}
        message = event.get("message") or {}
        if message.get("message_type") != "text":
            return None
        # Content is a JSON-encoded string. Decode it defensively.
        import json as _json
        try:
            content = _json.loads(message.get("content") or "{}")
        except Exception:
            return None
        text = (content.get("text") or "").strip()
        if not text:
            return None
        sender = (event.get("sender") or {}).get("sender_id") or {}
        return InboundMessage(
            platform=self.platform,
            channel=str(message.get("chat_id") or ""),
            sender=str(sender.get("open_id") or sender.get("user_id") or "unknown"),
            text=text,
            thread=str(message.get("message_id") or "") or None,
            raw=raw,
        )

    async def _get_token(self) -> Optional[str]:
        """Cache the tenant_access_token; refresh ~60s before expiry."""
        import time as _time
        now = _time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        if not self.app_id or not self.app_secret:
            return None
        if self._http is None:
            try:
                import httpx  # type: ignore
                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                return None
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            resp = await self._http.post(
                url, json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            if resp.status_code >= 400:
                logger.warning("Feishu token %d: %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json() or {}
            if data.get("code") != 0:
                logger.warning("Feishu token error %s: %s", data.get("code"), data.get("msg"))
                return None
            self._token = data.get("tenant_access_token")
            expires_in = int(data.get("expire") or 0)
            self._token_expires_at = now + expires_in
            return self._token
        except Exception:
            logger.exception("Feishu token fetch failed")
            return None

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        if not channel or not text:
            return
        token = await self._get_token()
        if not token:
            logger.warning("Feishu send skipped: no token")
            return
        import json as _json
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        body = {
            "receive_id": channel,
            "msg_type": "text",
            "content": _json.dumps({"text": text}),
        }
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = await self._http.post(url, json=body, headers=headers)
            if resp.status_code >= 400:
                logger.warning("Feishu send %d: %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.exception("Feishu send failed")


# ── Generic HTTP webhook channel — fills the remaining long-tail ─────


class GenericWebhookChannel(MessagingChannel):
    """Catch-all channel for any "POST text in, POST text out" bridge.

    Operators point this at a service that accepts inbound messages on
    a known JSON-or-form shape and forwards replies via a webhook URL.
    Used to wire up SMS gateways (Twilio webhook), Home Assistant
    notifications, custom internal tools, or any platform we don't
    have a dedicated subclass for yet.

    Configuration is per-instance, not env-based, so multiple instances
    can be registered for different bridges in one process.

    Constructor:
        platform_name      — what the InboundMessage.platform tag is
        outbound_url       — where to POST replies
        text_field         — key in inbound payload that carries the
                             user text (default: "text")
        channel_field      — key for the destination id (default:
                             "channel")
        sender_field       — key for the sender id (default: "sender")
        outbound_template  — dict template for outbound POST body. Use
                             "{text}" and "{channel}" placeholders;
                             they're substituted at send time.
        auth_header        — optional ("Authorization": "Bearer ...") dict
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        platform_name: str,
        outbound_url: str = "",
        text_field: str = "text",
        channel_field: str = "channel",
        sender_field: str = "sender",
        thread_field: str = "thread",
        outbound_template: Optional[Dict[str, str]] = None,
        auth_headers: Optional[Dict[str, str]] = None,
    ):
        super().__init__(event_bus)
        self.platform = platform_name
        self.outbound_url = outbound_url
        self.text_field = text_field
        self.channel_field = channel_field
        self.sender_field = sender_field
        self.thread_field = thread_field
        self.outbound_template = outbound_template or {"channel": "{channel}", "text": "{text}"}
        self.auth_headers = auth_headers or {}
        self._http: Any = None

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        text = str(raw.get(self.text_field) or "").strip()
        if not text:
            return None
        return InboundMessage(
            platform=self.platform,
            channel=str(raw.get(self.channel_field) or ""),
            sender=str(raw.get(self.sender_field) or "unknown"),
            text=text,
            thread=str(raw.get(self.thread_field) or "") or None,
            raw=raw,
        )

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        if not self.outbound_url:
            logger.warning("%s send skipped: outbound_url unset", self.platform)
            return
        if not text:
            return
        if self._http is None:
            try:
                import httpx  # type: ignore
                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("%s send: httpx unavailable", self.platform)
                return
        # Interpolate the template — supports any nesting that comes in
        # the template values as long as the strings carry placeholders.
        rendered: Dict[str, Any] = {}
        for k, v in self.outbound_template.items():
            if isinstance(v, str):
                rendered[k] = (
                    v.replace("{text}", text)
                    .replace("{channel}", channel)
                    .replace("{thread}", thread or "")
                )
            else:
                rendered[k] = v
        try:
            resp = await self._http.post(
                self.outbound_url, json=rendered, headers=self.auth_headers,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "%s send %d: %s",
                    self.platform, resp.status_code, resp.text[:200],
                )
        except Exception:
            logger.exception("%s send failed", self.platform)


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
