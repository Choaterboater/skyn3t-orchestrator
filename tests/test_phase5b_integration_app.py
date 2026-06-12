"""Phase 5B integration endpoints (web/app.py).

Exercises the new opt-in status + trigger endpoints the INTEGRATOR wired:
/api/backends, /api/channels, /api/cron (GET/POST), /api/gateway/channels,
/api/gateway/deliver, /api/browser/status.

The whole point of Phase 5B is graceful degradation: with no credentials and
no optional SDKs installed, every endpoint must return a benign, structured
payload (never a 500). These tests run against tmp/fake state — no network,
no orchestrator run, no real backend SDKs.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import skyn3t.web.app as web_app


@pytest.fixture(autouse=True)
def _loopback_no_token(monkeypatch):
    """Force the loopback fallback (no web_token) so TestClient — which
    connects from 127.0.0.1 — can reach every route without a session
    cookie, mirroring tests/test_web_hardening.py."""
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True))
    yield


@pytest.fixture
def client():
    return TestClient(web_app.app)


# ── /api/backends ─────────────────────────────────────────────────────────
def test_backends_status_reports_remote_backends_removed(client):
    resp = client.get("/api/backends")
    assert resp.status_code == 200
    body = resp.json()
    # C11: the standalone remote execution adapters were production-dead,
    # so they were removed. The endpoint now returns an empty list.
    assert body.get("backends") == []
    assert body.get("available") == []
    assert "removed" in body.get("note", "")


# ── /api/channels ─────────────────────────────────────────────────────────
def test_channels_status_registers_optional_channels_none_available(client):
    resp = client.get("/api/channels")
    assert resp.status_code == 200
    body = resp.json()
    registered = body.get("registered")
    available = body.get("available")
    assert isinstance(registered, list)
    assert isinstance(available, list)
    # Optional channels get registered even with no creds (they read env in
    # __init__) but stay out of `available` because is_available() is False.
    for plat in ("dingtalk", "wecom", "wechat", "line", "kakaotalk", "sms", "homeassistant"):
        assert plat in registered
        assert plat not in available


# ── /api/cron ─────────────────────────────────────────────────────────────
def test_cron_get_returns_503_without_scheduler(client, monkeypatch):
    monkeypatch.setattr(web_app, "orchestrator", None)
    resp = client.get("/api/cron")
    assert resp.status_code == 503


def test_cron_post_requires_text(client):
    resp = client.post("/api/cron", json={})
    # No scheduler => 503; scheduler present => 400 for missing text. Either
    # way it must not 500 and must not create anything.
    assert resp.status_code in (400, 503)


def test_cron_get_and_post_with_fake_scheduler(client, monkeypatch):
    """A fake SchedulerAgent (agent_type='scheduler') is resolved from the
    orchestrator registry and driven through the list_jobs / schedule_nl
    tasks."""
    from skyn3t.core.agent import TaskResult

    captured = {}

    class FakeScheduler:
        agent_type = "scheduler"
        name = "scheduler"

        async def execute(self, task, stdin_data=None):
            captured["input"] = dict(task.input_data)
            tt = task.input_data.get("task_type")
            if tt == "list_jobs":
                return TaskResult(task_id="t", success=True, output={"jobs": [], "count": 0})
            if tt == "schedule_nl":
                return TaskResult(
                    task_id="t", success=True, output={"job_id": "job-1", "success": True}
                )
            return TaskResult(task_id="t", success=False, error="unknown")

    fake_orch = SimpleNamespace(agents={"scheduler": FakeScheduler()})
    monkeypatch.setattr(web_app, "orchestrator", fake_orch)

    # GET list
    resp = client.get("/api/cron")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert captured["input"]["task_type"] == "list_jobs"

    # POST create
    resp = client.post(
        "/api/cron",
        json={
            "text": "every weekday at 9am",
            "agent_name": "researcher",
            "prompt": "morning briefing",
            "delivery": {"channel": "telegram", "to": "123"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert captured["input"]["task_type"] == "schedule_nl"
    assert captured["input"]["text"] == "every weekday at 9am"
    assert captured["input"]["payload"] == {"agent_name": "researcher", "prompt": "morning briefing"}
    assert captured["input"]["delivery"] == {"channel": "telegram", "to": "123"}


# ── /api/gateway/channels + /api/gateway/deliver ──────────────────────────
def test_gateway_channels_status(client):
    resp = client.get("/api/gateway/channels")
    assert resp.status_code == 200
    assert isinstance(resp.json().get("channels"), list)


def test_gateway_deliver_unconfigured_channel_is_skipped(client):
    resp = client.post(
        "/api/gateway/deliver",
        json={"channel": "telegram", "to": "u1", "text": "hello"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Unconfigured/unregistered channel => skipped, not an error.
    assert body["skipped"] is True
    assert body["ok"] is False
    assert body.get("error") is None


def test_gateway_deliver_validates_required_fields(client):
    assert client.post("/api/gateway/deliver", json={"to": "x", "text": "y"}).status_code == 400
    assert client.post("/api/gateway/deliver", json={"channel": "telegram", "to": "x"}).status_code == 400


# ── /api/browser/status ───────────────────────────────────────────────────
def test_browser_status_reports_readiness_without_launching(client):
    resp = client.get("/api/browser/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "available" in body
    assert "backend" in body
    assert isinstance(body["available"], bool)
    # Playwright is not installed and no cloud key => no backend.
    assert body["available"] is False
    assert body["backend"] is None
