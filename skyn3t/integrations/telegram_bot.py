"""Telegram studio control surface — long-polling.

No public URL required: the bot opens an outbound long-poll to Telegram
and receives updates over that connection. Buttons, slash-style
commands, photo uploads, and free-text DMs all flow through this loop.

Solo-DM mode: only one ``allowed_user_id`` may issue commands. Everyone
else is silently ignored.

Commands (slash and plain English both work):
* ``/start`` or ``help`` — show the command list
* ``/build <brief>`` or ``build a homelab dashboard`` — kick off a project
* ``/status [slug]`` or ``status canary-123`` — show project state
* ``/list`` — show the 10 most recent projects
* ``/approve [slug]`` — approve the latest project awaiting approval
* ``/reject [slug] [feedback]`` — reject with feedback
* ``/references`` (or ``/refs``) — list saved design references
* ``/tag <ref_id> <tag1> <tag2>`` — tag a reference
* ``/untag <ref_id> <tag>`` — remove a tag

Photos sent to the bot are saved as design references. Any photo
uploaded within 5 minutes before a ``/build`` auto-attaches to that
project; library references can also be matched by tag overlap.

Inline buttons emit callback queries with ``approve:<slug>`` /
``reject:<slug>`` / ``status:<slug>`` custom data; reject opens a
"force reply" so the user types feedback as a normal message.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from skyn3t.integrations import telegram_dispatch as dispatch
from skyn3t.integrations import telegram_photos as photos
from skyn3t.integrations.discord_intent import parse as parse_intent

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot"
_LONG_POLL_TIMEOUT_SECONDS = 25
_HTTP_TIMEOUT_SECONDS = 30.0  # > long-poll timeout
_RECONNECT_BACKOFF_MIN = 2.0
_RECONNECT_BACKOFF_MAX = 60.0


def _help_text() -> str:
    return (
        "*SkyN3t commands:*\n"
        "• `build a homelab dashboard` — start a project\n"
        "• `status canary-150` — check progress (omit slug for latest)\n"
        "• `approve` (or `approve canary-150`) — approve the latest awaiting project\n"
        "• `reject canary-150 feedback here` — reject and re-run with notes\n"
        "• `list` — recent projects\n\n"
        "Slash forms: `/build`, `/status`, `/approve`, `/reject`, `/list`, `/help`."
    )


def _format_status(proj: dict) -> str:
    slug = proj.get("slug") or "?"
    status = proj.get("status") or "?"
    stage = proj.get("current_stage") or proj.get("next_action") or ""
    score = (proj.get("quality_summary") or {}).get("score")
    verdict = (proj.get("quality_summary") or {}).get("verdict")
    lines = [f"*{slug}* — `{status}`"]
    if stage:
        lines.append(f"Stage: {stage}")
    if score is not None:
        lines.append(f"Score: *{score}/100* — verdict: `{verdict or '?'}`")
    return "\n".join(lines)


def _format_project_list(projects: list) -> str:
    if not projects:
        return "No projects yet. Try: `build a todo app`."

    def sort_key(p: dict) -> float:
        for k in ("updated_at", "created_at"):
            v = p.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0

    recent = sorted(projects, key=sort_key, reverse=True)[:10]
    lines = ["*Recent projects:*"]
    for p in recent:
        slug = p.get("slug") or "?"
        status = p.get("status") or "?"
        lines.append(f"• `{slug}` — {status}")
    return "\n".join(lines)


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

    waiting.sort(key=sort_key, reverse=True)
    return str(waiting[0].get("slug") or "") or None


def _most_recent_slug(runner: Any) -> Optional[str]:
    try:
        projects = runner.list_projects() if hasattr(runner, "list_projects") else []
    except Exception:  # noqa: BLE001
        return None
    if not projects:
        return None

    def sort_key(p: dict) -> float:
        for k in ("updated_at", "created_at"):
            v = p.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0

    return str(sorted(projects, key=sort_key, reverse=True)[0].get("slug") or "") or None


class TelegramBot:
    """Long-polling Telegram bot for the studio control surface."""

    def __init__(
        self,
        token: str,
        allowed_user_id: str,
        studio_runner: Optional[Any] = None,
    ) -> None:
        self.token = token
        self.allowed_user_id = str(allowed_user_id or "").strip()
        self.studio_runner = studio_runner
        self._offset = 0
        self._stop = False
        self._pending_reject_slugs: dict[str, str] = {}  # user_id → slug awaiting reject feedback

    async def start(self) -> None:
        """Run the long-poll loop until ``stop()`` is called."""
        if not self.token:
            raise ValueError("Telegram token is required")
        if not self.allowed_user_id:
            raise ValueError("Allowed user id is required (solo-DM mode)")
        logger.info("Telegram bot starting — allowed_user_id=%s", self.allowed_user_id)

        # Register canonical brand assets so they auto-attach to every
        # new project. Vision extraction runs in the background so the
        # long-poll loop starts immediately.
        try:
            added = photos.register_canonical_references()
            if added:
                logger.info("registered %d canonical brand reference(s)", len(added))
            asyncio.create_task(photos.extract_canonical_references())
        except Exception:
            logger.exception("canonical brand registration failed")
        backoff = _RECONNECT_BACKOFF_MIN
        while not self._stop:
            try:
                updates = await self._poll()
                backoff = _RECONNECT_BACKOFF_MIN
                for update in updates:
                    try:
                        await self._dispatch_update(update)
                    except Exception:
                        logger.exception("telegram update handling failed")
            except asyncio.CancelledError:
                logger.info("Telegram bot cancelled")
                raise
            except Exception:
                logger.exception("telegram poll failed; sleeping %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)

    def stop(self) -> None:
        self._stop = True

    async def _poll(self) -> list[dict]:
        url = f"{_API_BASE}{self.token}/getUpdates"
        payload = {
            "timeout": _LONG_POLL_TIMEOUT_SECONDS,
            "offset": self._offset,
            "allowed_updates": ["message", "callback_query"],
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        if not data.get("ok"):
            logger.warning("getUpdates not-ok: %s", data)
            return []
        updates = data.get("result") or []
        for u in updates:
            uid = int(u.get("update_id") or 0)
            if uid >= self._offset:
                self._offset = uid + 1
        return updates

    # ------------------------------------------------------------------
    # Update dispatching
    # ------------------------------------------------------------------

    async def _dispatch_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return
        if "message" in update:
            await self._handle_message(update["message"])
            return

    def _is_authorized(self, sender: dict) -> bool:
        sender_id = str(sender.get("id") or "")
        return sender_id == self.allowed_user_id

    async def _reply(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[dict] = None,
    ) -> Optional[dict]:
        return await dispatch.send_message(
            self.token, str(chat_id), text,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, message: dict) -> None:
        sender = message.get("from") or {}
        if not self._is_authorized(sender):
            return  # silently ignore strangers
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id") or 0)
        if not chat_id:
            return

        # Photo upload — route to the photo handler. Caption becomes
        # the photo's initial caption (used for tags / "for canary-X").
        photo_payload = message.get("photo")
        if photo_payload and isinstance(photo_payload, list):
            caption = str(message.get("caption") or "").strip()
            await self._handle_photo(chat_id, sender, photo_payload, caption)
            return

        # Document upload (PDFs, brand guidelines, design docs).
        # Filter by MIME type — only images and PDFs land in the
        # reference library. Other doc types (.docx, .zip, etc.) are
        # ignored to keep the library clean.
        document = message.get("document")
        if document and isinstance(document, dict):
            mime = str(document.get("mime_type") or "").lower()
            if mime.startswith("image/") or mime == "application/pdf":
                caption = str(message.get("caption") or "").strip()
                await self._handle_document(chat_id, sender, document, caption)
                return
            else:
                await self._reply(
                    chat_id,
                    f"📎 Got `{document.get('file_name') or 'a file'}` ({mime}), "
                    "but I only handle images and PDFs as design references.",
                )
                return

        text = str(message.get("text") or "").strip()
        if not text:
            return

        # If this user previously tapped Reject, treat the next message
        # as the feedback string for that pending project.
        user_key = str(sender.get("id") or "")
        if user_key in self._pending_reject_slugs:
            slug = self._pending_reject_slugs.pop(user_key)
            await self._do_reject(chat_id, slug, text)
            return

        # Slash commands take precedence over the intent parser.
        if text.startswith("/"):
            await self._handle_slash(chat_id, text)
            return

        intent = parse_intent(text)
        if intent.action == "help":
            await self._reply(chat_id, _help_text())
            return
        if intent.action == "list":
            await self._do_list(chat_id)
            return
        if intent.action == "start":
            brief = (intent.brief or "").strip()
            if not brief:
                await self._reply(chat_id, "Tell me what to build — e.g. `build a homelab dashboard`.")
                return
            await self._do_start(chat_id, brief, intent.slug)
            return
        if intent.action == "status":
            slug = intent.slug or _most_recent_slug(self.studio_runner)
            await self._do_status(chat_id, slug)
            return
        if intent.action == "approve":
            slug = intent.slug or _most_recent_awaiting_slug(self.studio_runner)
            if not slug:
                await self._reply(chat_id, "No project is awaiting approval right now.")
                return
            await self._do_approve(chat_id, slug)
            return
        if intent.action == "reject":
            slug = intent.slug or _most_recent_awaiting_slug(self.studio_runner)
            feedback = (intent.feedback or "").strip()
            if not slug:
                await self._reply(chat_id, "No project is awaiting approval right now.")
                return
            if not feedback:
                await self._reply(chat_id, f"Reply with feedback for `{slug}` and I'll send it back to the architect.")
                self._pending_reject_slugs[user_key] = slug
                return
            await self._do_reject(chat_id, slug, feedback)
            return

        # Unknown — show help.
        await self._reply(chat_id, _help_text())

    async def _handle_slash(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().lstrip("/").split("@", 1)[0]  # strip @botname suffix
        rest = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("start", "help"):
            await self._reply(chat_id, _help_text())
            return
        if cmd == "build":
            if not rest:
                await self._reply(chat_id, "Usage: `/build a homelab dashboard`")
                return
            await self._do_start(chat_id, rest, None)
            return
        if cmd == "list":
            await self._do_list(chat_id)
            return
        if cmd in ("references", "refs"):
            await self._do_references(chat_id)
            return
        if cmd == "tag":
            await self._do_tag(chat_id, rest, remove=False)
            return
        if cmd == "untag":
            await self._do_tag(chat_id, rest, remove=True)
            return
        if cmd == "status":
            slug = rest.split()[0] if rest else _most_recent_slug(self.studio_runner)
            await self._do_status(chat_id, slug)
            return
        if cmd == "approve":
            slug = rest.split()[0] if rest else _most_recent_awaiting_slug(self.studio_runner)
            if not slug:
                await self._reply(chat_id, "No project is awaiting approval right now.")
                return
            await self._do_approve(chat_id, slug)
            return
        if cmd == "reject":
            tokens = rest.split(maxsplit=1)
            slug = tokens[0] if tokens else _most_recent_awaiting_slug(self.studio_runner)
            feedback = tokens[1] if len(tokens) > 1 else ""
            if not slug:
                await self._reply(chat_id, "No project is awaiting approval right now.")
                return
            if not feedback:
                await self._reply(chat_id, f"Reply with feedback for `{slug}` and I'll send it back to the architect.")
                self._pending_reject_slugs[self.allowed_user_id] = slug
                return
            await self._do_reject(chat_id, slug, feedback)
            return

        await self._reply(chat_id, f"Unknown command `/{cmd}`. " + _help_text())

    # ------------------------------------------------------------------
    # Callback (button) handling
    # ------------------------------------------------------------------

    async def _handle_callback(self, callback: dict) -> None:
        sender = callback.get("from") or {}
        if not self._is_authorized(sender):
            await dispatch.answer_callback(self.token, callback.get("id", ""), "Not authorized.")
            return
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id") or 0)
        message_id = int(message.get("message_id") or 0)
        cb_id = str(callback.get("id") or "")

        if ":" not in data:
            await dispatch.answer_callback(self.token, cb_id, "Malformed button.")
            return
        action, _, token = data.partition(":")
        slug = dispatch.resolve_slug_token(token.strip())

        if action == "approve":
            await dispatch.answer_callback(self.token, cb_id, "Approving…")
            await self._do_approve(chat_id, slug, source_message_id=message_id)
            return
        if action == "reject":
            await dispatch.answer_callback(self.token, cb_id, "Reply with feedback.")
            self._pending_reject_slugs[str(sender.get("id") or "")] = slug
            await self._reply(
                chat_id,
                f"❌ Replying as feedback for *{slug}* — type the reason below and send.",
                reply_to_message_id=message_id,
            )
            return
        if action == "status":
            await dispatch.answer_callback(self.token, cb_id)
            await self._do_status(chat_id, slug)
            return

        await dispatch.answer_callback(self.token, cb_id, f"Unknown action: {action}")

    # ------------------------------------------------------------------
    # Studio actions
    # ------------------------------------------------------------------

    async def _do_start(self, chat_id: int, brief: str, slug: Optional[str]) -> None:
        if self.studio_runner is None:
            await self._reply(chat_id, "Studio runner not wired.")
            return
        try:
            manifest = await asyncio.to_thread(
                self.studio_runner.reserve_project, "auto", brief, slug=slug,
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(chat_id, f"Couldn't reserve project: {exc}")
            return
        new_slug = str(manifest.get("slug") or "")
        extra = {"source": f"telegram:{self.allowed_user_id}"}

        # Photo attachment: any photo uploaded by this user in the last
        # 5 minutes auto-attaches. If none, fall back to library search
        # via tag overlap with brief.
        attached_ids = self._attach_references_for_new_project(brief, new_slug)
        attach_note = ""
        if attached_ids:
            attach_note = f"\n📸 Attached design references: {', '.join(attached_ids)}"

        asyncio.create_task(
            self.studio_runner.start("auto", brief, slug=new_slug, extra=extra)
        )
        await self._reply(
            chat_id,
            f"🚀 Started *{new_slug}*. I'll ping you here when the architect needs review.{attach_note}",
        )

    def _attach_references_for_new_project(self, brief: str, slug: str) -> list[str]:
        """Auto-attach references for a newly-created project.

        Priority: recent uploads (last 5 min) > library tag-match.
        Writes attached refs to ``<project_dir>/design_references.md``
        which the DesignerAgent reads later.
        """
        try:
            from skyn3t.config.settings import get_settings
            project_dir = Path(get_settings().projects_dir) / slug
        except Exception:  # noqa: BLE001
            return []

        # Priority order:
        # 1. Photos uploaded in the last 5 min — explicit recent intent
        # 2. Library references whose tags overlap with the brief
        #
        # NOTE: we deliberately do NOT auto-attach the canonical brand
        # references here. Those represent SkyN3t's own product identity
        # (the cyan-on-black logo, dashboard look) — they should not
        # leak into every user-built project. Each project should pick
        # its own aesthetic from photos the user explicitly attaches.
        # The canonical brand is reserved for the dashboard UI itself.
        candidates = photos.recent_uploads(self.allowed_user_id)
        if not candidates:
            candidates = photos.match_references_to_brief(brief, self.allowed_user_id)
        if not candidates:
            return []

        # Filter out anything tagged "canonical" — even if a tag match
        # happens to pull one in, we don't want SkyN3t's brand to drive
        # user-built projects unless the user explicitly attaches it.
        candidates = [c for c in candidates if "canonical" not in (c.tags or [])]
        if not candidates:
            return []

        entry_ids = [c.id for c in candidates[:3]]
        try:
            photos.attach_references_to_project(project_dir, entry_ids)
        except Exception:
            logger.exception("attach references failed")
            return []
        return entry_ids

    async def _do_status(self, chat_id: int, slug: Optional[str]) -> None:
        if not slug:
            await self._reply(chat_id, "No projects yet. Try: `build a todo app`.")
            return
        if self.studio_runner is None:
            await self._reply(chat_id, "Studio runner not wired.")
            return
        proj = self.studio_runner.get_project(slug)
        if proj is None:
            await self._reply(chat_id, f"Project `{slug}` not found.")
            return
        await self._reply(chat_id, _format_status(proj))

    async def _do_list(self, chat_id: int) -> None:
        if self.studio_runner is None:
            await self._reply(chat_id, "Studio runner not wired.")
            return
        projects = self.studio_runner.list_projects() if hasattr(self.studio_runner, "list_projects") else []
        await self._reply(chat_id, _format_project_list(projects))

    async def _do_approve(
        self, chat_id: int, slug: str, source_message_id: Optional[int] = None
    ) -> None:
        if self.studio_runner is None:
            await self._reply(chat_id, "Studio runner not wired.")
            return
        proj = self.studio_runner.get_project(slug)
        if proj is None:
            await self._reply(chat_id, f"Project `{slug}` not found.")
            return
        try:
            await self.studio_runner.resume_after_approval(slug, "approve")
        except Exception as exc:  # noqa: BLE001
            await self._reply(chat_id, f"Couldn't approve: {exc}")
            return
        await self._reply(
            chat_id,
            f"✅ Approved *{slug}* — pipeline resuming.",
            reply_to_message_id=source_message_id,
        )

    async def _do_reject(self, chat_id: int, slug: str, feedback: str) -> None:
        if self.studio_runner is None:
            await self._reply(chat_id, "Studio runner not wired.")
            return
        proj = self.studio_runner.get_project(slug)
        if proj is None:
            await self._reply(chat_id, f"Project `{slug}` not found.")
            return
        try:
            await self.studio_runner.resume_after_approval(slug, "reject", feedback=feedback)
        except Exception as exc:  # noqa: BLE001
            await self._reply(chat_id, f"Couldn't reject: {exc}")
            return
        await self._reply(
            chat_id,
            f"❌ Rejected *{slug}* — architect re-running with your feedback.",
        )

    # ------------------------------------------------------------------
    # Photo handling + reference library
    # ------------------------------------------------------------------

    async def _handle_photo(
        self, chat_id: int, sender: dict, photo_payload: list, caption: str,
    ) -> None:
        """Ingest a user-uploaded photo as a design reference, then
        kick off vision extraction in the background so the bot can
        reply fast and the LLM call doesn't block the polling loop.

        If the caption is a build verb ("build a habit tracker"), we
        also kick off a project after the photo is saved — the photo
        will auto-attach via the 5-min recent-uploads window. This
        means users can send a photo + caption to start a build in a
        single message instead of two."""
        # Separate the build intent (if any) from caption-as-tags.
        caption_intent = parse_intent(caption) if caption else None
        is_build_caption = (
            caption_intent is not None
            and caption_intent.action == "start"
            and (caption_intent.brief or "").strip()
        )
        # If caption is a build command, don't use it as tags (the verb
        # words "build" "a" "with" are useless as design-reference tags).
        ingest_caption = "" if is_build_caption else caption

        result = await photos.ingest_telegram_photo(
            self.token, self.allowed_user_id, photo_payload, ingest_caption,
        )
        if not result.ok or result.entry is None:
            await self._reply(
                chat_id,
                f"Couldn't save that photo: {result.error or 'unknown error'}",
            )
            return
        entry = result.entry
        # Tell the user it landed, then run vision extraction in
        # background; the bot will edit/append once it's done.
        tag_hint = f" Tags: `{', '.join(entry.tags)}`" if entry.tags else ""
        msg = await self._reply(
            chat_id,
            f"📸 Saved reference *{entry.id}*.{tag_hint}\n"
            "Running design analysis…",
        )
        # Vision extraction
        ok = False
        try:
            ok = await photos.run_vision_extraction(entry.id)
        except Exception:
            logger.exception("vision extraction failed")
        # Re-read so we get the updated verdict
        refreshed = photos.get_reference(entry.id)
        if ok and refreshed and refreshed.verdict_one_liner:
            await self._reply(
                chat_id,
                f"📐 *{entry.id}* analyzed:\n> {refreshed.verdict_one_liner}\n\n"
                f"Tags: `{', '.join(refreshed.tags)}`\n"
                "It will auto-attach to your next `/build` within 5 min, "
                "or match by tag overlap on later builds.",
            )
        else:
            await self._reply(
                chat_id,
                f"⚠️ Saved *{entry.id}* but vision analysis failed. "
                "It'll still attach to your next build; the designer just "
                "won't have an LLM-extracted palette to lean on.",
            )

        # If the caption was a build command, kick off the project now.
        # The photo just saved is within the 5-min auto-attach window,
        # so it'll attach to this new project automatically.
        if is_build_caption and caption_intent is not None:
            brief = caption_intent.brief or ""
            await self._do_start(chat_id, brief, caption_intent.slug)

    async def _handle_document(
        self, chat_id: int, sender: dict, document: dict, caption: str,
    ) -> None:
        """Ingest a document (PDF or image) as a design reference.

        Same flow as ``_handle_photo`` but consumes the Telegram
        ``document`` payload instead of ``photo``. Useful for brand
        guidelines, portfolio PDFs, and high-res images sent as files
        (which Telegram preserves un-compressed)."""
        caption_intent = parse_intent(caption) if caption else None
        is_build_caption = (
            caption_intent is not None
            and caption_intent.action == "start"
            and (caption_intent.brief or "").strip()
        )
        ingest_caption = "" if is_build_caption else caption

        result = await photos.ingest_telegram_document(
            self.token, self.allowed_user_id, document, ingest_caption,
        )
        if not result.ok or result.entry is None:
            await self._reply(
                chat_id,
                f"Couldn't save that file: {result.error or 'unknown error'}",
            )
            return
        entry = result.entry
        file_name = str(document.get("file_name") or "")
        tag_hint = f" Tags: `{', '.join(entry.tags)}`" if entry.tags else ""
        kind = "PDF" if (entry.path or "").lower().endswith(".pdf") else "file"
        await self._reply(
            chat_id,
            f"📎 Saved {kind} *{entry.id}* ({file_name}).{tag_hint}\n"
            "Running design analysis…",
        )
        ok = False
        try:
            ok = await photos.run_vision_extraction(entry.id)
        except Exception:
            logger.exception("vision extraction failed")
        refreshed = photos.get_reference(entry.id)
        if ok and refreshed and refreshed.verdict_one_liner:
            await self._reply(
                chat_id,
                f"📐 *{entry.id}* analyzed:\n> {refreshed.verdict_one_liner}\n\n"
                f"Tags: `{', '.join(refreshed.tags)}`\n"
                "It'll auto-attach to your next `/build` within 5 min.",
            )
        else:
            await self._reply(
                chat_id,
                f"⚠️ Saved *{entry.id}* but vision analysis failed. "
                "It'll still attach to your next build; the designer just "
                "won't have an LLM-extracted palette to lean on.",
            )
        if is_build_caption and caption_intent is not None:
            brief = caption_intent.brief or ""
            await self._do_start(chat_id, brief, caption_intent.slug)

    async def _do_references(self, chat_id: int) -> None:
        entries = photos.list_references(self.allowed_user_id)
        if not entries:
            await self._reply(
                chat_id,
                "No design references yet. Send me a photo (mood board, "
                "logo, screenshot of a UI you like) and I'll save it.",
            )
            return
        lines = ["*Design references:*"]
        for e in entries[:10]:
            tag_str = f" — `{', '.join(e.tags[:5])}`" if e.tags else ""
            verdict = f"\n  > {e.verdict_one_liner}" if e.verdict_one_liner else ""
            lines.append(f"• *{e.id}*{tag_str}{verdict}")
        if len(entries) > 10:
            lines.append(f"\n…and {len(entries) - 10} more.")
        lines.append(
            "\nTag with `/tag <id> word1 word2`. Untag with `/untag <id> word`."
        )
        await self._reply(chat_id, "\n".join(lines))

    async def _do_tag(self, chat_id: int, rest: str, remove: bool) -> None:
        tokens = rest.split()
        if len(tokens) < 2:
            verb = "untag" if remove else "tag"
            await self._reply(chat_id, f"Usage: `/{verb} <ref_id> <word> [word2…]`")
            return
        ref_id = tokens[0]
        tag_words = tokens[1:]
        kwargs = {"remove": tag_words} if remove else {"add": tag_words}
        updated = photos.update_tags(ref_id, **kwargs)
        if updated is None:
            await self._reply(chat_id, f"Reference `{ref_id}` not found.")
            return
        await self._reply(
            chat_id,
            f"*{ref_id}* tags: `{', '.join(updated.tags) or '(none)'}`",
        )
