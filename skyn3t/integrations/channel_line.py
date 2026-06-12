"""LINE messaging-channel adapter for SkyN3t.

LINE is the dominant consumer messenger in Japan, Taiwan, and Thailand.
Its Messaging API uses a single long-lived channel access token (no
token-fetch dance), so this adapter follows the WhatsAppChannel pattern in
``messaging.py`` rather than the Feishu token-mint pattern: a bearer token
authorizes outbound sends, and inbound webhook events are normalized into a
platform-neutral ``InboundMessage``.

Wire references:
  - Inbound: POST to your webhook with
             {"events": [{"type":"message","message":{"type":"text",
              "text":"hi","id":"..."},"source":{"userId":"U.."},
              "replyToken":"..."}]}
  - Reply:   POST https://api.line.me/v2/bot/message/reply
             {replyToken, messages:[{type:"text",text}]}   (preferred)
  - Push:    POST https://api.line.me/v2/bot/message/push
             {to, messages:[{type:"text",text}]}           (fallback)

LINE's reply token is single-use and short-lived. We carry it as the
InboundMessage.thread; ``send`` uses the reply endpoint when the supplied
``thread`` looks like a reply token, otherwise falls back to push.

Env config (opt-in; absent creds => is_available() False, send() no-ops):
  LINE_CHANNEL_ACCESS_TOKEN — long-lived channel access token (Bearer)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from skyn3t.core.events import EventBus
from skyn3t.integrations.messaging import InboundMessage, MessagingChannel

logger = logging.getLogger("skyn3t.integrations.channel_line")

_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
_PUSH_URL = "https://api.line.me/v2/bot/message/push"


class LineChannel(MessagingChannel):
    """LINE Messaging API channel via api.line.me.

    Designed for webhook inbound: LINE POSTs an events envelope to your
    webhook which calls ``channel.ingest(payload)``. Replies prefer the
    single-use reply token (carried as InboundMessage.thread); when no valid
    reply token is available the push endpoint targets the user id directly.
    """

    platform = "line"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        channel_access_token: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.channel_access_token = (
            channel_access_token or os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
        )
        self._http: Any = None

    def is_available(self) -> bool:
        """True when the channel access token is configured.

        Pure / non-raising: the gateway and status endpoint key off this to
        decide whether the channel is enabled.
        """
        return bool(self.channel_access_token)

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a LINE webhook events envelope.

        Shape:
            {"events": [{
                "type": "message",
                "replyToken": "rt-abc",
                "source": {"type": "user", "userId": "Uxxx"},
                "message": {"type": "text", "id": "mid", "text": "hi"}
            }]}
        Returns the first text message event, or None if there isn't one.
        """
        try:
            for event in raw.get("events") or []:
                if event.get("type") != "message":
                    continue
                message = event.get("message") or {}
                if message.get("type") != "text":
                    continue
                text = str(message.get("text") or "").strip()
                if not text:
                    continue
                source = event.get("source") or {}
                # Prefer userId; group/room chats expose groupId/roomId.
                target = (
                    source.get("userId")
                    or source.get("groupId")
                    or source.get("roomId")
                    or ""
                )
                # Carry the single-use reply token as the thread key so send()
                # can use the (cheaper, push-quota-free) reply endpoint.
                return InboundMessage(
                    platform=self.platform,
                    channel=str(target),
                    sender=str(source.get("userId") or "unknown"),
                    text=text,
                    thread=str(event.get("replyToken") or "") or None,
                    raw=raw,
                )
        except Exception:
            logger.exception("LINE inbound parse failed")
        return None

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        """Post a reply via the LINE Messaging API.

        When ``thread`` carries a reply token, use the reply endpoint;
        otherwise push to ``channel`` (the user/group/room id). Absent creds
        => no-op with a warning (never raises).
        """
        if not self.is_available():
            logger.warning("LINE send skipped: CHANNEL_ACCESS_TOKEN unset")
            return
        if not text:
            return
        if self._http is None:
            try:
                import httpx  # type: ignore

                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("LINE send: httpx unavailable")
                return
        messages = [{"type": "text", "text": text}]
        if thread:
            url = _REPLY_URL
            body: Dict[str, Any] = {"replyToken": thread, "messages": messages}
        else:
            if not channel:
                return
            url = _PUSH_URL
            body = {"to": channel, "messages": messages}
        headers = {"Authorization": f"Bearer {self.channel_access_token}"}
        try:
            resp = await self._http.post(url, json=body, headers=headers)
            if resp.status_code >= 400:
                logger.warning("LINE send %d: %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.exception("LINE send failed")
