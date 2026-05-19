"""Notification dispatcher for the approval gate.

Supports two Discord delivery paths:

* **Bot channel** — preferred when ``DISCORD_TOKEN`` + a channel id are
  configured. Posts via ``POST /channels/{id}/messages`` which lets us
  attach interactive buttons (Approve / Reject / Open dashboard).
* **Webhook** — fallback when only a webhook URL is configured.
  Incoming webhooks can't carry interactive components, so we send a
  plain embed with a dashboard link.

The dispatch entry point ``dispatch(slug, agent_name, dashboard_url,
cfg)`` is unchanged so existing callers in ``runner.py`` don't need
edits. The cfg shape adds an optional ``notify.discord_bot_channel_id``
field; bot token is read from settings.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_THROTTLE_WINDOW_SECONDS = 60.0
_HTTP_TIMEOUT_SECONDS = 5.0
_DISCORD_DESCRIPTION_MAX = 3900  # Discord caps embed descriptions at 4096; leave headroom.
_last_dispatch: Dict[Tuple[str, str], float] = {}
_lock = asyncio.Lock()


def _summarize_architecture(artifact_dir: Optional[Path]) -> str:
    """Pull the first portion of architecture.md so the user can decide
    without opening the dashboard. Returns "" if the file is missing or
    unreadable."""
    if not artifact_dir:
        return ""
    arch_path = Path(artifact_dir) / "architecture.md"
    if not arch_path.exists():
        return ""
    try:
        text = arch_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    # Prefer the Overview section if present; fall back to the first
    # ~1200 chars of the document.
    lines = text.splitlines()
    overview: list[str] = []
    in_overview = False
    for line in lines:
        stripped = line.strip().lower()
        if stripped.startswith("## "):
            if in_overview:
                break
            if "overview" in stripped:
                in_overview = True
                continue
        if in_overview:
            overview.append(line)
    if overview:
        summary = "\n".join(overview).strip()
    else:
        summary = text.strip()
    if len(summary) > 1200:
        summary = summary[:1200].rstrip() + "…"
    return summary


def _discord_token() -> Optional[str]:
    """Pull the bot token from settings (or env), without crashing if
    settings can't be loaded (e.g. in tests with minimal env)."""
    try:
        from skyn3t.config.settings import get_settings
        tok = get_settings().discord_token
        if tok:
            return tok
    except Exception:  # noqa: BLE001
        logger.debug("settings not available for discord token", exc_info=True)
    return os.getenv("DISCORD_TOKEN")


def _build_components(slug: str, dashboard_url: str) -> list:
    """Discord action row with Approve / Reject / Open dashboard buttons."""
    components: list = [
        {"type": 2, "style": 3, "label": "Approve", "custom_id": f"approve:{slug}"},
        {"type": 2, "style": 4, "label": "Reject", "custom_id": f"reject:{slug}"},
    ]
    if dashboard_url:
        components.append(
            {"type": 2, "style": 5, "label": "Open dashboard", "url": dashboard_url}
        )
    return [{"type": 1, "components": components}]


def _build_embed_payload(
    slug: str, agent_name: str, dashboard_url: str, summary: str = ""
) -> dict:
    description_parts = [
        "Pipeline halted at the human approval gate. Approve to continue, "
        "Reject to send feedback back to the architect."
    ]
    if summary:
        description_parts.append("")
        description_parts.append("**Architect's plan:**")
        description_parts.append(summary)
    description = "\n".join(description_parts)
    if len(description) > _DISCORD_DESCRIPTION_MAX:
        description = description[:_DISCORD_DESCRIPTION_MAX].rstrip() + "…"
    return {
        "content": f"\U0001F50D `{slug}` needs review after **{agent_name}**",
        "embeds": [
            {
                "title": f"Approve architecture for {slug}",
                "url": dashboard_url or None,
                "description": description,
            }
        ],
    }


async def _post_via_bot(channel_id: str, payload: dict) -> Optional[dict]:
    """POST a message via the bot. Returns the parsed JSON response on
    success (so callers can pluck out message id / thread id), or
    ``None`` on failure."""
    token = _discord_token()
    if not token:
        return None
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            try:
                parsed = response.json()
                return parsed if isinstance(parsed, dict) else {}
            except Exception:  # noqa: BLE001
                return {}
    except Exception:  # noqa: BLE001
        logger.warning("discord bot-channel dispatch failed", exc_info=True)
        return None


async def start_thread_for_message(channel_id: str, message_id: str, name: str) -> Optional[str]:
    """Open a public thread off an existing message. Returns the thread id."""
    token = _discord_token()
    if not token or not channel_id or not message_id:
        return None
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}/threads"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    # Auto-archive after 1 day of inactivity (1440 minutes).
    body = {"name": name[:100] or "skyn3t-project", "auto_archive_duration": 1440}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            return str(data.get("id") or "") or None
    except Exception:  # noqa: BLE001
        logger.warning("discord start-thread failed", exc_info=True)
        return None


async def post_to_thread(thread_id: str, content: str) -> bool:
    """Post a follow-up reply into an existing thread."""
    token = _discord_token()
    if not token or not thread_id:
        return False
    url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=headers, json={"content": content})
            response.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("discord post-to-thread failed", exc_info=True)
        return False


async def _post_via_webhook(webhook: str, payload: dict) -> bool:
    # Webhooks reject the ``components`` key (only bot applications can
    # send interactive components). Strip it for webhook delivery.
    safe_payload = {k: v for k, v in payload.items() if k != "components"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(webhook, json=safe_payload)
            response.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("discord webhook dispatch failed", exc_info=True)
        return False


async def dispatch(
    slug: str,
    agent_name: str,
    dashboard_url: str,
    cfg: Dict[str, Any],
    artifact_dir: Optional[Path] = None,
) -> Dict[str, bool]:
    """Send notifications about a pending approval.

    Returns ``{channel_name: True/False}`` for each configured channel
    that was attempted. Empty/missing config → ``{}`` (silent no-op).

    Routing:
    * If ``discord_bot_channel_id`` is set AND ``DISCORD_TOKEN`` is
      configured → bot-channel POST with interactive buttons.
    * Else if ``discord_webhook`` is set → fallback webhook (no buttons).

    ``artifact_dir`` — if provided, the dispatcher pulls a summary from
    ``<artifact_dir>/architecture.md`` and includes it in the embed.
    """
    notify_cfg = (cfg or {}).get("notify") or {}
    webhook = str(notify_cfg.get("discord_webhook") or "").strip()
    channel_id = str(notify_cfg.get("discord_bot_channel_id") or "").strip()

    if not webhook and not channel_id:
        return {}

    key = (slug, agent_name)
    async with _lock:
        last = _last_dispatch.get(key, 0.0)
        now = time.monotonic()
        if now - last < _THROTTLE_WINDOW_SECONDS:
            return {"discord": False, "throttled": True}
        _last_dispatch[key] = now

    summary = _summarize_architecture(artifact_dir)
    payload = _build_embed_payload(slug, agent_name, dashboard_url, summary)

    # Prefer bot channel (buttons work + thread support).
    if channel_id and _discord_token():
        payload_with_buttons = {**payload, "components": _build_components(slug, dashboard_url)}
        message = await _post_via_bot(channel_id, payload_with_buttons)
        if message is not None:
            result: Dict[str, Any] = {"discord": True}
            message_id = str(message.get("id") or "")
            if message_id:
                result["message_id"] = message_id
                # Spawn a thread off this message so all subsequent
                # updates for this project nest under it.
                thread_id = await start_thread_for_message(channel_id, message_id, slug)
                if thread_id:
                    result["thread_id"] = thread_id
                    result["channel_id"] = channel_id
            return result
        # Bot delivery failed → fall through to webhook fallback.

    if webhook:
        if await _post_via_webhook(webhook, payload):
            return {"discord": True}

    return {"discord": False}
