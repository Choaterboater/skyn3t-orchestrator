"""Phase 5B — BrowserAgent graceful-skip + backend selection.

These tests MUST NOT launch a real browser or hit the network. We assert:

* ``available()`` is non-raising and reflects the env/SDK gates.
* ``select_browser_backend()`` picks local-cdp > cloud > None.
* The agent degrades gracefully (success=False, skipped=True) with no
  backend, and routes to a fake backend when one is monkeypatched in.
"""

from __future__ import annotations

import asyncio

from skyn3t.agents import browser_agent as ba
from skyn3t.agents.browser_agent import (
    BrowserAgent,
    BrowserbaseBackend,
    BrowserSnapshot,
    BrowserStep,
    BrowserUseBackend,
    LocalCdpBackend,
    select_browser_backend,
)
from skyn3t.core.agent import TaskRequest


def _run(coro):
    return asyncio.run(coro)


# ── value objects ────────────────────────────────────────────────────────


def test_step_and_snapshot_defaults():
    step = BrowserStep(ok=True)
    assert step.detail == "" and step.error is None
    snap = BrowserSnapshot(url="http://x")
    assert snap.title == "" and snap.text == "" and snap.screenshot_path is None


# ── availability gates (non-raising) ─────────────────────────────────────


def test_available_never_raises(monkeypatch):
    # Even with no env / no SDK, available() returns a bool and never throws.
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    assert isinstance(LocalCdpBackend.available(), bool)
    assert BrowserbaseBackend.available() is False
    assert BrowserUseBackend.available() is False


def test_local_available_tracks_playwright_probe(monkeypatch):
    monkeypatch.setattr(ba, "_playwright_importable", lambda: True)
    monkeypatch.delenv("SKYN3T_BROWSER_DISABLE_LOCAL", raising=False)
    assert LocalCdpBackend.available() is True
    monkeypatch.setattr(ba, "_playwright_importable", lambda: False)
    assert LocalCdpBackend.available() is False


def test_local_available_opt_out(monkeypatch):
    monkeypatch.setattr(ba, "_playwright_importable", lambda: True)
    monkeypatch.setenv("SKYN3T_BROWSER_DISABLE_LOCAL", "1")
    assert LocalCdpBackend.available() is False


def test_playwright_probe_swallows_errors(monkeypatch):
    def boom(_name):
        raise RuntimeError("broken install")

    monkeypatch.setattr(ba.importlib.util, "find_spec", boom)
    # Probe must swallow the error and report not-importable.
    assert ba._playwright_importable() is False


def test_browserbase_requires_key_and_sdk(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "k")
    # Key present but SDK spec missing => unavailable.
    monkeypatch.setattr(
        ba.importlib.util,
        "find_spec",
        lambda name: None if name == "browserbase" else object(),
    )
    assert BrowserbaseBackend.available() is False


def test_browseruse_requires_key(monkeypatch):
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    assert BrowserUseBackend.available() is False
    monkeypatch.setenv("BROWSER_USE_API_KEY", "k")
    monkeypatch.setattr(
        ba.importlib.util,
        "find_spec",
        lambda name: object() if name == "browser_use" else None,
    )
    assert BrowserUseBackend.available() is True


# ── backend selection ────────────────────────────────────────────────────


def test_select_returns_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(LocalCdpBackend, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(BrowserbaseBackend, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(BrowserUseBackend, "available", classmethod(lambda cls: False))
    assert select_browser_backend() is None


def test_select_prefers_local(monkeypatch):
    monkeypatch.setattr(LocalCdpBackend, "available", classmethod(lambda cls: True))
    monkeypatch.setattr(BrowserbaseBackend, "available", classmethod(lambda cls: True))
    monkeypatch.setattr(BrowserUseBackend, "available", classmethod(lambda cls: True))
    backend = select_browser_backend()
    assert isinstance(backend, LocalCdpBackend)
    assert backend.name == "local-cdp"


def test_select_falls_back_to_cloud(monkeypatch):
    monkeypatch.setattr(LocalCdpBackend, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(BrowserbaseBackend, "available", classmethod(lambda cls: True))
    monkeypatch.setattr(BrowserUseBackend, "available", classmethod(lambda cls: True))
    backend = select_browser_backend()
    assert isinstance(backend, BrowserbaseBackend)


def test_select_skips_backend_whose_available_raises(monkeypatch):
    def boom(cls):
        raise RuntimeError("nope")

    monkeypatch.setattr(LocalCdpBackend, "available", classmethod(boom))
    monkeypatch.setattr(BrowserbaseBackend, "available", classmethod(lambda cls: True))
    backend = select_browser_backend()
    assert isinstance(backend, BrowserbaseBackend)


# ── graceful skip path (no real browser) ─────────────────────────────────


def test_agent_graceful_skip_no_backend(monkeypatch):
    monkeypatch.setattr(ba, "select_browser_backend", lambda: None)
    agent = BrowserAgent()
    assert agent.backend_available is False
    task = TaskRequest(input_data={"task_type": "browse", "url": "http://x"})
    res = _run(agent.execute(task))
    assert res.success is False
    assert res.output == {"skipped": True, "reason": "no browser backend"}


def test_agent_health_check_true_without_browser(monkeypatch):
    monkeypatch.setattr(ba, "select_browser_backend", lambda: None)
    agent = BrowserAgent()
    assert _run(agent.health_check()) is True


def test_agent_unknown_task_type(monkeypatch):
    monkeypatch.setattr(ba, "select_browser_backend", lambda: None)
    agent = BrowserAgent()
    task = TaskRequest(input_data={"task_type": "frobnicate", "url": "http://x"})
    res = _run(agent.execute(task))
    assert res.success is False
    assert "Unknown task type" in (res.error or "")


# ── happy path with a FAKE backend (no network, no browser) ──────────────


class _FakeBackend:
    name = "fake"

    def __init__(self):
        self.opened = None
        self.closed = False
        self.acted = []

    @classmethod
    def available(cls):
        return True

    async def open(self, url):
        self.opened = url

    async def act(self, instruction):
        self.acted.append(instruction)
        return BrowserStep(ok=True, detail=f"did {instruction}")

    async def snapshot(self):
        return BrowserSnapshot(
            url=self.opened or "",
            title="Fake Title",
            text="hello world body text",
            screenshot_path="/tmp/shot.png",
        )

    async def close(self):
        self.closed = True


def _agent_with(monkeypatch, fake):
    monkeypatch.setattr(ba, "select_browser_backend", lambda: fake)
    return BrowserAgent()


def test_browse_runs_instructions_and_snapshots(monkeypatch):
    fake = _FakeBackend()
    agent = _agent_with(monkeypatch, fake)
    assert agent.backend_available is True
    task = TaskRequest(
        input_data={
            "task_type": "browse",
            "url": "http://example.com",
            "instructions": ["click login", "type creds"],
        }
    )
    res = _run(agent.execute(task))
    assert res.success is True
    assert fake.opened == "http://example.com"
    assert res.output["backend"] == "fake"
    assert res.output["title"] == "Fake Title"
    assert len(res.output["steps"]) == 2
    assert fake.acted == ["click login", "type creds"]
    assert fake.closed is True  # backend always torn down


def test_extract_returns_truncated_text(monkeypatch):
    fake = _FakeBackend()
    agent = _agent_with(monkeypatch, fake)
    task = TaskRequest(
        input_data={"task_type": "extract", "url": "http://example.com", "max_chars": 5}
    )
    res = _run(agent.execute(task))
    assert res.success is True
    assert res.output["text"] == "hello"  # truncated to 5 chars
    assert fake.closed is True


def test_screenshot_returns_path(monkeypatch):
    fake = _FakeBackend()
    agent = _agent_with(monkeypatch, fake)
    task = TaskRequest(input_data={"task_type": "screenshot", "url": "http://x"})
    res = _run(agent.execute(task))
    assert res.success is True
    assert res.output["screenshot_path"] == "/tmp/shot.png"


def test_missing_url_errors(monkeypatch):
    fake = _FakeBackend()
    agent = _agent_with(monkeypatch, fake)
    task = TaskRequest(input_data={"task_type": "browse"})
    res = _run(agent.execute(task))
    assert res.success is False
    assert "No url" in (res.error or "")


def test_backend_closed_even_on_error(monkeypatch):
    class _Boom(_FakeBackend):
        async def open(self, url):
            raise RuntimeError("open failed")

    fake = _Boom()
    agent = _agent_with(monkeypatch, fake)
    task = TaskRequest(input_data={"task_type": "browse", "url": "http://x"})
    res = _run(agent.execute(task))
    assert res.success is False
    assert "open failed" in (res.error or "")
    assert fake.closed is True


def test_initialize_sets_backend_metadata(monkeypatch):
    fake = _FakeBackend()
    agent = _agent_with(monkeypatch, fake)
    _run(agent.initialize())
    assert agent.metadata["initialized"] is True
    assert agent.metadata["backend"] == "fake"


def test_local_act_without_open_returns_error():
    # No browser launched: act() on a fresh local backend must not crash.
    backend = LocalCdpBackend()
    step = _run(backend.act("goto http://x"))
    assert step.ok is False
    assert "no page open" in (step.error or "")


def test_local_close_idempotent():
    # close() on an un-opened backend is a safe no-op.
    backend = LocalCdpBackend()
    _run(backend.close())
    _run(backend.close())
