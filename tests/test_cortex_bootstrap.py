"""Tests for ``skyn3t.cortex.bootstrap.CortexBootstrap``.

The bootstrap is the consolidated lifecycle owner for the autonomy
cortex. Tests focus on the contract operators rely on:

- start/stop are idempotent and don't raise on double-call
- a broken component is isolated — siblings still come up
- the disable env var (``SKYN3T_CORTEX_DISABLE``) skips by name
- ``status()`` reflects per-component started/error state and
  surfaces proposal handlers + counts via ProposalStore
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from skyn3t.cortex.bootstrap import CortexBootstrap, _disabled_set


# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------


class _FakeEventBus:
    def __init__(self):
        self.published: list = []

    def publish(self, event):
        self.published.append(event)

    def subscribe(self, *_a, **_k):
        return None

    def unsubscribe(self, *_a, **_k):
        return None


def _fake_orchestrator() -> SimpleNamespace:
    return SimpleNamespace(
        event_bus=_FakeEventBus(),
        agents={},
    )


# ---------------------------------------------------------------------
# Disable parsing
# ---------------------------------------------------------------------


def test_disabled_set_parses_csv(monkeypatch):
    monkeypatch.setenv("SKYN3T_CORTEX_DISABLE", "curiosity,auto_cleanup")
    assert _disabled_set() == {"curiosity", "auto_cleanup"}


def test_disabled_set_handles_star(monkeypatch):
    monkeypatch.setenv("SKYN3T_CORTEX_DISABLE", "*")
    assert _disabled_set() == {"*"}


def test_disabled_set_empty_when_unset(monkeypatch):
    monkeypatch.delenv("SKYN3T_CORTEX_DISABLE", raising=False)
    assert _disabled_set() == set()


# ---------------------------------------------------------------------
# Lifecycle: happy path with fully-faked components
# ---------------------------------------------------------------------


class _SyncStartStop:
    def __init__(self, *_a, **_k):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _AsyncStartStop:
    def __init__(self, *_a, **_k):
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class _NoStop:
    def __init__(self, *_a, **_k):
        self.started = False

    def start(self):
        self.started = True


class _RaisesOnStart:
    def __init__(self, *_a, **_k):
        pass

    def start(self):
        raise RuntimeError("boom")


def _stub_specs(monkeypatch, mapping):
    """Replace ``_register_default_components`` with a controllable stub.

    ``mapping`` is name -> factory. Each factory either returns an
    instance or raises (construct failure). Bootstrap behaviour around
    those paths is what we want to verify, separately from the real
    cortex modules.
    """
    from skyn3t.cortex import bootstrap as bs

    def _replacement(self):
        for name, factory in mapping.items():
            try:
                instance = factory()
            except Exception as exc:
                self._components.append(
                    bs._Component(
                        name=name,
                        instance=bs._DisabledMarker(name),
                        error=f"construct: {exc}",
                        skipped_reason="construct failed",
                    )
                )
                continue
            self._components.append(bs._Component(name=name, instance=instance))

    monkeypatch.setattr(CortexBootstrap, "_register_default_components", _replacement)


def _stub_install_handlers(monkeypatch, *, succeed: bool = True):
    """Make handler install a no-op (or raise) without importing cortex.handlers."""

    async def _replacement(self):
        if succeed:
            self._handlers_installed = True
        else:
            raise RuntimeError("handlers boom")

    from skyn3t.cortex.bootstrap import CortexBootstrap as _CB

    monkeypatch.setattr(_CB, "_install_handlers", _replacement)


@pytest.mark.asyncio
async def test_start_invokes_each_component_lifecycle(monkeypatch):
    sync = _SyncStartStop()
    asyncio_inst = _AsyncStartStop()
    no_stop = _NoStop()
    _stub_specs(monkeypatch, {
        "sync": lambda: sync,
        "async": lambda: asyncio_inst,
        "nostop": lambda: no_stop,
    })
    _stub_install_handlers(monkeypatch)

    cb = CortexBootstrap(_fake_orchestrator())
    await cb.start()

    assert sync.started is True
    assert asyncio_inst.started is True
    assert no_stop.started is True
    status = cb.status()
    assert status["booted"] is True
    assert {c["name"] for c in status["components"]} == {"sync", "async", "nostop"}
    assert all(c["started"] for c in status["components"])


@pytest.mark.asyncio
async def test_start_is_idempotent(monkeypatch):
    inst = _SyncStartStop()
    _stub_specs(monkeypatch, {"only": lambda: inst})
    _stub_install_handlers(monkeypatch)

    cb = CortexBootstrap(_fake_orchestrator())
    await cb.start()
    # Force the started flag down to detect re-entry; calling start again
    # should be a no-op (does NOT re-invoke component.start).
    inst.started = "sentinel"
    await cb.start()
    assert inst.started == "sentinel"


@pytest.mark.asyncio
async def test_failure_in_one_component_does_not_abort_others(monkeypatch):
    good = _SyncStartStop()
    _stub_specs(monkeypatch, {
        "bad": lambda: _RaisesOnStart(),
        "good": lambda: good,
    })
    _stub_install_handlers(monkeypatch)

    cb = CortexBootstrap(_fake_orchestrator())
    await cb.start()

    assert good.started is True
    statuses = {c["name"]: c for c in cb.status()["components"]}
    assert statuses["bad"]["started"] is False
    assert "boom" in (statuses["bad"]["error"] or "")
    assert statuses["good"]["started"] is True


@pytest.mark.asyncio
async def test_stop_calls_lifecycle_only_on_started_components(monkeypatch):
    a = _AsyncStartStop()
    b_no_stop = _NoStop()
    _stub_specs(monkeypatch, {"a": lambda: a, "b": lambda: b_no_stop})
    _stub_install_handlers(monkeypatch)

    cb = CortexBootstrap(_fake_orchestrator())
    await cb.start()
    await cb.stop()

    assert a.stopped is True
    # b had no stop() — should not have crashed, just skipped.
    assert cb._wired is False


@pytest.mark.asyncio
async def test_stop_is_idempotent_before_start(monkeypatch):
    cb = CortexBootstrap(_fake_orchestrator())
    # Should not raise even though nothing's been wired.
    await cb.stop()
    assert cb._wired is False


# ---------------------------------------------------------------------
# Status payload
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_surfaces_proposal_store_counts(monkeypatch, tmp_path):
    """``status()`` pulls handler list + counts + recent failures from
    the proposal store. Validate the wiring by swapping in a fake store."""
    from skyn3t.cortex import bootstrap as bs

    class _FakeStore:
        def registered_handlers(self):
            return ["tuning", "feature", "studio_debug"]

        def counts(self):
            return {"pending": 2, "approved": 1, "failed": 1}

        def recent_failures(self, limit=5):
            return [{"id": "p1", "kind": "tuning", "title": "Tune x", "error": "boom"}]

    monkeypatch.setattr(bs.CortexBootstrap, "_proposal_store", lambda self: _FakeStore())
    _stub_specs(monkeypatch, {})
    _stub_install_handlers(monkeypatch)

    cb = CortexBootstrap(_fake_orchestrator())
    await cb.start()

    st = cb.status()
    assert st["proposal_handlers"] == ["tuning", "feature", "studio_debug"]
    assert st["proposal_counts"] == {"pending": 2, "approved": 1, "failed": 1}
    assert st["recent_failures"][0]["kind"] == "tuning"


@pytest.mark.asyncio
async def test_status_component_details_via_get_status(monkeypatch):
    class _WithDetails:
        def start(self):
            self.started = True

        def get_status(self):
            return {"config_path": "data/config/runtime.json", "wired": True}

    _stub_specs(monkeypatch, {"detailed": lambda: _WithDetails()})
    _stub_install_handlers(monkeypatch)

    cb = CortexBootstrap(_fake_orchestrator())
    await cb.start()
    comp = cb.status()["components"][0]
    assert comp["details"]["config_path"] == "data/config/runtime.json"


# ---------------------------------------------------------------------
# Disable env var
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_env_var_skips_named_components(monkeypatch):
    monkeypatch.setenv("SKYN3T_CORTEX_DISABLE", "curiosity")
    cb = CortexBootstrap(_fake_orchestrator())
    # Use the real registration (not the stub) to verify env-var path.
    # Components that require external state (gated_tuner config dir,
    # auto_cleanup repo) may legitimately fail to construct in CI; we
    # only care that "curiosity" appears as skipped.
    _stub_install_handlers(monkeypatch)
    await cb.start()
    names_to_status = {c["name"]: c for c in cb.status()["components"]}
    curiosity = names_to_status.get("curiosity")
    assert curiosity is not None
    assert curiosity["started"] is False
    assert "disabled" in (curiosity["skipped_reason"] or "")


@pytest.mark.asyncio
async def test_disable_env_var_star_skips_everything(monkeypatch):
    monkeypatch.setenv("SKYN3T_CORTEX_DISABLE", "*")
    _stub_install_handlers(monkeypatch)
    cb = CortexBootstrap(_fake_orchestrator())
    await cb.start()
    for c in cb.status()["components"]:
        assert c["started"] is False
        assert "disabled" in (c["skipped_reason"] or "")


# ---------------------------------------------------------------------
# ProposalStore additions used by status()
# ---------------------------------------------------------------------


def test_proposal_store_counts(tmp_path):
    from skyn3t.cortex.proposals import ProposalStore

    store = ProposalStore(root=tmp_path / "proposals")
    store.create(kind="tuning", title="t1", summary="", detail="", payload={}, source="t")
    store.create(kind="tuning", title="t2", summary="", detail="", payload={}, source="t")
    store.create(kind="feature", title="f1", summary="", detail="", payload={}, source="t")

    counts = store.counts()
    assert counts.get("pending") == 3


def test_proposal_store_recent_failures(tmp_path):
    """Failed proposals (created without a handler then approved) should
    show up in ``recent_failures`` sorted newest-first."""
    from skyn3t.cortex.proposals import ProposalStore

    store = ProposalStore(root=tmp_path / "proposals")
    pids = []
    for i in range(3):
        p = store.create(
            kind="nope", title=f"t{i}", summary="", detail="",
            payload={}, source="t",
        )
        pids.append(p.id)

    async def _approve_all():
        for pid in pids:
            await store.approve(pid)

    asyncio.run(_approve_all())

    failures = store.recent_failures(limit=2)
    assert len(failures) == 2
    assert all(f["kind"] == "nope" for f in failures)
    assert all("no handler" in (f["error"] or "") for f in failures)
