"""Tests for the Discord control-surface dispatcher.

Mocks the studio runner and httpx so no network or real Discord token
is needed. Covers slash commands, DM intent dispatch, button-press
flows, signature verification, and the in-process rate limiter.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from skyn3t.integrations import discord_commands as dc

# ---------------------------------------------------------------------------
# Fake runner
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
        self.started.append({"slug": slug, "brief": brief, "template": template, "extra": extra or {}})

    def get_project(self, slug):
        for p in self.projects:
            if p["slug"] == slug:
                return p
        return None

    def list_projects(self):
        return list(self.projects)

    async def resume_after_approval(self, slug, decision, edited_md=None, feedback=None):
        self.resumed.append({"slug": slug, "decision": decision, "edited_md": edited_md, "feedback": feedback})
        proj = self.get_project(slug)
        if proj is not None:
            proj["status"] = "running" if decision == "approve" else "rejected"
        return proj or {}


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    dc._user_calls.clear()
    yield
    dc._user_calls.clear()


# ---------------------------------------------------------------------------
# Slash command tests
# ---------------------------------------------------------------------------


def _slash_payload(name: str, options: List[Dict[str, Any]] | None = None, user_id: str = "u1") -> dict:
    return {
        "type": dc.INTERACTION_APPLICATION_COMMAND,
        "data": {"name": name, "options": options or []},
        "member": {"user": {"id": user_id, "username": "tester"}},
        "application_id": "app-1",
        "token": "interaction-token",
    }


@pytest.mark.asyncio
async def test_slash_start_kicks_off_with_brief_and_auto_slug():
    runner = FakeRunner()
    payload = _slash_payload("skyn3t-start", [{"name": "brief", "value": "build a homelab dashboard"}])

    result = await dc.handle_interaction(payload, runner)

    # Deferred response so Discord shows a spinner
    assert result.response["type"] == dc.RESPONSE_DEFERRED_CHANNEL_MESSAGE
    assert result.follow_up is not None
    # The follow-up actually does the work; run it manually
    await result.follow_up()
    assert len(runner.reserved) == 1
    assert runner.reserved[0]["brief"] == "build a homelab dashboard"
    assert len(runner.started) == 1


@pytest.mark.asyncio
async def test_slash_start_honors_explicit_slug():
    runner = FakeRunner()
    payload = _slash_payload("skyn3t-start", [
        {"name": "brief", "value": "todo app"},
        {"name": "slug", "value": "my-todo"},
    ])
    result = await dc.handle_interaction(payload, runner)
    await result.follow_up()
    assert runner.reserved[0]["slug"] == "my-todo"


@pytest.mark.asyncio
async def test_slash_start_missing_brief_returns_ephemeral_error():
    runner = FakeRunner()
    payload = _slash_payload("skyn3t-start", [])
    result = await dc.handle_interaction(payload, runner)
    assert result.response["type"] == dc.RESPONSE_CHANNEL_MESSAGE_WITH_SOURCE
    assert "Brief is required" in result.response["data"]["content"]


@pytest.mark.asyncio
async def test_slash_status_returns_formatted_summary():
    runner = FakeRunner()
    runner.projects.append({
        "slug": "canary-150",
        "status": "awaiting_approval",
        "current_stage": "architect",
        "review_score": 72,
    })
    payload = _slash_payload("skyn3t-status", [{"name": "slug", "value": "canary-150"}])
    result = await dc.handle_interaction(payload, runner)
    content = result.response["data"]["content"]
    assert "canary-150" in content
    assert "awaiting_approval" in content
    assert "72" in content


@pytest.mark.asyncio
async def test_slash_status_unknown_slug():
    runner = FakeRunner()
    payload = _slash_payload("skyn3t-status", [{"name": "slug", "value": "missing"}])
    result = await dc.handle_interaction(payload, runner)
    assert "not found" in result.response["data"]["content"]


@pytest.mark.asyncio
async def test_slash_approve_resumes_pipeline():
    runner = FakeRunner()
    runner.projects.append({"slug": "canary-1", "status": "awaiting_approval"})
    payload = _slash_payload("skyn3t-approve", [{"name": "slug", "value": "canary-1"}])
    result = await dc.handle_interaction(payload, runner)
    await result.follow_up()
    assert runner.resumed == [{"slug": "canary-1", "decision": "approve", "edited_md": None, "feedback": None}]


@pytest.mark.asyncio
async def test_slash_reject_resumes_with_feedback():
    runner = FakeRunner()
    runner.projects.append({"slug": "canary-1", "status": "awaiting_approval"})
    payload = _slash_payload("skyn3t-reject", [
        {"name": "slug", "value": "canary-1"},
        {"name": "feedback", "value": "palette is wrong"},
    ])
    result = await dc.handle_interaction(payload, runner)
    await result.follow_up()
    assert runner.resumed[0]["decision"] == "reject"
    assert runner.resumed[0]["feedback"] == "palette is wrong"


@pytest.mark.asyncio
async def test_slash_list_shows_projects():
    runner = FakeRunner()
    runner.projects.extend([
        {"slug": "p1", "status": "running", "created_at": 100.0},
        {"slug": "p2", "status": "completed", "created_at": 200.0},
    ])
    payload = _slash_payload("skyn3t-list", [])
    result = await dc.handle_interaction(payload, runner)
    content = result.response["data"]["content"]
    assert "p1" in content and "p2" in content
    # most-recent first
    assert content.index("p2") < content.index("p1")


# ---------------------------------------------------------------------------
# Button-press tests
# ---------------------------------------------------------------------------


def _component_payload(custom_id: str, user_id: str = "u1") -> dict:
    return {
        "type": dc.INTERACTION_MESSAGE_COMPONENT,
        "data": {"custom_id": custom_id, "component_type": 2},
        "member": {"user": {"id": user_id, "username": "tester", "global_name": "Tester"}},
        "application_id": "app-1",
        "token": "interaction-token",
        "message": {"id": "m1"},
    }


@pytest.mark.asyncio
async def test_button_approve_triggers_resume():
    runner = FakeRunner()
    runner.projects.append({"slug": "canary-1", "status": "awaiting_approval"})
    payload = _component_payload("approve:canary-1")

    result = await dc.handle_interaction(payload, runner)
    assert result.response["type"] == dc.RESPONSE_DEFERRED_UPDATE_MESSAGE
    assert result.follow_up is not None
    # Stub out the network edit so the follow-up doesn't actually call Discord
    async def fake_edit(p, c): pass
    dc._edit_original_response = fake_edit  # type: ignore[assignment]
    await result.follow_up()
    assert runner.resumed == [{"slug": "canary-1", "decision": "approve", "edited_md": None, "feedback": None}]


@pytest.mark.asyncio
async def test_button_reject_opens_modal():
    runner = FakeRunner()
    payload = _component_payload("reject:canary-1")
    result = await dc.handle_interaction(payload, runner)
    assert result.response["type"] == dc.RESPONSE_MODAL
    assert result.response["data"]["custom_id"] == "reject_modal:canary-1"
    # No follow-up — modal submit is a separate interaction
    assert result.follow_up is None


@pytest.mark.asyncio
async def test_modal_submit_resumes_with_feedback():
    runner = FakeRunner()
    runner.projects.append({"slug": "canary-1", "status": "awaiting_approval"})
    payload = {
        "type": dc.INTERACTION_MODAL_SUBMIT,
        "data": {
            "custom_id": "reject_modal:canary-1",
            "components": [{
                "type": 1,
                "components": [{"type": 4, "custom_id": "feedback", "value": "stack mismatch"}],
            }],
        },
        "member": {"user": {"id": "u1"}},
        "application_id": "app-1",
        "token": "tok",
    }
    result = await dc.handle_interaction(payload, runner)
    await result.follow_up()
    assert runner.resumed[0]["decision"] == "reject"
    assert runner.resumed[0]["feedback"] == "stack mismatch"


@pytest.mark.asyncio
async def test_malformed_button_id():
    runner = FakeRunner()
    payload = _component_payload("approve")  # missing colon
    result = await dc.handle_interaction(payload, runner)
    assert "Malformed" in result.response["data"]["content"]


# ---------------------------------------------------------------------------
# DM intent tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_start_intent_kicks_off():
    runner = FakeRunner()
    reply = await dc.handle_dm("build a homelab dashboard", "user-99", runner)
    # Yield once so the asyncio.create_task in _kickoff_project gets scheduled
    await asyncio.sleep(0)
    assert "Started" in reply
    assert len(runner.reserved) == 1
    assert "homelab" in runner.reserved[0]["brief"]


@pytest.mark.asyncio
async def test_dm_unknown_returns_help():
    runner = FakeRunner()
    reply = await dc.handle_dm("hi", "user-99", runner)
    assert "SkyN3t" in reply or "commands" in reply.lower()


@pytest.mark.asyncio
async def test_dm_approve_resolves_most_recent_awaiting():
    runner = FakeRunner()
    runner.projects.append({"slug": "old", "status": "running", "created_at": 50.0})
    runner.projects.append({"slug": "awaiting-1", "status": "awaiting_approval", "created_at": 100.0})
    reply = await dc.handle_dm("approve", "user-99", runner)
    assert "Approved" in reply
    assert "awaiting-1" in reply
    assert runner.resumed[0]["slug"] == "awaiting-1"


@pytest.mark.asyncio
async def test_dm_status_with_no_projects():
    runner = FakeRunner()
    reply = await dc.handle_dm("status", "user-99", runner)
    assert "No projects" in reply


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def test_verify_signature_with_empty_inputs():
    assert dc.verify_signature("", "sig", "ts", b"body") is False
    assert dc.verify_signature("aabb", "", "ts", b"body") is False
    assert dc.verify_signature("aabb", "sig", "", b"body") is False


def test_verify_signature_invalid_hex():
    assert dc.verify_signature("zzzz", "zzzz", "ts", b"body") is False


def test_verify_signature_valid_roundtrip():
    """Generate a fresh keypair, sign a body, verify it round-trips."""
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    public_hex = sk.verify_key.encode().hex()
    timestamp = "1700000000"
    body = b'{"type":1}'
    signed = sk.sign(timestamp.encode("utf-8") + body)
    signature_hex = signed.signature.hex()
    assert dc.verify_signature(public_hex, signature_hex, timestamp, body) is True


def test_verify_signature_wrong_body():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    public_hex = sk.verify_key.encode().hex()
    timestamp = "1700000000"
    signed = sk.sign(timestamp.encode("utf-8") + b'{"type":1}')
    # Same signature but different body
    assert dc.verify_signature(public_hex, signed.signature.hex(), timestamp, b'{"type":2}') is False


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_blocks_after_threshold():
    runner = FakeRunner()
    runner.projects.append({"slug": "canary-1", "status": "running"})

    # 5 approves should pass, 6th should be rate-limited.
    for i in range(dc._RATE_LIMIT_MAX):
        payload = _slash_payload("skyn3t-status", [{"name": "slug", "value": "canary-1"}], user_id="burst")
        result = await dc.handle_interaction(payload, runner)
        assert "Slow down" not in result.response["data"]["content"]

    payload = _slash_payload("skyn3t-status", [{"name": "slug", "value": "canary-1"}], user_id="burst")
    result = await dc.handle_interaction(payload, runner)
    assert "Slow down" in result.response["data"]["content"]


@pytest.mark.asyncio
async def test_rate_limit_isolated_per_user():
    runner = FakeRunner()
    runner.projects.append({"slug": "canary-1", "status": "running"})
    # User A exhausts limit
    for _ in range(dc._RATE_LIMIT_MAX):
        await dc.handle_interaction(
            _slash_payload("skyn3t-status", [{"name": "slug", "value": "canary-1"}], user_id="A"), runner,
        )
    # User B should still pass
    result = await dc.handle_interaction(
        _slash_payload("skyn3t-status", [{"name": "slug", "value": "canary-1"}], user_id="B"), runner,
    )
    assert "Slow down" not in result.response["data"]["content"]


# ---------------------------------------------------------------------------
# Ping handling (Discord initial endpoint health check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_returns_pong():
    runner = FakeRunner()
    payload = {"type": dc.INTERACTION_PING}
    result = await dc.handle_interaction(payload, runner)
    assert result.response == {"type": dc.RESPONSE_PONG}
