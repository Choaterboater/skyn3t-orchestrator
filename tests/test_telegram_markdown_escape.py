"""Tests for Telegram legacy-Markdown escaping.

Telegram's ``parse_mode="Markdown"`` returns 400 on unbalanced or
unescaped ``*`` / ``_`` / `` ` `` / ``[`` characters in user content.
The dispatcher retries with parse_mode stripped, so the message
arrives but loses ALL formatting — bold headers, code blocks, the
section dividers in approval messages all turn into plain text.

These tests pin the contract:
1. ``escape_markdown`` is a backslash-escape over the four legacy
   special chars + the backslash itself (escape-the-escape).
2. ``_format_approval_message`` applies it to slug / summary /
   plain_english — the three user-supplied fields.
3. ``_format_status`` / ``_format_project_list`` apply it to
   slug / status / stage / verdict.
"""

from __future__ import annotations

from skyn3t.integrations.telegram_bot import (
    _format_project_list,
    _format_status,
)
from skyn3t.integrations.telegram_dispatch import (
    _format_approval_message,
    escape_markdown,
)


class TestEscapeMarkdown:
    def test_passes_plain_text_unchanged(self):
        assert escape_markdown("hello world") == "hello world"

    def test_escapes_asterisk(self):
        assert escape_markdown("a*b") == "a\\*b"

    def test_escapes_underscore(self):
        assert escape_markdown("file_name") == "file\\_name"

    def test_escapes_backtick(self):
        assert escape_markdown("use `code`") == "use \\`code\\`"

    def test_escapes_open_bracket(self):
        assert escape_markdown("link [text]") == "link \\[text]"

    def test_escapes_backslash_first(self):
        # The backslash itself must be escaped, and it must happen
        # before the other replacements so we don't double-escape
        # our own escapes.
        assert escape_markdown("a\\*b") == "a\\\\\\*b"

    def test_escapes_multiple_in_one_string(self):
        assert escape_markdown("*bold* and _italic_") == (
            "\\*bold\\* and \\_italic\\_"
        )

    def test_empty_input(self):
        assert escape_markdown("") == ""

    def test_none_passes_through(self):
        # Helper is used on `.get(...) or ""` chains, so a falsy value
        # must round-trip without raising.
        assert escape_markdown(None) is None


class TestFormatApprovalMessageEscapes:
    def test_unsafe_slug_does_not_break_template(self):
        body = _format_approval_message(
            slug="my_*weird*_slug",
            agent_name="architect",
            summary="",
            dashboard_url="",
            plain_english="",
        )
        # The literal characters are escaped (backslash + char), and
        # the surrounding `*...*` bold template stays intact.
        assert "🔍 *my\\_\\*weird\\*\\_slug* needs review" in body

    def test_unsafe_summary_is_escaped(self):
        body = _format_approval_message(
            slug="build-todo",
            agent_name="architect",
            summary="Use *bold* and `code`",
            dashboard_url="",
            plain_english="",
        )
        assert "\\*bold\\*" in body
        assert "\\`code\\`" in body

    def test_unsafe_plain_english_is_escaped(self):
        body = _format_approval_message(
            slug="build-todo",
            agent_name="architect",
            summary="",
            dashboard_url="",
            plain_english="Will use file_loader and [config] dir.",
        )
        assert "file\\_loader" in body
        assert "\\[config]" in body

    def test_safe_slug_passes_through_unchanged(self):
        body = _format_approval_message(
            slug="build-a-habit-tracker-120f40",
            agent_name="architect",
            summary="ship it",
            dashboard_url="",
            plain_english="",
        )
        # No special chars → no backslashes in the slug field.
        assert "🔍 *build-a-habit-tracker-120f40* needs review" in body


class TestFormatStatusAndProjectListEscape:
    def test_status_escapes_slug_with_special_chars(self):
        body = _format_status({
            "slug": "build_my_*app*",
            "status": "running",
            "current_stage": "code",
        })
        assert "*build\\_my\\_\\*app\\** — `running`" in body
        assert "Stage: code" in body

    def test_status_escapes_stage_with_special_chars(self):
        body = _format_status({
            "slug": "ok-slug",
            "status": "running",
            "current_stage": "writing src/components/*.jsx",
        })
        assert "Stage: writing src/components/\\*.jsx" in body

    def test_status_escapes_verdict(self):
        body = _format_status({
            "slug": "ok-slug",
            "status": "completed",
            "quality_summary": {"score": 72, "verdict": "go_for_launch"},
        })
        assert "verdict: `go\\_for\\_launch`" in body

    def test_project_list_escapes_slugs(self):
        projects = [
            {"slug": "build_*risky*", "status": "running", "updated_at": 1.0},
            {"slug": "safe-slug", "status": "done", "updated_at": 0.5},
        ]
        body = _format_project_list(projects)
        assert "`build\\_\\*risky\\*` — running" in body
        assert "`safe-slug` — done" in body

    def test_empty_project_list_unchanged(self):
        # The empty-list message contains backticks as template
        # markup, not user input — must not be re-escaped.
        body = _format_project_list([])
        assert body == "No projects yet. Try: `build a todo app`."
