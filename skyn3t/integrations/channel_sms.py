"""SMS channel via Twilio's REST API for SkyN3t.

Twilio is the de-facto SMS bridge. Two halves:

  - **Outbound**: POST to the Messages resource with HTTP Basic auth
    (Account SID + Auth Token):

        POST https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json
        Authorization: Basic base64(SID:TOKEN)
        Content-Type: application/x-www-form-urlencoded
        From={TWILIO_FROM}&To={recipient}&Body={text}

  - **Inbound**: Twilio POSTs an application/x-www-form-urlencoded
    webhook to a URL the operator configures on the phone number. The
    interesting fields are ``Body`` (the message), ``From`` (sender
    E.164), ``To`` (our number), and ``MessageSid`` (a stable id we use
    as the thread key). ``handle_inbound`` normalizes that shape.

The ``channel`` argument to ``send`` is the destination phone number in
E.164 form (``+15551234567``). ``thread`` is unused by SMS (no native
threading) and accepted only for interface symmetry.

Env config:
  TWILIO_ACCOUNT_SID  — Account SID (starts 'AC...'); used in the URL
                        and as the Basic-auth username. Required.
  TWILIO_AUTH_TOKEN   — Auth Token; the Basic-auth password. Required.
  TWILIO_FROM         — sending number / messaging-service SID. Required
                        (the From= field on every outbound message).

is_available() => True only when SID, token, and From are all present.
Any missing => adapter disabled, send() is a quiet no-op, never a crash.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, Optional

from skyn3t.core.events import EventBus
from skyn3t.integrations.messaging import InboundMessage, MessagingChannel

logger = logging.getLogger("skyn3t.integrations.channel_sms")

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class SmsChannel(MessagingChannel):
    """SMS channel backed by the Twilio REST API."""

    platform = "sms"

    def __init__(
        self,
        event_bus: EventBus,
        *,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_number: Optional[str] = None,
    ):
        super().__init__(event_bus)
        self.account_sid = account_sid or os.getenv("TWILIO_ACCOUNT_SID", "")
        self.auth_token = auth_token or os.getenv("TWILIO_AUTH_TOKEN", "")
        self.from_number = from_number or os.getenv("TWILIO_FROM", "")
        # Lazy httpx — importing this module must never require httpx.
        self._http: Any = None

    # ── availability gate ──────────────────────────────────────────────

    def is_available(self) -> bool:
        """True when SID, auth token, and From number are all configured.

        Pure / non-raising — the gateway + status endpoint key off this.
        """
        return bool(self.account_sid and self.auth_token and self.from_number)

    # ── inbound (Twilio webhook) ───────────────────────────────────────

    async def handle_inbound(self, raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Normalize a Twilio inbound-SMS webhook payload.

        Twilio POSTs application/x-www-form-urlencoded; the FastAPI route
        parses it into a dict before calling ``ingest`` / this method.
        Relevant fields (Twilio capitalizes them):

            {
              "MessageSid": "SM...",
              "From": "+15551112222",     # the texter
              "To": "+15559998888",       # our Twilio number
              "Body": "deploy the bot",
              "NumMedia": "0"
            }

        ``Body`` is required; empty => ignore (e.g. delivery callbacks,
        status callbacks that carry no Body). The conversation
        ``channel`` is the sender's number, because that's where the
        reply must go.
        """
        if not isinstance(raw, dict):
            return None
        body = str(raw.get("Body") or raw.get("body") or "").strip()
        if not body:
            return None
        sender = str(raw.get("From") or raw.get("from") or "").strip()
        if not sender:
            return None
        sid = raw.get("MessageSid") or raw.get("SmsSid") or raw.get("message_sid")
        return InboundMessage(
            platform=self.platform,
            # Reply target is the sender's number.
            channel=sender,
            sender=sender,
            text=body,
            thread=str(sid) if sid else None,
            raw=raw,
        )

    # ── outbound (Twilio Messages REST) ────────────────────────────────

    def _auth_header(self) -> str:
        token = f"{self.account_sid}:{self.auth_token}".encode("utf-8")
        return "Basic " + base64.b64encode(token).decode("ascii")

    async def send(
        self, channel: str, text: str, *, thread: Optional[str] = None
    ) -> None:
        """Send an SMS to ``channel`` (E.164 number) via Twilio.

        ``thread`` is ignored (SMS has no native threading). No-op when
        creds are missing or when ``channel``/``text`` is empty.
        """
        if not self.is_available():
            logger.warning(
                "SMS send skipped: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / "
                "TWILIO_FROM unset"
            )
            return
        if not channel or not text:
            return
        if self._http is None:
            try:
                import httpx  # type: ignore

                self._http = httpx.AsyncClient(timeout=15.0)
            except Exception:
                logger.exception("SMS send: httpx unavailable")
                return
        url = f"{_TWILIO_API_BASE}/Accounts/{self.account_sid}/Messages.json"
        # Twilio Messages is form-encoded, not JSON.
        data: Dict[str, Any] = {
            "From": self.from_number,
            "To": channel,
            "Body": text,
        }
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            resp = await self._http.post(url, data=data, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "SMS send %d: %s", resp.status_code, resp.text[:200]
                )
        except Exception:
            logger.exception("SMS send failed")

    async def shutdown(self) -> None:
        """Close the HTTP client. Idempotent / safe to call repeatedly."""
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
