"""Tests for the Telegram studio control surface bot.

Mocks httpx and the studio runner — no real Telegram traffic, no real
project work. Covers command dispatch, callback (button) handling,
user-id gating, and the reject-feedback follow-up flow.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from skyn3t.integrations import telegram_bot as tg
from skyn3t.integrations import telegram_dispatch as tgd
from skyn3t.config.settings import get_settings

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRunner:
    def __init__(self):
        self.projects: List[dict] = []
        self.reserved: List[dict] = []
        self.started: List[dict] = []
        self.resumed: List[dict] = []

    def reserve_project(self, template, brief, slug=None, **kwargs):
        new_slug = slug or f"canary-{len(self.projects) + 100}"
        manifest = {"slug": new_slug, "status": "running", "brief": brief, "template": template}
        self.projects.append(manifest)
        self.reserved.append(manifest)
        return manifest

    async def start(self, template, brief, slug=None, extra=None, **kwargs):
        self.started.append({"slug": slug, "brief": brief, "extra": extra or {}})

    def get_project(self, slug):
        for p in self.projects:
            if p["slug"] == slug:
                return p
        return None

    def list_projects(self):
        return list(self.projects)

    async def resume_after_approval(self, slug, decision, edited_md=None, feedback=None):
        self.resumed.append({"slug": slug, "decision": decision, "feedback": feedback})
        proj = self.get_project(slug)
        if proj is not None:
            proj["status"] = "running" if decision == "approve" else "rejected"
        return proj or {}


class _SendCapture:
    """Captures dispatch.send_message and answer_callback calls so we can
    assert without needing real HTTP."""
    def __init__(self):
        self.calls: List[dict] = []
        self.ack_calls: List[dict] = []

    async def __call__(self, token, chat_id, text, reply_to_message_id=None, reply_markup=None, parse_mode="Markdown"):
        self.calls.append({
            "token": token,
            "chat_id": str(chat_id),
            "text": text,
            "reply_to_message_id": reply_to_message_id,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        })
        return {"message_id": 1}


@pytest.fixture
def captured_send(monkeypatch):
    cap = _SendCapture()
    monkeypatch.setattr(tg.dispatch, "send_message", cap)

    async def fake_ack(token, cb_id, text=""):
        cap.ack_calls.append({"token": token, "cb_id": cb_id, "text": text})
    monkeypatch.setattr(tg.dispatch, "answer_callback", fake_ack)
    return cap


def _bot(runner=None):
    return tg.TelegramBot(token="t", allowed_user_id="42", studio_runner=runner)


# ---------------------------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stranger_dms_are_ignored(captured_send):
    bot = _bot(FakeRunner())
    await bot._handle_message({
        "from": {"id": "999"},  # not the allowed_user_id
        "chat": {"id": 999},
        "text": "build a todo app",
    })
    assert captured_send.calls == []


@pytest.mark.asyncio
async def test_stranger_callbacks_are_ignored(captured_send):
    bot = _bot(FakeRunner())
    await bot._handle_callback({
        "from": {"id": "999"},
        "id": "cb-1",
        "data": "approve:canary-1",
        "message": {"chat": {"id": 999}, "message_id": 1},
    })
    assert captured_send.calls == []


# ---------------------------------------------------------------------------
# Free-text intent tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_text_replied(captured_send):
    bot = _bot(FakeRunner())
    await bot._handle_message({
        "from": {"id": "42"}, "chat": {"id": 42}, "text": "help",
    })
    assert any("SkyN3t commands" in c["text"] for c in captured_send.calls)


@pytest.mark.asyncio
async def test_build_intent_kicks_off_project(captured_send):
    runner = FakeRunner()
    bot = _bot(runner)
    await bot._handle_message({
        "from": {"id": "42"},
        "chat": {"id": 42},
        "message_id": 123,
        "text": "build a homelab dashboard",
    })
    await asyncio.sleep(0)  # let the create_task fire
    assert len(runner.reserved) == 1
    assert runner.started[0]["extra"]["telegram"] == {
        "chat_id": "42",
        "starter_message_id": 123,
    }
    assert captured_send.calls[-1]["reply_to_message_id"] == 123
    assert "stage updates" in captured_send.calls[-1]["text"]


@pytest.mark.asyncio
async def test_list_intent(captured_send):
    runner = FakeRunner()
    runner.projects.extend([
        {"slug": "p1", "status": "running", "created_at": 100.0},
        {"slug": "p2", "status": "completed", "created_at": 200.0},
    ])
    bot = _bot(runner)
    await bot._handle_message({
        "from": {"id": "42"}, "chat": {"id": 42}, "text": "list",
    })
    reply = captured_send.calls[-1]["text"]
    assert "p1" in reply and "p2" in reply


@pytest.mark.asyncio
async def test_status_intent_uses_latest_when_no_slug(captured_send):
    runner = FakeRunner()
    runner.projects.append({"slug": "latest-1", "status": "running", "created_at": 100.0})
    bot = _bot(runner)
    await bot._handle_message({
        "from": {"id": "42"}, "chat": {"id": 42}, "text": "status",
    })
    reply = captured_send.calls[-1]["text"]
    assert "latest-1" in reply


@pytest.mark.asyncio
async def test_approve_no_slug_finds_most_recent_awaiting(captured_send):
    runner = FakeRunner()
    runner.projects.extend([
        {"slug": "old", "status": "running", "created_at": 50.0},
        {"slug": "awaiting-1", "status": "awaiting_approval", "created_at": 100.0},
    ])
    bot = _bot(runner)
    await bot._handle_message({
        "from": {"id": "42"}, "chat": {"id": 42}, "text": "approve",
    })
    assert runner.resumed and runner.resumed[0]["slug"] == "awaiting-1"


@pytest.mark.asyncio
async def test_approve_when_nothing_pending(captured_send):
    runner = FakeRunner()
    bot = _bot(runner)
    await bot._handle_message({
        "from": {"id": "42"}, "chat": {"id": 42}, "text": "approve",
    })
    assert any("No project is awaiting" in c["text"] for c in captured_send.calls)
    assert runner.resumed == []


# ---------------------------------------------------------------------------
# Slash command tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slash_help(captured_send):
    bot = _bot(FakeRunner())
    await bot._handle_message({
        "from": {"id": "42"}, "chat": {"id": 42}, "text": "/help",
    })
    assert any("SkyN3t commands" in c["text"] for c in captured_send.calls)


@pytest.mark.asyncio
async def test_slash_build_requires_brief(captured_send):
    bot = _bot(FakeRunner())
    await bot._handle_message({
        "from": {"id": "42"}, "chat": {"id": 42}, "text": "/build",
    })
    assert any("Usage:" in c["text"] for c in captured_send.calls)


@pytest.mark.asyncio
async def test_slash_build_with_brief(captured_send):
    runner = FakeRunner()
    bot = _bot(runner)
    await bot._handle_message({
        "from": {"id": "42"},
        "chat": {"id": 42},
        "message_id": 456,
        "text": "/build a finance tracker",
    })
    await asyncio.sleep(0)
    assert runner.reserved and "finance" in runner.reserved[0]["brief"]
    assert runner.started[0]["extra"]["telegram"]["starter_message_id"] == 456


@pytest.mark.asyncio
async def test_slash_with_botname_suffix_stripped(captured_send):
    runner = FakeRunner()
    bot = _bot(runner)
    await bot._handle_message({
        "from": {"id": "42"}, "chat": {"id": 42}, "text": "/help@skyn3t_bot",
    })
    assert any("SkyN3t commands" in c["text"] for c in captured_send.calls)


# ---------------------------------------------------------------------------
# Inline button callback tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_button_resumes(captured_send):
    runner = FakeRunner()
    runner.projects.append({"slug": "canary-1", "status": "awaiting_approval"})
    bot = _bot(runner)
    await bot._handle_callback({
        "from": {"id": "42"}, "id": "cb-1", "data": "approve:canary-1",
        "message": {"chat": {"id": 42}, "message_id": 1},
    })
    assert runner.resumed == [{"slug": "canary-1", "decision": "approve", "feedback": None}]
    assert any("Approved" in c["text"] for c in captured_send.calls)


@pytest.mark.asyncio
async def test_reject_button_prompts_for_feedback(captured_send):
    runner = FakeRunner()
    runner.projects.append({"slug": "canary-1", "status": "awaiting_approval"})
    bot = _bot(runner)
    await bot._handle_callback({
        "from": {"id": "42"}, "id": "cb-2", "data": "reject:canary-1",
        "message": {"chat": {"id": 42}, "message_id": 1},
    })
    # No resume yet — waiting for the feedback message
    assert runner.resumed == []
    assert "42" in bot._pending_reject_slugs
    assert any("feedback" in c["text"].lower() for c in captured_send.calls)


@pytest.mark.asyncio
async def test_reject_feedback_flow_completes(captured_send):
    runner = FakeRunner()
    runner.projects.append({"slug": "canary-1", "status": "awaiting_approval"})
    bot = _bot(runner)
    # 1. Tap Reject
    await bot._handle_callback({
        "from": {"id": "42"}, "id": "cb-2", "data": "reject:canary-1",
        "message": {"chat": {"id": 42}, "message_id": 1},
    })
    # 2. Send feedback message
    await bot._handle_message({
        "from": {"id": "42"}, "chat": {"id": 42}, "text": "the palette is wrong",
    })
    assert runner.resumed == [{"slug": "canary-1", "decision": "reject", "feedback": "the palette is wrong"}]


@pytest.mark.asyncio
async def test_status_button(captured_send):
    runner = FakeRunner()
    runner.projects.append({"slug": "canary-1", "status": "running"})
    bot = _bot(runner)
    await bot._handle_callback({
        "from": {"id": "42"}, "id": "cb-3", "data": "status:canary-1",
        "message": {"chat": {"id": 42}, "message_id": 1},
    })
    assert any("canary-1" in c["text"] for c in captured_send.calls)


@pytest.mark.asyncio
async def test_malformed_button_data(captured_send):
    runner = FakeRunner()
    bot = _bot(runner)
    await bot._handle_callback({
        "from": {"id": "42"}, "id": "cb-4", "data": "garbage",
        "message": {"chat": {"id": 42}, "message_id": 1},
    })
    # Nothing should have been replied; just an answer_callback for the spinner
    assert captured_send.calls == []
    assert len(captured_send.ack_calls) == 1


# ---------------------------------------------------------------------------
# Dispatch (outbound) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_skips_when_unconfigured(monkeypatch):
    # Make settings return no token
    s = get_settings()
    monkeypatch.setattr(s, "telegram_token", None)
    monkeypatch.setattr(s, "telegram_user_id", None)
    result = await tgd.dispatch_approval("canary-1", "ArchitectAgent", "")
    assert result == {"ok": False, "message_id": None, "chat_id": ""}


@pytest.mark.asyncio
async def test_dispatch_posts_when_configured(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "telegram_token", "test-token")
    monkeypatch.setattr(s, "telegram_user_id", "42")
    # Clear throttle so this fires
    tgd._last_dispatch.clear()

    async def fake_send(token, chat_id, text, reply_to_message_id=None, reply_markup=None, parse_mode="Markdown"):
        return {"message_id": 555}
    monkeypatch.setattr(tgd, "send_message", fake_send)

    result = await tgd.dispatch_approval("canary-test", "ArchitectAgent", "http://x")
    assert result == {"ok": True, "message_id": 555, "chat_id": "42"}


@pytest.mark.asyncio
async def test_dispatch_throttles_duplicates(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "telegram_token", "test-token")
    monkeypatch.setattr(s, "telegram_user_id", "42")
    tgd._last_dispatch.clear()

    async def fake_send(*args, **kwargs):
        return {"message_id": 1}
    monkeypatch.setattr(tgd, "send_message", fake_send)

    first = await tgd.dispatch_approval("canary-throttle", "ArchitectAgent", "")
    second = await tgd.dispatch_approval("canary-throttle", "ArchitectAgent", "")
    assert first["ok"] is True
    assert second.get("throttled") is True
