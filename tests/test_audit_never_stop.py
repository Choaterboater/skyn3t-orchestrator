"""Regression test: never-stop watchdog must recover an exception-killed loop.

When a monitored component's loop task dies with a non-CancelledError exception
(e.g. an ImportError raised above the inner try/except), the component's own
``stop()`` does ``await self._task`` and only swallows ``CancelledError``. Awaiting
an already-finished, exception-dead task re-raises the stored exception. Before the
fix, that exception propagated out of ``_restart_component`` as ``recovery_failed``:
``start()`` never ran and the component's ``_running`` flag stayed True, so the
watchdog looped on the same failure forever.

This test uses a real asyncio task that died with an ImportError and a real
component ``stop()`` that mirrors the production implementations, so it exercises
the actual re-raise behavior rather than a mock.
"""

from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import MagicMock

import pytest

from skyn3t.config.settings import get_settings
from skyn3t.cortex.never_stop import NeverStopWatchdog


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeComponent:
    """Mirrors a monitored component whose loop died with a real exception.

    ``stop()`` reproduces the production pattern: cancel the task then
    ``await self._task`` catching only ``CancelledError`` — which re-raises a
    stored non-CancelledError exception on a finished task.
    """

    def __init__(self) -> None:
        self._running = True
        self._task: Optional[asyncio.Task] = None
        self.start_calls = 0

    async def _doomed_loop(self) -> None:
        # Simulate an ImportError raised above the inner try/except: the loop
        # task dies immediately with a non-CancelledError exception.
        raise ImportError("module that backs the loop went missing")

    async def boot_doomed(self) -> None:
        self._task = asyncio.create_task(self._doomed_loop())
        # Let the task run to completion (and store the exception) without
        # surfacing it here.
        try:
            await self._task
        except ImportError:
            pass

    async def stop(self) -> None:
        # Exact shape of the production stop(): only CancelledError is swallowed.
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def start(self) -> None:
        # start() short-circuits while _running is True (matches production).
        if self._running:
            return
        self._running = True
        self.start_calls += 1
        self._task = asyncio.create_task(asyncio.sleep(3600))


@pytest.mark.asyncio
async def test_watchdog_recovers_exception_killed_loop(monkeypatch):
    monkeypatch.setenv("SKYN3T_NEVER_STOP", "1")
    monkeypatch.setenv("SKYN3T_CONTINUOUS_IMPROVEMENT", "1")
    get_settings.cache_clear()

    comp = _FakeComponent()
    await comp.boot_doomed()

    # The task is finished and holds a non-CancelledError exception.
    assert comp._task is not None
    assert comp._task.done()
    assert isinstance(comp._task.exception(), ImportError)

    bus = MagicMock()
    orch = MagicMock()
    orch._running = True
    orch._continuous_improvement = comp
    orch._autonomous_coordinator = None
    orch._agent_fleet_coordinator = None

    watchdog = NeverStopWatchdog(orch, bus)

    # Before the fix: stop() re-raises the ImportError, recovery is reported as
    # recovery_failed, start() is never called, and _running stays True.
    await watchdog._restart_component("continuous_improvement", comp)

    # After the fix: start() actually ran and the component is live again.
    assert comp.start_calls == 1, "start() must run to restart the dead loop"
    assert comp._running is True
    assert comp._task is not None and not comp._task.done()
    assert watchdog._recoveries_total == 1
    assert watchdog._last_recovery_at is not None

    payload = bus.publish.call_args[0][0].payload
    assert payload.get("kind") == "NEVER_STOP_RECOVERED"
    assert payload.get("component") == "continuous_improvement"
    assert not str(payload.get("reason", "")).startswith("recovery_failed")

    # Clean up the long-lived sleep task.
    comp._task.cancel()
    try:
        await comp._task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_watchdog_swallows_stop_reraise_and_still_restarts(monkeypatch):
    """A re-raising stop() must not abort recovery; start() still runs."""
    monkeypatch.setenv("SKYN3T_NEVER_STOP", "1")
    monkeypatch.setenv("SKYN3T_CONTINUOUS_IMPROVEMENT", "1")
    get_settings.cache_clear()

    comp = _FakeComponent()
    await comp.boot_doomed()

    stop_called = False
    real_stop = comp.stop

    async def _reraising_stop() -> None:
        nonlocal stop_called
        stop_called = True
        # Mirror the production failure mode directly: awaiting the finished,
        # exception-dead task re-raises the stored exception (only CancelledError
        # is caught by the real stop()).
        await real_stop()
        raise ImportError("module that backs the loop went missing")

    comp.stop = _reraising_stop  # type: ignore[assignment]

    orch = MagicMock()
    orch._running = True
    orch._continuous_improvement = comp
    orch._autonomous_coordinator = None
    orch._agent_fleet_coordinator = None

    watchdog = NeverStopWatchdog(orch, MagicMock())
    await watchdog._restart_component("continuous_improvement", comp)

    # stop() is invoked, its exception is swallowed, and start() still runs.
    assert stop_called is True
    assert comp.start_calls == 1
    assert comp._running is True
    assert watchdog._recoveries_total == 1

    comp._task.cancel()
    try:
        await comp._task
    except asyncio.CancelledError:
        pass
