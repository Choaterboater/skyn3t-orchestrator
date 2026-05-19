"""Tests for the Discord intent parser."""

from skyn3t.integrations.discord_intent import Intent, parse


def test_parse_start_with_brief():
    out = parse("start a homelab dashboard")
    assert out.action == "start"
    assert out.brief and "homelab dashboard" in out.brief


def test_parse_start_implicit_build_verb():
    out = parse("build a todo app")
    assert out.action == "start"
    assert out.brief and "todo app" in out.brief


def test_parse_no_verb_freeform_is_unknown():
    """No verb → unknown. Don't auto-start projects from chitchat."""
    assert parse("an internal admin panel for users").action == "unknown"
    assert parse("never got the ping").action == "unknown"
    assert parse("this is broken").action == "unknown"


def test_parse_status_with_slug():
    out = parse("status canary-150")
    assert out.action == "status"
    assert out.slug == "canary-150"


def test_parse_status_no_slug():
    out = parse("status")
    assert out.action == "status"
    assert out.slug is None


def test_parse_approve_no_slug():
    out = parse("approve")
    assert out.action == "approve"
    assert out.slug is None


def test_parse_approve_with_slug():
    out = parse("approve canary-150")
    assert out.action == "approve"
    assert out.slug == "canary-150"


def test_parse_approve_lgtm_alias():
    out = parse("lgtm")
    assert out.action == "approve"


def test_parse_reject_with_feedback():
    out = parse("reject canary-150 the palette is wrong")
    assert out.action == "reject"
    assert out.slug == "canary-150"
    assert out.feedback and "palette is wrong" in out.feedback


def test_parse_reject_no_feedback():
    out = parse("reject canary-150")
    assert out.action == "reject"
    assert out.slug == "canary-150"
    assert out.feedback is None


def test_parse_list_command():
    assert parse("list").action == "list"
    assert parse("ls").action == "list"
    assert parse("show projects").action == "list"


def test_parse_unknown_short_string():
    assert parse("hi").action == "unknown"
    assert parse("").action == "unknown"


def test_parse_help():
    assert parse("help").action == "help"
    assert parse("?").action == "help"
    assert parse("commands").action == "help"


def test_parse_case_insensitive():
    out = parse("START a project to build a dashboard")
    assert out.action == "start"


def test_parse_strips_bot_mention():
    out = parse("<@123456> start a project")
    assert out.action == "start"


def test_parse_strips_bang_mention():
    out = parse("<@!123> approve canary-150")
    assert out.action == "approve"
    assert out.slug == "canary-150"


def test_parse_slug_stripped_from_brief():
    out = parse("start canary-200 a real estate listing tool")
    assert out.action == "start"
    assert out.brief and "real estate" in out.brief
    assert out.brief and "canary-200" not in out.brief


def test_intent_dataclass_defaults():
    i = Intent(action="start")
    assert i.slug is None
    assert i.brief is None
    assert i.feedback is None
