"""Notification dispatcher for the approval gate.

v1 supports Discord webhooks only. The shape — single ``dispatch`` entry
point that takes a config dict — leaves room to add more channels
(Slack, Telegram, email) later without changing callers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Tuple

import httpx

logger = logging.getLogger(__name__)

_THROTTLE_WINDOW_SECONDS = 60.0
_HTTP_TIMEOUT_SECONDS = 5.0
_last_dispatch: Dict[Tuple[str, str], float] = {}
_lock = asyncio.Lock()


async def dispatch(
    slug: str,
    agent_name: str,
    dashboard_url: str,
    cfg: Dict[str, Any],
) -> Dict[str, bool]:
    """Send notifications about a pending approval. Returns a dict of
    {channel_name: True/False} for each configured channel that was
    attempted. Empty/missing config → ``{}`` (silent no-op)."""
    notify_cfg = (cfg or {}).get("notify") or {}
    webhook = str(notify_cfg.get("discord_webhook") or "").strip()
    if not webhook:
        return {}

    key = (slug, agent_name)
    async with _lock:
        last = _last_dispatch.get(key, 0.0)
        now = time.monotonic()
        if now - last < _THROTTLE_WINDOW_SECONDS:
            return {"discord": False, "throttled": True}
        _last_dispatch[key] = now

    payload = {
        "content": f"\U0001F50D {slug} needs review after {agent_name}",
        "embeds": [
            {
                "title": "Approve architecture.md",
                "url": dashboard_url,
                "description": (
                    "Pipeline halted. Open the dashboard to review, edit, "
                    "or reject the architecture before the next stage runs."
                ),
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(webhook, json=payload)
            response.raise_for_status()
        return {"discord": True}
    except Exception:
        logger.warning("discord webhook dispatch failed", exc_info=True)
        return {"discord": False}
