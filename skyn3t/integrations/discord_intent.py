"""Plain-English intent parser for Discord DMs / mentions.

Pure functions, regex-driven, no LLM call. ``parse(text)`` returns an
``Intent`` describing what the user asked for; the caller is responsible
for resolving the actual project (e.g. for ``approve`` with no slug,
caller picks the most-recent ``awaiting_approval`` project).

The parser is intentionally permissive: anything that doesn't match a
known verb but mentions "build a"/"make a"/"create a" is treated as a
project-start brief, so newcomers can type natural language without
learning syntax.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_SLUG_RE = re.compile(r"\b([a-z][a-z0-9-]{2,40}-\d{1,6}|[a-z][a-z0-9]{1,15}-[a-z0-9-]{2,40})\b", re.IGNORECASE)
_MENTION_RE = re.compile(r"<@!?(\d+)>")
_START_VERBS = ("start", "build", "make", "create", "spin up", "kick off", "launch", "scaffold", "generate")
_STATUS_VERBS = ("status", "check", "where", "how is", "how's", "progress")
_APPROVE_VERBS = ("approve", "approved", "ok", "okay", "lgtm", "ship", "ship it", "go", "proceed", "yes")
_REJECT_VERBS = ("reject", "rejected", "no", "deny", "stop", "scrap", "kill", "redo", "retry")
_LIST_VERBS = ("list", "ls", "show projects", "show me", "all projects", "what's running", "whats running")
_HELP_VERBS = ("help", "?", "commands", "what can you do")


@dataclass
class Intent:
    action: str  # start | status | approve | reject | list | help | unknown
    slug: Optional[str] = None
    brief: Optional[str] = None
    feedback: Optional[str] = None


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub("", text).strip()


def _find_slug(text: str) -> Optional[str]:
    m = _SLUG_RE.search(text)
    return m.group(0).lower() if m else None


def _starts_with_any(text: str, verbs: tuple) -> bool:
    lowered = text.lower().strip()
    return any(
        lowered == v or lowered.startswith(v + " ") or lowered.startswith(v + ":") for v in verbs
    )


def _contains_any(text: str, verbs: tuple) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(v)}\b", lowered) for v in verbs)


def parse(text: str) -> Intent:
    """Parse a Discord message into an Intent.

    The parser is order-sensitive: it checks the cheapest unambiguous
    verbs first (approve/reject/status/list/help) before falling through
    to ``start`` which has the broadest catch-all.
    """
    if not text or not text.strip():
        return Intent(action="unknown")

    cleaned = _strip_mentions(text).strip()
    if not cleaned:
        return Intent(action="unknown")

    lowered = cleaned.lower()
    slug = _find_slug(cleaned)

    # help / commands
    if lowered in _HELP_VERBS or _starts_with_any(cleaned, _HELP_VERBS):
        return Intent(action="help")

    # list
    if _starts_with_any(cleaned, _LIST_VERBS) or lowered in ("projects", "show projects"):
        return Intent(action="list")

    # approve / reject — both can have a slug + optional feedback
    if _starts_with_any(cleaned, _APPROVE_VERBS):
        return Intent(action="approve", slug=slug)

    if _starts_with_any(cleaned, _REJECT_VERBS):
        # everything after the verb (and slug, if present) is feedback
        feedback = _strip_after_verb(cleaned, _REJECT_VERBS, slug)
        return Intent(action="reject", slug=slug, feedback=feedback or None)

    # status
    if _starts_with_any(cleaned, _STATUS_VERBS):
        return Intent(action="status", slug=slug)

    # start — explicit verb. Preserve the verb in the brief because the
    # downstream planner uses verbs like "build" / "create" / "make" as
    # signal that this is a software-build task (not a docs-only one).
    # Previous behavior stripped the verb, causing the planner to fall
    # back to brainstorm-only pipelines for clearly-build briefs.
    if _starts_with_any(cleaned, _START_VERBS):
        # Only strip the slug if present; keep the verb intact.
        brief = cleaned
        if slug:
            import re as _re
            brief = _re.sub(rf"\b{_re.escape(slug)}\b", "", brief, count=1, flags=_re.IGNORECASE).strip()
        return Intent(action="start", brief=brief or cleaned, slug=slug)

    return Intent(action="unknown")


def _strip_after_verb(text: str, verbs: tuple, slug: Optional[str]) -> str:
    """Return the substring after the matched verb (and slug, if any),
    trimmed of common filler words. Used to extract briefs and feedback.
    """
    lowered = text.lower()
    cut = 0
    for v in verbs:
        if lowered.startswith(v + " ") or lowered.startswith(v + ":"):
            cut = len(v) + 1
            break
        if lowered == v:
            return ""
    remainder = text[cut:].strip(" :\n\t")
    if slug:
        # remove the first occurrence of the slug from the remainder
        remainder = re.sub(rf"\b{re.escape(slug)}\b", "", remainder, count=1, flags=re.IGNORECASE).strip()
    return remainder
