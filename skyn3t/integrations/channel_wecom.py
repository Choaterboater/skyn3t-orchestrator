"""WeCom / WeChat Work (企业微信) messaging-channel adapter for SkyN3t.

WeCom is Tencent's enterprise IM — the in-company sibling of consumer
WeChat. It shares the Tencent "access_token auth + JSON message" wire
shape with Feishu, so this adapter mirrors the existing FeishuChannel in
``messaging.py``: a ``corp_id``/``corp_secret`` pair mints a short-lived
``access_token`` that authorizes the application message-send endpoint;
inbound message-callbacks are normalized into a platform-neutral
``InboundMessage``.

Wire references:
  - Token:   GET  https://qyapi.weixin.qq.com/cgi-bin/gettoken
             ?corpid=..&corpsecret=.. -> {errcode, access_token, expires_in}
  - Send:    POST https://qyapi.weixin.qq.com/cgi-bin/message/send
             ?access_token=..  {touser, msgtype:"text", agentid, text:{content}}
  - Inbound: WeCom posts decrypted callback JSON with
             {MsgType:"text", Content, FromUserName, AgentID, MsgId, ...}

Env config (opt-in; absent creds => is_available() False, send() no-ops):
  WECOM_CORP_ID     — enterprise corp id
  WECOM_SECRET      — application secret (mints access_token)
  WECOM_AGENT_ID    — application agentid (required for message send)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from skyn3t.core.events import EventBus
from skyn3t.integrations.messaging import InboundMessage, MessagingChannel

logger = logging.getLogger("skyn3t.integrations.channel_wecom")

_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"


class WeComChannel(MessagingChannel):
    """WeChat Work (WeCom) application channel via the qyapi.weixin.qq.com API.

    Designed for the message-callback inbound pattern: WeCom POSTs each
    (decrypted) message event to a webhook/handler which calls
    ``channel.ingest(payload)``. Replies go out through the application
    message/send endpoint, authorized by an ``access_token`` minted from the
    corp id/secret pair (cached, refreshed ~60s before expiry).
    """

    platform = "wecom"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        corp_id: Optional[str] = None,
        corp_secret: Optional[str] = None,
        agent_id: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.corp_id = corp_id or os.getenv("WECOM_CORP_ID", "")
        self.corp_secret = corp_secret or os.getenv("WECOM_SECRET", "")
        self.agent_id = agent_id or os.getenv("WECOM_AGENT_ID", "")
        self._http: Any = None
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def is_available(self) -> bool:
        """True when corp id + secret + agent id are configured.

        Pure / non-raising: the gateway and status endpoint key off this to
        decide whether the channel is enabled.
        """
        return bool(self.corp_id and self.corp_secret and self.agent_id)

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a WeCom (decrypted) message-callback payload.

        Shape (text message; WeCom uses XML-style PascalCase keys which the
        callback decrypt layer typically hands over as a dict):
            {
              "MsgType": "text",
              "Content": "build me a thing",
              "FromUserName": "alice",
              "ToUserName": "wwcorpid",
              "AgentID": "1000002",
              "MsgId": "1234567890"
            }
        Returns None for anything that isn't a text message.
        """
        try:
            if raw.get("MsgType") != "text":
                return None
            text = str(raw.get("Content") or "").strip()
            if not text:
                return None
            return InboundMessage(
                platform=self.platform,
                channel=str(raw.get("FromUserName") or ""),
                sender=str(raw.get("FromUserName") or "unknown"),
                text=text,
                thread=str(raw.get("MsgId") or "") or None,
                raw=raw,
            )
        except Exception:
            logger.exception("WeCom inbound parse failed")
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
                logger.exception("WeCom token: httpx unavailable")
                return None
        try:
            resp = await self._http.get(
                _TOKEN_URL,
                params={"corpid": self.corp_id, "corpsecret": self.corp_secret},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "WeCom token %d: %s", resp.status_code, resp.text[:200]
                )
                return None
            data = resp.json() or {}
            if data.get("errcode") not in (0, None):
                logger.warning(
                    "WeCom token error %s: %s",
                    data.get("errcode"),
                    data.get("errmsg"),
                )
                return None
            token = data.get("access_token")
            if not token:
                return None
            self._token = token
            expires_in = int(data.get("expires_in") or 0)
            self._token_expires_at = now + expires_in
            return self._token
        except Exception:
            logger.exception("WeCom token fetch failed")
            return None

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        """Post a reply via the WeCom application message/send API.

        ``channel`` is the recipient user id (touser). Absent creds => no-op
        with a warning (never raises).
        """
        if not self.is_available():
            logger.warning("WeCom send skipped: CORP_ID / SECRET / AGENT_ID unset")
            return
        if not channel or not text:
            return
        token = await self._get_token()
        if not token:
            logger.warning("WeCom send skipped: no access token")
            return
        body: Dict[str, Any] = {
            "touser": channel,
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {"content": text},
        }
        try:
            resp = await self._http.post(
                _SEND_URL, params={"access_token": token}, json=body
            )
            if resp.status_code >= 400:
                logger.warning(
                    "WeCom send %d: %s", resp.status_code, resp.text[:200]
                )
        except Exception:
            logger.exception("WeCom send failed")
