"""FastAPI webhook handler for Telegram.

Exposes ``POST /webhooks/telegram`` so Telegram's ``setWebhook`` can
deliver Update payloads. The handler hands the raw payload off to
TelegramChannel.ingest(), which normalizes it + publishes a
TASK_CREATED event on the orchestrator bus.

Optional secret-token verification: when ``TELEGRAM_WEBHOOK_SECRET`` is
set, the handler requires Telegram's ``X-Telegram-Bot-Api-Secret-Token``
header to match. Telegram strongly recommends this — without it, anyone
who knows your webhook URL can spam your bot with forged updates.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request

from skyn3t.integrations.messaging import TelegramChannel, get_default_router

logger = logging.getLogger("skyn3t.integrations.telegram_webhook")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _get_webhook_secret() -> str:
    return os.getenv("TELEGRAM_WEBHOOK_SECRET", "")


@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> Dict[str, Any]:
    """Receive a Telegram Update.

    Returns immediately after ingestion — Telegram resends if it doesn't
    get a 2xx within ~30 seconds, so we never block on the agent's reply.
    The reply is delivered asynchronously by the orchestrator's
    MessagingRouter via TelegramChannel.send() later.
    """
    expected = _get_webhook_secret()
    if expected:
        # Constant-time compare to avoid trivial timing-attack noise.
        # Telegram's header is a plain string, so secrets module is fine.
        import hmac
        if not hmac.compare_digest(expected, x_telegram_bot_api_secret_token or ""):
            raise HTTPException(status_code=401, detail="invalid telegram secret token")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    # The channel may have been registered with a custom token at boot;
    # if nobody registered one we'll lazily create it here so the
    # webhook still works during development.
    rtr = get_default_router()
    channel = rtr.get("telegram")
    if channel is None:
        # Best-effort: pull bus + token from defaults.
        from skyn3t.core.events import EventBus
        channel = TelegramChannel(EventBus())
        rtr.register(channel)
    try:
        await channel.ingest(payload)
    except Exception:
        logger.exception("Telegram ingest failed for update_id=%s", payload.get("update_id"))
        # We still return 200 — re-delivering a bad payload won't help.
    return {"ok": True}
