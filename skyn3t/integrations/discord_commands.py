"""Discord control surface — slash commands, DMs, button presses.

The web entrypoint at ``/api/discord/interactions`` calls
``handle_interaction(payload)``. The Gateway bot calls ``handle_dm`` for
free-text messages and ``handle_button`` is unused right now (the
button presses arrive via the HTTP interactions endpoint too, since
that's how Discord routes message components).

Auth model: anyone in the bot's Discord server can issue commands.
Per-user rate-limiting (5 commands/min) prevents accidental abuse.

The dispatcher itself doesn't import FastAPI or the runner directly —
it takes a runner instance via ``handle_interaction(payload, runner)``
so it's easy to test with fakes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import httpx

from skyn3t.config.settings import get_settings
from skyn3t.integrations.discord_intent import Intent, parse as parse_intent

logger = logging.getLogger(__name__)


# Discord interaction types (https://discord.com/developers/docs/interactions/receiving-and-responding)
INTERACTION_PING = 1
INTERACTION_APPLICATION_COMMAND = 2
INTERACTION_MESSAGE_COMPONENT = 3
INTERACTION_MODAL_SUBMIT = 5

# Interaction response types
RESPONSE_PONG = 1
RESPONSE_CHANNEL_MESSAGE_WITH_SOURCE = 4
RESPONSE_DEFERRED_CHANNEL_MESSAGE = 5
RESPONSE_DEFERRED_UPDATE_MESSAGE = 6
RESPONSE_UPDATE_MESSAGE = 7
RESPONSE_MODAL = 9

FLAG_EPHEMERAL = 1 << 6  # 64


# ---------------------------------------------------------------------------
# Rate limiter (per Discord user id)
# ---------------------------------------------------------------------------

_RATE_LIMIT_WINDOW_SECONDS = 60.0
_RATE_LIMIT_MAX = 5
_user_calls: Dict[str, deque] = {}
_rate_lock = asyncio.Lock()


async def _rate_limited(user_id: str) -> bool:
    if not user_id:
        return False
    now = time.monotonic()
    async with _rate_lock:
        bucket = _user_calls.setdefault(user_id, deque())
        while bucket and now - bucket[0] > _RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT_MAX:
            return True
        bucket.append(now)
        return False


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def verify_signature(public_key_hex: str, signature: str, timestamp: str, body: bytes) -> bool:
    """Verify an inbound Discord interaction signature.

    Discord signs ``timestamp + body`` with Ed25519 using the
    application's public key. Returns True on valid signature.
    """
    if not public_key_hex or not signature or not timestamp:
        return False
    try:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        verify_key.verify(timestamp.encode("utf-8") + body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError, Exception):  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Slash command schemas (registered via /api/discord/register-commands)
# ---------------------------------------------------------------------------

SLASH_COMMANDS: list[dict] = [
    {
        "name": "skyn3t-start",
        "description": "Start a new SkyN3t studio project",
        "options": [
            {"type": 3, "name": "brief", "description": "Plain-English brief", "required": True},
            {"type": 3, "name": "slug", "description": "Optional slug", "required": False},
            {"type": 3, "name": "template", "description": "Template (default: auto)", "required": False},
        ],
    },
    {
        "name": "skyn3t-status",
        "description": "Check status of a SkyN3t project",
        "options": [
            {"type": 3, "name": "slug", "description": "Project slug", "required": True},
        ],
    },
    {
        "name": "skyn3t-approve",
        "description": "Approve a project's architecture and resume the pipeline",
        "options": [
            {"type": 3, "name": "slug", "description": "Project slug", "required": True},
        ],
    },
    {
        "name": "skyn3t-reject",
        "description": "Reject a project's architecture (re-runs Architect)",
        "options": [
            {"type": 3, "name": "slug", "description": "Project slug", "required": True},
            {"type": 3, "name": "feedback", "description": "What was wrong", "required": True},
        ],
    },
    {
        "name": "skyn3t-list",
        "description": "List recent SkyN3t projects",
    },
]


async def register_slash_commands(application_id: str, bot_token: str) -> dict:
    """Idempotently register the slash command set with Discord.

    Returns ``{"ok": True, "commands": [...]}`` on success.
    Safe to re-run — Discord upserts by name.
    """
    if not application_id or not bot_token:
        raise ValueError("application_id and bot_token are required")
    url = f"https://discord.com/api/v10/applications/{application_id}/commands"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.put(url, headers=headers, json=SLASH_COMMANDS)
        response.raise_for_status()
        return {"ok": True, "commands": response.json()}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    """A Discord interaction response payload + an optional follow-up coroutine.

    Discord requires a response within 3 seconds. For long-running work
    (like starting a project), return a deferred response and let the
    follow-up coroutine post the result after the work finishes.
    """
    response: dict
    follow_up: Optional[Any] = None  # callable returning coroutine, or None


async def handle_interaction(payload: dict, runner: Any) -> DispatchResult:
    """Main HTTP-interactions dispatcher. Handles slash commands,
    message-component (button) presses, and modal submits.

    Returns a ``DispatchResult`` with the Discord response payload and
    optionally a coroutine factory the caller schedules in the
    background after responding.
    """
    interaction_type = int(payload.get("type", 0))

    if interaction_type == INTERACTION_PING:
        return DispatchResult(response={"type": RESPONSE_PONG})

    user_id = _extract_user_id(payload)
    if await _rate_limited(user_id):
        return DispatchResult(response=_ephemeral_reply(
            "Slow down — you're sending commands too fast. Try again in a moment."
        ))

    if interaction_type == INTERACTION_APPLICATION_COMMAND:
        return await _handle_slash(payload, runner, user_id)

    if interaction_type == INTERACTION_MESSAGE_COMPONENT:
        return await _handle_component(payload, runner, user_id)

    if interaction_type == INTERACTION_MODAL_SUBMIT:
        return await _handle_modal(payload, runner, user_id)

    return DispatchResult(response=_ephemeral_reply("Unrecognized interaction type."))


# ---------------------------------------------------------------------------
# Slash handling
# ---------------------------------------------------------------------------


async def _handle_slash(payload: dict, runner: Any, user_id: str) -> DispatchResult:
    data = payload.get("data") or {}
    name = str(data.get("name") or "")
    options = {opt["name"]: opt.get("value") for opt in (data.get("options") or [])}

    if name == "skyn3t-start":
        brief = str(options.get("brief") or "").strip()
        slug = (options.get("slug") or None)
        template = str(options.get("template") or "auto")
        if not brief:
            return DispatchResult(response=_ephemeral_reply("Brief is required."))
        return DispatchResult(
            response={"type": RESPONSE_DEFERRED_CHANNEL_MESSAGE, "data": {"flags": FLAG_EPHEMERAL}},
            follow_up=lambda: _kickoff_and_follow_up(payload, runner, brief, slug, template, user_id),
        )

    if name == "skyn3t-status":
        slug = str(options.get("slug") or "").strip()
        if not slug:
            return DispatchResult(response=_ephemeral_reply("Slug is required."))
        proj = runner.get_project(slug) if hasattr(runner, "get_project") else None
        if proj is None:
            return DispatchResult(response=_ephemeral_reply(f"Project `{slug}` not found."))
        return DispatchResult(response=_ephemeral_reply(_format_status(proj)))

    if name == "skyn3t-approve":
        slug = str(options.get("slug") or "").strip()
        if not slug:
            return DispatchResult(response=_ephemeral_reply("Slug is required."))
        return DispatchResult(
            response={"type": RESPONSE_DEFERRED_CHANNEL_MESSAGE, "data": {"flags": FLAG_EPHEMERAL}},
            follow_up=lambda: _resume_and_follow_up(payload, runner, slug, "approve", None, None, user_id),
        )

    if name == "skyn3t-reject":
        slug = str(options.get("slug") or "").strip()
        feedback = str(options.get("feedback") or "").strip()
        if not slug or not feedback:
            return DispatchResult(response=_ephemeral_reply("Slug and feedback are required."))
        return DispatchResult(
            response={"type": RESPONSE_DEFERRED_CHANNEL_MESSAGE, "data": {"flags": FLAG_EPHEMERAL}},
            follow_up=lambda: _resume_and_follow_up(payload, runner, slug, "reject", None, feedback, user_id),
        )

    if name == "skyn3t-list":
        projects = runner.list_projects() if hasattr(runner, "list_projects") else []
        return DispatchResult(response=_ephemeral_reply(_format_project_list(projects)))

    return DispatchResult(response=_ephemeral_reply(f"Unknown command: {name}"))


# ---------------------------------------------------------------------------
# Component (button) handling
# ---------------------------------------------------------------------------


async def _handle_component(payload: dict, runner: Any, user_id: str) -> DispatchResult:
    data = payload.get("data") or {}
    custom_id = str(data.get("custom_id") or "")
    if ":" not in custom_id:
        return DispatchResult(response=_ephemeral_reply("Malformed button id."))

    action, _, slug = custom_id.partition(":")
    slug = slug.strip()
    if not slug:
        return DispatchResult(response=_ephemeral_reply("Missing slug in button id."))

    if action == "approve":
        return DispatchResult(
            response={"type": RESPONSE_DEFERRED_UPDATE_MESSAGE},
            follow_up=lambda: _resume_and_update_message(
                payload, runner, slug, "approve", None, None, user_id
            ),
        )

    if action == "reject":
        # Pop a modal asking for feedback. The modal submit then runs the resume.
        return DispatchResult(response={
            "type": RESPONSE_MODAL,
            "data": {
                "custom_id": f"reject_modal:{slug}",
                "title": f"Reject {slug}",
                "components": [{
                    "type": 1,
                    "components": [{
                        "type": 4,  # text input
                        "custom_id": "feedback",
                        "label": "What's wrong?",
                        "style": 2,  # paragraph
                        "required": True,
                        "max_length": 1000,
                    }],
                }],
            },
        })

    return DispatchResult(response=_ephemeral_reply(f"Unknown button action: {action}"))


async def _handle_modal(payload: dict, runner: Any, user_id: str) -> DispatchResult:
    data = payload.get("data") or {}
    custom_id = str(data.get("custom_id") or "")
    if not custom_id.startswith("reject_modal:"):
        return DispatchResult(response=_ephemeral_reply("Unknown modal."))
    slug = custom_id.split(":", 1)[1].strip()
    feedback = ""
    for row in data.get("components") or []:
        for inner in row.get("components") or []:
            if inner.get("custom_id") == "feedback":
                feedback = str(inner.get("value") or "").strip()
    if not slug or not feedback:
        return DispatchResult(response=_ephemeral_reply("Slug or feedback missing."))
    return DispatchResult(
        response={"type": RESPONSE_DEFERRED_CHANNEL_MESSAGE, "data": {"flags": FLAG_EPHEMERAL}},
        follow_up=lambda: _resume_and_follow_up(payload, runner, slug, "reject", None, feedback, user_id),
    )


# ---------------------------------------------------------------------------
# DM handling (Gateway-side, called from discord_bot.py)
# ---------------------------------------------------------------------------


async def handle_dm(text: str, user_id: str, runner: Any) -> str:
    """Parse a free-text DM/mention and run the requested action.

    Returns a plain-text reply the bot posts back to the user.
    """
    if await _rate_limited(user_id):
        return "Slow down — you're sending commands too fast. Try again in a moment."

    intent = parse_intent(text)
    if intent.action == "help":
        return _help_text()

    if intent.action == "list":
        projects = runner.list_projects() if hasattr(runner, "list_projects") else []
        return _format_project_list(projects)

    if intent.action == "start":
        brief = (intent.brief or "").strip()
        if not brief:
            return "Tell me what to build — e.g. 'build a homelab dashboard'."
        try:
            slug = await _kickoff_project(runner, brief, intent.slug, template="auto", source_user=user_id)
            return f"🚀 Started `{slug}`. I'll ping you when the architect needs review."
        except Exception as exc:  # noqa: BLE001
            logger.exception("DM start failed")
            return f"Couldn't start project: {exc}"

    if intent.action == "status":
        slug = intent.slug or _most_recent_slug(runner)
        if not slug:
            return "No projects yet. Try: `start a todo app`."
        proj = runner.get_project(slug) if hasattr(runner, "get_project") else None
        if proj is None:
            return f"Project `{slug}` not found."
        return _format_status(proj)

    if intent.action == "approve":
        slug = intent.slug or _most_recent_awaiting_slug(runner)
        if not slug:
            return "No project is awaiting approval right now."
        try:
            await _resume_project(runner, slug, "approve", None, None, source_user=user_id)
            return f"✅ Approved `{slug}` — pipeline resuming."
        except Exception as exc:  # noqa: BLE001
            logger.exception("DM approve failed")
            return f"Couldn't approve: {exc}"

    if intent.action == "reject":
        slug = intent.slug or _most_recent_awaiting_slug(runner)
        if not slug:
            return "No project is awaiting approval right now."
        feedback = intent.feedback or "Rejected via Discord without specific feedback."
        try:
            await _resume_project(runner, slug, "reject", None, feedback, source_user=user_id)
            return f"❌ Rejected `{slug}` — architect will re-run with your feedback."
        except Exception as exc:  # noqa: BLE001
            logger.exception("DM reject failed")
            return f"Couldn't reject: {exc}"

    return _help_text()


# ---------------------------------------------------------------------------
# Runner adapters
# ---------------------------------------------------------------------------


async def _kickoff_project(
    runner: Any,
    brief: str,
    slug: Optional[str],
    template: str = "auto",
    source_user: str = "",
) -> str:
    """Wraps StudioRunner.reserve_project + start. Returns the created slug.

    Runs reserve_project off the loop (it does sync subprocess work) and
    schedules the long-running ``start`` coroutine in the background so
    the caller can return a response quickly.
    """
    manifest = await asyncio.to_thread(
        runner.reserve_project, template, brief, slug=slug
    )
    new_slug = str(manifest.get("slug") or "")
    extra = {"source": f"discord:{source_user}"} if source_user else {}
    asyncio.create_task(runner.start(template, brief, slug=new_slug, extra=extra))
    return new_slug


async def _resume_project(
    runner: Any,
    slug: str,
    decision: str,
    edited_md: Optional[str],
    feedback: Optional[str],
    source_user: str = "",
) -> dict:
    """Wraps StudioRunner.resume_after_approval. Tags approval history
    with the originating Discord user.
    """
    proj = runner.get_project(slug) if hasattr(runner, "get_project") else None
    if proj is None:
        raise FileNotFoundError(slug)
    return await runner.resume_after_approval(
        slug, decision, edited_md=edited_md, feedback=feedback
    )


def _most_recent_slug(runner: Any) -> Optional[str]:
    try:
        projects = runner.list_projects() if hasattr(runner, "list_projects") else []
    except Exception:  # noqa: BLE001
        return None
    if not projects:
        return None
    # Newest first by updated_at if present, else by creation_time
    def sort_key(p: dict) -> float:
        for k in ("updated_at", "created_at"):
            v = p.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0
    sorted_projects = sorted(projects, key=sort_key, reverse=True)
    return str(sorted_projects[0].get("slug") or "") or None


def _most_recent_awaiting_slug(runner: Any) -> Optional[str]:
    try:
        projects = runner.list_projects() if hasattr(runner, "list_projects") else []
    except Exception:  # noqa: BLE001
        return None
    waiting = [p for p in projects if str(p.get("status") or "") == "awaiting_approval"]
    if not waiting:
        return None
    def sort_key(p: dict) -> float:
        for k in ("updated_at", "created_at"):
            v = p.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0
    return str(sorted(waiting, key=sort_key, reverse=True)[0].get("slug") or "") or None


# ---------------------------------------------------------------------------
# Long-running follow-ups (Discord requires response within 3s)
# ---------------------------------------------------------------------------


async def _kickoff_and_follow_up(
    payload: dict, runner: Any, brief: str, slug: Optional[str], template: str, user_id: str
) -> None:
    try:
        new_slug = await _kickoff_project(runner, brief, slug, template, source_user=user_id)
        text = f"🚀 Started `{new_slug}`. I'll ping you when architect needs review."
    except Exception as exc:  # noqa: BLE001
        logger.exception("slash start failed")
        text = f"Couldn't start: {exc}"
    await _post_followup(payload, text)


async def _resume_and_follow_up(
    payload: dict, runner: Any, slug: str, decision: str,
    edited_md: Optional[str], feedback: Optional[str], user_id: str,
) -> None:
    try:
        await _resume_project(runner, slug, decision, edited_md, feedback, source_user=user_id)
        verb = "Approved" if decision == "approve" else "Rejected"
        emoji = "✅" if decision == "approve" else "❌"
        text = f"{emoji} {verb} `{slug}`."
    except FileNotFoundError:
        text = f"Project `{slug}` not found."
    except Exception as exc:  # noqa: BLE001
        logger.exception("slash resume failed")
        text = f"Couldn't {decision}: {exc}"
    await _post_followup(payload, text)


async def _resume_and_update_message(
    payload: dict, runner: Any, slug: str, decision: str,
    edited_md: Optional[str], feedback: Optional[str], user_id: str,
) -> None:
    """For button presses: edit the original embed message in-place after the resume completes."""
    try:
        await _resume_project(runner, slug, decision, edited_md, feedback, source_user=user_id)
        verb = "Approved" if decision == "approve" else "Rejected"
        emoji = "✅" if decision == "approve" else "❌"
        username = _extract_username(payload) or "Discord user"
        text = f"{emoji} {verb} `{slug}` by {username}."
    except FileNotFoundError:
        text = f"Project `{slug}` not found."
    except Exception as exc:  # noqa: BLE001
        logger.exception("button resume failed")
        text = f"Couldn't {decision}: {exc}"
    await _edit_original_response(payload, text)


async def _post_followup(payload: dict, content: str) -> None:
    """POST to /webhooks/{app_id}/{token} to send a follow-up after a deferred response."""
    settings = get_settings()
    app_id = settings.discord_application_id or payload.get("application_id")
    token = payload.get("token")
    if not app_id or not token:
        logger.warning("can't post follow-up: missing app_id or interaction token")
        return
    url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={"content": content, "flags": FLAG_EPHEMERAL})
    except Exception:  # noqa: BLE001
        logger.exception("discord follow-up post failed")


async def _edit_original_response(payload: dict, content: str) -> None:
    settings = get_settings()
    app_id = settings.discord_application_id or payload.get("application_id")
    token = payload.get("token")
    if not app_id or not token:
        return
    url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.patch(url, json={"content": content, "embeds": [], "components": []})
    except Exception:  # noqa: BLE001
        logger.exception("discord edit-original failed")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _extract_user_id(payload: dict) -> str:
    member = payload.get("member") or {}
    user = (member.get("user") if isinstance(member, dict) else None) or payload.get("user") or {}
    return str(user.get("id") or "")


def _extract_username(payload: dict) -> str:
    member = payload.get("member") or {}
    user = (member.get("user") if isinstance(member, dict) else None) or payload.get("user") or {}
    name = user.get("global_name") or user.get("username") or ""
    return f"@{name}" if name else ""


def _ephemeral_reply(content: str) -> dict:
    return {
        "type": RESPONSE_CHANNEL_MESSAGE_WITH_SOURCE,
        "data": {"content": content, "flags": FLAG_EPHEMERAL},
    }


def _format_status(proj: dict) -> str:
    slug = proj.get("slug") or "?"
    status = proj.get("status") or "?"
    stage = proj.get("current_stage") or proj.get("next_action") or ""
    score = proj.get("review_score")
    lines = [f"**{slug}** — {status}"]
    if stage:
        lines.append(f"Stage: {stage}")
    if score is not None:
        lines.append(f"Score: {score}/100")
    return "\n".join(lines)


def _format_project_list(projects: list) -> str:
    if not projects:
        return "No projects yet."
    def sort_key(p: dict) -> float:
        for k in ("updated_at", "created_at"):
            v = p.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0
    sorted_projects = sorted(projects, key=sort_key, reverse=True)[:10]
    lines = ["**Recent projects:**"]
    for p in sorted_projects:
        slug = p.get("slug") or "?"
        status = p.get("status") or "?"
        lines.append(f"• `{slug}` — {status}")
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "**SkyN3t Discord commands:**\n"
        "• `build a homelab dashboard` — start a project\n"
        "• `status canary-150` — check progress\n"
        "• `approve` (or `approve canary-150`) — approve the latest awaiting project\n"
        "• `reject canary-150 the palette is wrong` — reject with feedback\n"
        "• `list` — show recent projects\n"
        "\nOr use slash commands: `/skyn3t-start`, `/skyn3t-status`, `/skyn3t-approve`, `/skyn3t-reject`, `/skyn3t-list`"
    )
