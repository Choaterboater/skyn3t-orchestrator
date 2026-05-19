"""Telegram outbound dispatcher for the studio control surface.

Solo-DM model: messages go to a single Telegram user (the one whose ID
is in ``SKYN3T_TELEGRAM_USER_ID``). Inline keyboards carry Approve /
Reject / Status buttons. All subsequent updates for a project reply to
the original message so it nests visually in the chat — the closest
Telegram analogue to a Discord thread.

This module is HTTP-only (httpx); it does not handle inbound polling —
see ``telegram_bot.py`` for that.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot"
_HTTP_TIMEOUT_SECONDS = 5.0
_THROTTLE_WINDOW_SECONDS = 60.0
_TELEGRAM_MESSAGE_MAX = 3800  # 4096 cap; leave room for header + footer
_last_dispatch: Dict[Tuple[str, str], float] = {}
_lock = asyncio.Lock()


_PRIORITY_SECTIONS = ("overview", "components", "data model", "apis", "api")
_SKIP_SECTIONS = ("non-goals", "out of scope", "appendix", "references")

_PLAIN_ENGLISH_SYSTEM = (
    "You translate technical software architecture into plain English "
    "that a non-developer can understand. Be concrete and brief. "
    "No jargon: explain CRUD as 'add/edit/check off/delete', not 'CRUD'. "
    "Explain APIs as 'the back-end URLs the app calls'. "
    "Explain database tables as 'where it stores X'. "
    "3-5 short sentences. End with a one-line recommendation: "
    "'Looks right for the request — tap Approve.' or "
    "'Doesn't match what you asked for — tap Reject and tell the architect what to change.'"
)


async def _plain_english_summary(brief: str, architecture_md: str) -> str:
    """Ask the LLM for a 3-5 sentence plain-English translation of the
    architect's plan. Returns "" on any error so the caller can fall back
    to the raw technical summary."""
    if not architecture_md.strip():
        return ""
    try:
        from skyn3t.adapters.llm_client import LLMClient
        client = LLMClient(default_model=None, backend=None)
        prompt = (
            f"BRIEF FROM USER:\n{brief.strip()}\n\n"
            f"ARCHITECT'S TECHNICAL PLAN:\n{architecture_md[:4000]}\n\n"
            "Translate the architect's plan into plain English for the "
            "non-developer who wrote the brief. Match the brief — if "
            "they asked for a homelab dashboard but the plan describes "
            "a todo app, flag the mismatch in your recommendation."
        )
        text = await client.complete(
            prompt,
            system=_PLAIN_ENGLISH_SYSTEM,
            max_tokens=350,
            temperature=0.2,
            timeout=20.0,
        )
        return (text or "").strip()
    except Exception:  # noqa: BLE001
        logger.warning("plain-english summary generation failed", exc_info=True)
        return ""


def _summarize_architecture(artifact_dir: Optional[Path]) -> str:
    """Pull the decision-relevant portions of architecture.md.

    Decision-grade sections are Overview, Components, Data model, and
    APIs — those are what the user is approving. We include them in
    that order, cap at ~3000 chars so the Telegram message stays under
    the 4096 limit even with the boilerplate header/footer.
    """
    if not artifact_dir:
        return ""
    arch_path = Path(artifact_dir) / "architecture.md"
    if not arch_path.exists():
        return ""
    try:
        text = arch_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    # Group lines by section header so we can pick the ones we want.
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current = "__intro__"
    sections[current] = []
    order.append(current)
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            current = heading
            if current not in sections:
                sections[current] = []
                order.append(current)
            continue
        sections[current].append(line)

    chosen: list[str] = []
    used = set()
    for key in _PRIORITY_SECTIONS:
        for sec in order:
            if sec in used or sec in _SKIP_SECTIONS:
                continue
            if sec == key or sec.startswith(key + " "):
                body = "\n".join(sections.get(sec, [])).strip()
                if body:
                    chosen.append(f"### {sec.title()}\n{body}")
                    used.add(sec)
                break

    # Fall back to whole document if no recognized sections.
    if not chosen:
        summary = text.strip()
    else:
        summary = "\n\n".join(chosen)

    if len(summary) > 3000:
        summary = summary[:3000].rstrip() + "…"
    return summary


def _token_registry_path() -> Path:
    """Persisted slug-token map. Lives under data/ so it survives
    server restarts and works across processes."""
    try:
        from skyn3t.config.settings import get_settings
        return Path(get_settings().data_dir) / "telegram_slug_tokens.json"
    except Exception:  # noqa: BLE001
        return Path("data/telegram_slug_tokens.json")


def _load_token_registry() -> dict[str, str]:
    path = _token_registry_path()
    if not path.exists():
        return {}
    try:
        import json as _json
        data = _json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001
        logger.debug("slug token registry unreadable; starting fresh", exc_info=True)
    return {}


def _save_token_registry(reg: dict[str, str]) -> None:
    path = _token_registry_path()
    try:
        import json as _json
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(_json.dumps(reg, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        logger.warning("slug token registry write failed", exc_info=True)


def _slug_token(slug: str) -> str:
    """Telegram caps inline-keyboard ``callback_data`` at 64 bytes. Long
    slugs like ``a-simple-dashboard-allowing-to-upload-excel-files-...``
    blow past that, so we use the slug itself when short, else a short
    hash. The mapping is persisted under ``data/telegram_slug_tokens.json``
    so callbacks resolve correctly even after a server restart or when
    sent from a separate process (e.g. a one-off CLI script)."""
    if len(slug) + len("approve:") <= 60:  # leave headroom
        return slug
    import hashlib
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:12]
    reg = _load_token_registry()
    if reg.get(digest) != slug:
        reg[digest] = slug
        _save_token_registry(reg)
    return digest


def resolve_slug_token(token: str) -> str:
    """Inverse of ``_slug_token``: turn a callback token back into a
    full slug. Returns the token unchanged if it's already a slug."""
    reg = _load_token_registry()
    return reg.get(token, token)


def _is_publicly_routable(url: str) -> bool:
    """Telegram rejects inline-keyboard URL buttons whose URL points to
    localhost/127.0.0.1/private hosts — the message fails with 400. So
    we omit the dashboard button unless the URL is public-looking.
    """
    if not url:
        return False
    lowered = url.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return False
    for blocked in ("localhost", "127.0.0.1", "0.0.0.0", "://192.168.", "://10.", "://172.16.", "://172.17.", "://172.18.", "://172.19.", "://172.2", "://172.30.", "://172.31."):
        if blocked in lowered:
            return False
    return True


def _build_inline_keyboard(slug: str, dashboard_url: str = "") -> dict:
    """Approve / Reject / Status buttons. Reject prompts the user to
    reply with feedback. The Open-dashboard button is included only
    when the URL is publicly reachable — Telegram refuses localhost
    URLs in inline keyboards. Callback data is capped at 64 bytes by
    Telegram, so long slugs are hashed and resolved on receipt."""
    token = _slug_token(slug)
    keyboard: list[list[dict]] = [
        [
            {"text": "✅ Approve", "callback_data": f"approve:{token}"},
            {"text": "❌ Reject", "callback_data": f"reject:{token}"},
        ],
        [
            {"text": "📊 Status", "callback_data": f"status:{token}"},
        ],
    ]
    if _is_publicly_routable(dashboard_url):
        keyboard.append([{"text": "🔗 Open dashboard", "url": dashboard_url}])
    return {"inline_keyboard": keyboard}


def _format_approval_message(
    slug: str,
    agent_name: str,
    summary: str,
    dashboard_url: str,
    plain_english: str = "",
) -> str:
    parts = [
        f"🔍 *{slug}* needs review",
        "",
        "Tap *Approve* to continue, *Reject* to send feedback back to the architect.",
    ]
    if plain_english:
        parts.append("")
        parts.append("─── *What this means* ───")
        parts.append(plain_english)
    if summary:
        parts.append("")
        parts.append("─── *Technical plan* ───")
        parts.append(summary)
    if dashboard_url and _is_publicly_routable(dashboard_url):
        parts.append("")
        parts.append(f"Dashboard: {dashboard_url}")
    body = "\n".join(parts)
    if len(body) > _TELEGRAM_MESSAGE_MAX:
        body = body[: _TELEGRAM_MESSAGE_MAX].rstrip() + "…"
    return body


async def _telegram_post(token: str, method: str, payload: dict) -> Optional[dict]:
    """POST to the Bot API. Returns parsed JSON ``result`` on success,
    ``None`` on failure.

    On 400 Bad Request — almost always a Markdown parse error from
    unescaped underscores / asterisks / backticks in stage progress
    text — we retry once with ``parse_mode`` removed so the message
    still goes through as plain text.
    """
    if not token:
        return None
    url = f"{_API_BASE}{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
            if response.status_code == 400 and "parse_mode" in payload:
                # Almost certainly a Markdown parsing failure. Strip
                # parse_mode and retry — message still gets through,
                # just without formatting. Log the original error body
                # so we can tighten the formatter for next time.
                try:
                    err = response.json()
                except Exception:  # noqa: BLE001
                    err = {"raw": response.text[:300]}
                logger.warning(
                    "telegram %s 400 — retrying as plain text. error=%s text_preview=%r",
                    method, err, str(payload.get("text", ""))[:200],
                )
                plain_payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                response = await client.post(url, json=plain_payload)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                logger.warning("telegram %s returned not-ok: %s", method, data)
                return None
            result = data.get("result")
            return result if isinstance(result, dict) else None
    except Exception:  # noqa: BLE001
        logger.warning("telegram %s failed", method, exc_info=True)
        return None


async def send_message(
    token: str,
    chat_id: str,
    text: str,
    reply_to_message_id: Optional[int] = None,
    reply_markup: Optional[dict] = None,
    parse_mode: str = "Markdown",
) -> Optional[dict]:
    """Send a Telegram message. Returns the full ``message`` object on
    success so callers can pluck out ``message_id`` for later replies."""
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await _telegram_post(token, "sendMessage", payload)


async def edit_message(
    token: str,
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: Optional[dict] = None,
    parse_mode: str = "Markdown",
) -> Optional[dict]:
    """Edit an existing message (e.g. to remove buttons after approval)."""
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await _telegram_post(token, "editMessageText", payload)


async def answer_callback(token: str, callback_query_id: str, text: str = "") -> None:
    """Acknowledge a button press so Telegram stops showing the spinner."""
    await _telegram_post(
        token,
        "answerCallbackQuery",
        {"callback_query_id": callback_query_id, "text": text[:200]},
    )


async def dispatch_approval(
    slug: str,
    agent_name: str,
    dashboard_url: str,
    artifact_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Send a project's approval-gate notification to the configured user.

    Returns ``{"ok": bool, "message_id": int | None, "chat_id": str}``.
    """
    try:
        from skyn3t.config.settings import get_settings
        settings = get_settings()
        token = settings.telegram_token
        chat_id = settings.telegram_user_id
    except Exception:  # noqa: BLE001
        return {"ok": False, "message_id": None, "chat_id": ""}

    if not token or not chat_id:
        return {"ok": False, "message_id": None, "chat_id": ""}

    # 60-second throttle per (slug, agent) to avoid stutter when retries
    # fire the same gate multiple times.
    key = (slug, agent_name)
    async with _lock:
        last = _last_dispatch.get(key, 0.0)
        now = time.monotonic()
        if now - last < _THROTTLE_WINDOW_SECONDS:
            return {"ok": False, "throttled": True, "chat_id": chat_id}
        _last_dispatch[key] = now

    summary = _summarize_architecture(artifact_dir)
    # Generate a plain-English translation so the user can decide without
    # parsing the architect's dev jargon. Best-effort: on failure we fall
    # back to the technical summary only.
    plain_english = ""
    if artifact_dir is not None:
        brief = ""
        try:
            import json as _json
            mf = Path(artifact_dir) / "project.json"
            if mf.exists():
                brief = str(_json.loads(mf.read_text(encoding="utf-8")).get("brief") or "")
        except Exception:  # noqa: BLE001
            pass
        full_arch = ""
        try:
            arch_path = Path(artifact_dir) / "architecture.md"
            if arch_path.exists():
                full_arch = arch_path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        if full_arch:
            plain_english = await _plain_english_summary(brief, full_arch)
    text = _format_approval_message(slug, agent_name, summary, dashboard_url, plain_english)
    keyboard = _build_inline_keyboard(slug, dashboard_url)

    message = await send_message(token, chat_id, text, reply_markup=keyboard)
    if message is None:
        return {"ok": False, "message_id": None, "chat_id": chat_id}
    return {
        "ok": True,
        "message_id": int(message.get("message_id") or 0),
        "chat_id": str(chat_id),
    }


async def post_thread_reply(message_id: Optional[int], content: str) -> bool:
    """Reply into a project's thread (i.e. as a reply to its starter
    message). No-op if Telegram isn't configured or ``message_id`` is
    missing.

    Progress messages from the studio runner contain stage names like
    ``consistency_reviewer`` (underscores) and Python class names like
    ``ConsistencyReviewerAgent`` — these unescaped Markdown special
    characters cause Telegram to return 400 on parse. We send these as
    PLAIN TEXT (no parse_mode) so the message always lands.
    """
    if not message_id:
        return False
    try:
        from skyn3t.config.settings import get_settings
        settings = get_settings()
        token = settings.telegram_token
        chat_id = settings.telegram_user_id
    except Exception:  # noqa: BLE001
        return False
    if not token or not chat_id:
        return False
    # Plain text to side-step Markdown parsing entirely. The fallback
    # path in _telegram_post would catch parse errors anyway, but
    # avoiding the round-trip is cheaper and the formatting on these
    # short progress lines doesn't add value.
    payload: dict = {
        "chat_id": str(chat_id),
        "text": content[: _TELEGRAM_MESSAGE_MAX],
        "disable_web_page_preview": True,
        "reply_to_message_id": int(message_id),
        "allow_sending_without_reply": True,
    }
    result = await _telegram_post(token, "sendMessage", payload)
    return result is not None


async def update_starter_message(
    chat_id: Optional[str],
    message_id: Optional[int],
    new_text: str,
    keep_buttons: bool = False,
) -> bool:
    """Edit the original notification — typically called after the user
    decides, so the message reflects the outcome and (by default) the
    buttons disappear."""
    if not chat_id or not message_id:
        return False
    try:
        from skyn3t.config.settings import get_settings
        settings = get_settings()
        token = settings.telegram_token
    except Exception:  # noqa: BLE001
        return False
    if not token:
        return False
    result = await edit_message(
        token, chat_id, int(message_id), new_text[: _TELEGRAM_MESSAGE_MAX],
        reply_markup=None if not keep_buttons else None,
    )
    return result is not None
