"""WeChat Official Account (微信公众号) messaging-channel adapter for SkyN3t.

This targets the consumer WeChat **Official Account** platform (mp.weixin):
a public account that users follow and chat with. It shares the Tencent
"access_token auth + JSON message" wire shape with Feishu/WeCom, so this
adapter mirrors the existing FeishuChannel in ``messaging.py``: an
``app_id``/``app_secret`` pair mints a short-lived ``access_token`` that
authorizes the customer-service message endpoint; inbound message
callbacks are normalized into a platform-neutral ``InboundMessage``.

Wire references:
  - Token:   GET  https://api.weixin.qq.com/cgi-bin/token
             ?grant_type=client_credential&appid=..&secret=..
             -> {access_token, expires_in} | {errcode, errmsg}
  - Send:    POST https://api.weixin.qq.com/cgi-bin/message/custom/send
             ?access_token=..  {touser, msgtype:"text", text:{content}}
  - Inbound: WeChat posts an XML message which the callback layer typically
             hands over as a dict: {MsgType:"text", Content, FromUserName,
             ToUserName, MsgId, ...}

Env config (opt-in; absent creds => is_available() False, send() no-ops):
  WECHAT_APP_ID     — official-account app id (appid)
  WECHAT_APP_SECRET — official-account app secret (mints access_token)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from skyn3t.core.events import EventBus
from skyn3t.integrations.messaging import InboundMessage, MessagingChannel

logger = logging.getLogger("skyn3t.integrations.channel_wechat")

_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
_SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/custom/send"


class WeChatChannel(MessagingChannel):
    """WeChat Official Account channel via the api.weixin.qq.com API.

    Designed for the message-callback inbound pattern: WeChat POSTs each
    message event to a webhook/handler which calls ``channel.ingest(payload)``.
    Replies go out through the customer-service custom/send endpoint within
    the 48-hour service window, authorized by an ``access_token`` minted from
    the app id/secret pair (cached, refreshed ~60s before expiry).
    """

    platform = "wechat"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.app_id = app_id or os.getenv("WECHAT_APP_ID", "")
        self.app_secret = app_secret or os.getenv("WECHAT_APP_SECRET", "")
        self._http: Any = None
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def is_available(self) -> bool:
        """True when the app id + secret are configured.

        Pure / non-raising: the gateway and status endpoint key off this to
        decide whether the channel is enabled.
        """
        return bool(self.app_id and self.app_secret)

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a WeChat Official-Account message-callback payload.

        Shape (text message; WeChat uses XML PascalCase keys which the
        callback layer typically hands over as a dict):
            {
              "MsgType": "text",
              "Content": "build me a thing",
              "FromUserName": "oUserOpenId",
              "ToUserName": "gh_officialaccount",
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
            logger.exception("WeChat inbound parse failed")
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
                logger.exception("WeChat token: httpx unavailable")
                return None
        try:
            resp = await self._http.get(
                _TOKEN_URL,
                params={
                    "grant_type": "client_credential",
                    "appid": self.app_id,
                    "secret": self.app_secret,
                },
            )
            if resp.status_code >= 400:
                logger.warning(
                    "WeChat token %d: %s", resp.status_code, resp.text[:200]
                )
                return None
            data = resp.json() or {}
            if data.get("errcode"):
                logger.warning(
                    "WeChat token error %s: %s",
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
            logger.exception("WeChat token fetch failed")
            return None

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        """Post a reply via the WeChat customer-service custom/send API.

        ``channel`` is the recipient user's openid (touser). Absent creds =>
        no-op with a warning (never raises).
        """
        if not self.is_available():
            logger.warning("WeChat send skipped: APP_ID / APP_SECRET unset")
            return
        if not channel or not text:
            return
        token = await self._get_token()
        if not token:
            logger.warning("WeChat send skipped: no access token")
            return
        body: Dict[str, Any] = {
            "touser": channel,
            "msgtype": "text",
            "text": {"content": text},
        }
        try:
            resp = await self._http.post(
                _SEND_URL, params={"access_token": token}, json=body
            )
            if resp.status_code >= 400:
                logger.warning(
                    "WeChat send %d: %s", resp.status_code, resp.text[:200]
                )
        except Exception:
            logger.exception("WeChat send failed")
