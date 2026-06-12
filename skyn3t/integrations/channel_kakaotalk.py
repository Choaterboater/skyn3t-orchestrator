"""KakaoTalk (카카오톡) messaging-channel adapter for SkyN3t.

KakaoTalk is the dominant messenger in South Korea. This adapter targets
the Kakao platform's bot/skill webhook for inbound and the Kakao REST
Message API (kapi.kakao.com) for outbound, authorized with the app's REST
API key as a bearer/admin key. It follows the WhatsAppChannel pattern in
``messaging.py`` (single key, no token-fetch dance): the REST API key
authorizes sends, and inbound skill payloads are normalized into a
platform-neutral ``InboundMessage``.

Wire references:
  - Inbound: Kakao i Open Builder POSTs a skill payload:
             {"userRequest": {"utterance": "hi",
                              "user": {"id": "uid"}},
              "bot": {"id": ".."}, ...}
  - Send:    POST https://kapi.kakao.com/v2/api/talk/memo/default/send
             header Authorization: Bearer <rest_api_key>
             form  template_object={"object_type":"text","text":..,..}

The Kakao skill protocol normally replies inline in the webhook HTTP
response; this adapter additionally supports the REST send path so the
gateway can push proactive/scheduled messages.

Env config (opt-in; absent creds => is_available() False, send() no-ops):
  KAKAO_REST_API_KEY — application REST API key (Bearer for send)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from skyn3t.core.events import EventBus
from skyn3t.integrations.messaging import InboundMessage, MessagingChannel

logger = logging.getLogger("skyn3t.integrations.channel_kakaotalk")

_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
_DEFAULT_LINK = "https://developers.kakao.com"


class KakaoTalkChannel(MessagingChannel):
    """KakaoTalk channel via the Kakao bot webhook + REST Message API.

    Designed for webhook inbound: the Kakao i Open Builder POSTs a skill
    payload to your webhook which calls ``channel.ingest(payload)``. Outbound
    pushes use the Kakao REST Message API authorized by the app's REST API
    key (Bearer). Absent the key, the channel reports itself unavailable and
    ``send`` no-ops.
    """

    platform = "kakaotalk"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        rest_api_key: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.rest_api_key = rest_api_key or os.getenv("KAKAO_REST_API_KEY", "")
        self._http: Any = None

    def is_available(self) -> bool:
        """True when the REST API key is configured.

        Pure / non-raising: the gateway and status endpoint key off this to
        decide whether the channel is enabled.
        """
        return bool(self.rest_api_key)

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a Kakao i Open Builder skill payload.

        Shape:
            {
              "userRequest": {
                "utterance": "build me a thing",
                "user": {"id": "kakao-user-id", "type": "botUserKey"}
              },
              "bot": {"id": "..."},
              "action": {...}
            }
        Returns None when there's no user utterance text.
        """
        try:
            user_request = raw.get("userRequest") or {}
            text = str(user_request.get("utterance") or "").strip()
            if not text:
                return None
            user = user_request.get("user") or {}
            user_id = str(user.get("id") or "unknown")
            return InboundMessage(
                platform=self.platform,
                channel=user_id,
                sender=user_id,
                text=text,
                thread=None,
                raw=raw,
            )
        except Exception:
            logger.exception("KakaoTalk inbound parse failed")
            return None

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        """Post a message via the Kakao REST Message API (talk memo).

        Absent the REST API key => no-op with a warning (never raises).
        """
        if not self.is_available():
            logger.warning("KakaoTalk send skipped: REST_API_KEY unset")
            return
        if not text:
            return
        if self._http is None:
            try:
                import httpx  # type: ignore

                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("KakaoTalk send: httpx unavailable")
                return
        import json as _json

        template_object = {
            "object_type": "text",
            "text": text,
            "link": {"web_url": _DEFAULT_LINK, "mobile_web_url": _DEFAULT_LINK},
        }
        data = {"template_object": _json.dumps(template_object, ensure_ascii=False)}
        headers = {"Authorization": f"Bearer {self.rest_api_key}"}
        try:
            resp = await self._http.post(_SEND_URL, data=data, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "KakaoTalk send %d: %s", resp.status_code, resp.text[:200]
                )
        except Exception:
            logger.exception("KakaoTalk send failed")
