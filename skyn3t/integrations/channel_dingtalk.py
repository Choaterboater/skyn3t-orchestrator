"""DingTalk (钉钉) messaging-channel adapter for SkyN3t.

DingTalk is Alibaba's enterprise collaboration platform — the dominant
work-IM in mainland China alongside WeCom. This adapter follows the same
shape as the existing FeishuChannel in ``messaging.py``: an
``app_key``/``app_secret`` pair mints a short-lived ``access_token`` which
authorizes outbound sends, and inbound messages arrive as event-callback
POSTs that we normalize into a platform-neutral ``InboundMessage``.

Wire references:
  - Token:   POST https://api.dingtalk.com/v1.0/oauth2/accessToken
             {appKey, appSecret} -> {accessToken, expireIn}
  - Send:    POST https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend
             (interactive bot → user) with x-acs-dingtalk-access-token header
  - Inbound: DingTalk stream/callback delivers a message payload with
             {msgtype:"text", text:{content}, senderStaffId, conversationId,
              msgId, ...}

Env config (opt-in; absent creds => is_available() False, send() no-ops):
  DINGTALK_APP_KEY      — enterprise app key
  DINGTALK_APP_SECRET   — enterprise app secret (mints access_token)
  DINGTALK_ROBOT_CODE   — optional robotCode for the interactive bot send
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from skyn3t.core.events import EventBus
from skyn3t.integrations.messaging import InboundMessage, MessagingChannel

logger = logging.getLogger("skyn3t.integrations.channel_dingtalk")

_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_SEND_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"


class DingTalkChannel(MessagingChannel):
    """DingTalk enterprise-bot channel via the api.dingtalk.com OpenAPI.

    Designed for the event-callback (or stream) inbound pattern: DingTalk
    POSTs each message event to a webhook/handler which calls
    ``channel.ingest(payload)``. Replies go out through the interactive
    robot batchSend endpoint, authorized by an ``access_token`` minted from
    the app key/secret pair (cached, refreshed ~60s before expiry).
    """

    platform = "dingtalk"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        robot_code: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.app_key = app_key or os.getenv("DINGTALK_APP_KEY", "")
        self.app_secret = app_secret or os.getenv("DINGTALK_APP_SECRET", "")
        self.robot_code = robot_code or os.getenv("DINGTALK_ROBOT_CODE", "")
        self._http: Any = None
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def is_available(self) -> bool:
        """True when the app key + secret are configured.

        Pure / non-raising: the gateway and status endpoint key off this to
        decide whether the channel is enabled.
        """
        return bool(self.app_key and self.app_secret)

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a DingTalk message-callback payload.

        Shape (interactive-robot / stream text message):
            {
              "msgtype": "text",
              "text": {"content": "build me a thing"},
              "senderStaffId": "manager1234",
              "senderNick": "Alice",
              "conversationId": "cidXXXX",
              "msgId": "msgXXXX",
              "robotCode": "ding..."
            }
        Returns None for anything that isn't a text message.
        """
        try:
            if raw.get("msgtype") != "text":
                return None
            text = ((raw.get("text") or {}).get("content") or "").strip()
            if not text:
                return None
            sender = (
                raw.get("senderStaffId")
                or raw.get("senderId")
                or raw.get("senderNick")
                or "unknown"
            )
            return InboundMessage(
                platform=self.platform,
                channel=str(raw.get("conversationId") or ""),
                sender=str(sender),
                text=text,
                thread=str(raw.get("msgId") or "") or None,
                raw=raw,
            )
        except Exception:
            logger.exception("DingTalk inbound parse failed")
            return None

    async def _get_token(self) -> Optional[str]:
        """Cache the access_token; refresh ~60s before expiry."""
        import time as _time

        now = _time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        if not self.is_available():
            return None
        if self._http is None:
            try:
                import httpx  # type: ignore

                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("DingTalk token: httpx unavailable")
                return None
        try:
            resp = await self._http.post(
                _TOKEN_URL,
                json={"appKey": self.app_key, "appSecret": self.app_secret},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "DingTalk token %d: %s", resp.status_code, resp.text[:200]
                )
                return None
            data = resp.json() or {}
            token = data.get("accessToken")
            if not token:
                logger.warning("DingTalk token error: %s", str(data)[:200])
                return None
            self._token = token
            expires_in = int(data.get("expireIn") or 0)
            self._token_expires_at = now + expires_in
            return self._token
        except Exception:
            logger.exception("DingTalk token fetch failed")
            return None

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        """Post a reply via the DingTalk interactive-robot batchSend API.

        Absent creds => no-op with a warning (never raises).
        """
        if not self.is_available():
            logger.warning("DingTalk send skipped: APP_KEY / APP_SECRET unset")
            return
        if not channel or not text:
            return
        token = await self._get_token()
        if not token:
            logger.warning("DingTalk send skipped: no access token")
            return
        import json as _json

        body: Dict[str, Any] = {
            "robotCode": self.robot_code,
            "userIds": [u for u in channel.split(",") if u] or [channel],
            "msgKey": "sampleText",
            "msgParam": _json.dumps({"content": text}),
        }
        headers = {"x-acs-dingtalk-access-token": token}
        try:
            resp = await self._http.post(_SEND_URL, json=body, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "DingTalk send %d: %s", resp.status_code, resp.text[:200]
                )
        except Exception:
            logger.exception("DingTalk send failed")
