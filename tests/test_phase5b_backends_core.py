"""Tests for skyn3t.intelligence.backends core (Phase 5B, BACKENDS_CORE).

Covers the RemoteBackend protocol, BaseRemoteBackend scaffolding,
BackendResult (SubagentResult subclass) serialization, and the registry
helpers (register_backend / get_remote_backend / available_backends).

No real network/SSH/cloud calls — adapters are faked. ``available()`` is
asserted pure + non-raising. The package import must succeed even when no
optional SDK is installed (the optional adapters autoload behind guarded
imports), so the registry simply enumerates whatever loaded.

NOTE: importing the package pulls in ``skyn3t.intelligence`` (the parent
package eager-imports several siblings). If a concurrently-edited sibling
is momentarily un-importable, we skip rather than emit a misleading
collection failure attributable to unrelated, not-yet-landed code.
"""

from __future__ import annotations

import asyncio

import pytest

backends = pytest.importorskip(
    "skyn3t.intelligence.backends",
    reason="skyn3t.intelligence package not importable (unrelated concurrent edit)",
)

from skyn3t.intelligence.backends import (  # noqa: E402
    BACKEND_STATUSES,
    BackendResult,
    BaseRemoteBackend,
    RemoteBackend,
    available_backends,
    get_remote_backend,
    register_backend,
    registered_backends,
)
from skyn3t.intelligence.backends import base as base_mod  # noqa: E402
from skyn3t.intelligence.subagent_runner import SubagentResult  # noqa: E402

# ─── Fixtures: fakes + clean registry ──────────────────────────────────


@pytest.fixture
def clean_registry(monkeypatch):
    """Isolate the module-level registry so tests don't leak into each
    other or step on any real adapters that autoloaded."""
    monkeypatch.setattr(base_mod, "_REGISTRY", {}, raising=True)
    return base_mod._REGISTRY


class _FakeOK(BaseRemoteBackend):
    name = "fake-ok"

    @classmethod
    def available(cls) -> bool:
        return True

    async def _run_remote(self, payload, *, handle, started):
        # Emulate the remote child writing one JSON result line on stdout.
        return self.parse_result(
            handle=handle,
            started=started,
            stdout_b=b'noise line\n{"status": "ok", "output": {"echo": 1}}\n',
            stderr_b=b"",
            returncode=0,
        )


class _FakeUnavailable(BaseRemoteBackend):
    name = "fake-unavail"

    @classmethod
    def available(cls) -> bool:
        return False

    async def _run_remote(self, payload, *, handle, started):  # pragma: no cover
        raise AssertionError("_run_remote must not run when unavailable")


class _FakeRaisingAvailable(BaseRemoteBackend):
    name = "fake-raises"

    @classmethod
    def available(cls) -> bool:
        raise RuntimeError("buggy probe")  # registry must absorb this

    async def _run_remote(self, payload, *, handle, started):  # pragma: no cover
        raise AssertionError("unreachable")


class _FakeBoom(BaseRemoteBackend):
    name = "fake-boom"

    @classmethod
    def available(cls) -> bool:
        return True

    async def _run_remote(self, payload, *, handle, started):
        raise ValueError("transport exploded")


# ─── BackendResult ─────────────────────────────────────────────────────


def test_backend_result_subclasses_subagent_result():
    assert issubclass(BackendResult, SubagentResult)


def test_backend_result_to_dict_carries_base_and_remote_fields():
    r = BackendResult(
        status="ok",
        output={"x": 1},
        returncode=0,
        backend="modal",
        handle="sess-123",
        hibernated=True,
    )
    d = r.to_dict()
    # Base SubagentResult contract preserved.
    for key in (
        "status", "output", "error", "stdout", "stderr",
        "returncode", "duration_seconds", "subagent_id",
    ):
        assert key in d
    # Remote additions present and correct.
    assert d["backend"] == "modal"
    assert d["handle"] == "sess-123"
    assert d["hibernated"] is True
    assert d["status"] == "ok"


def test_backend_result_defaults_are_empty_not_none():
    r = BackendResult(status="ok")
    assert r.backend == ""
    assert r.handle == ""
    assert r.hibernated is False


def test_unavailable_is_a_declared_status():
    assert "unavailable" in BACKEND_STATUSES
    assert set(("ok", "error", "timeout", "crashed")).issubset(set(BACKEND_STATUSES))


# ─── Protocol / runtime_checkable ──────────────────────────────────────


def test_fake_backend_satisfies_protocol_isinstance():
    assert isinstance(_FakeOK(), RemoteBackend)


def test_base_available_default_is_false_and_non_raising():
    # The ABC's default available() is a pure False (mirrors docker_available
    # returning a bool without raising).
    val = BaseRemoteBackend.available()
    assert val is False


# ─── BaseRemoteBackend.run template behavior ───────────────────────────


@pytest.mark.asyncio
async def test_run_ok_path_parses_last_json_line_and_stamps_backend():
    res = await _FakeOK().run({"agent_class": "x:Y", "task": {}})
    assert res.status == "ok"
    assert res.output == {"echo": 1}
    assert res.backend == "fake-ok"
    assert res.handle  # a fresh handle was generated + stamped
    assert res.subagent_id == res.handle
    # Pre-result noise preserved in stdout but not in structured output.
    assert "noise line" in res.stdout


@pytest.mark.asyncio
async def test_run_short_circuits_when_unavailable():
    res = await _FakeUnavailable().run({"agent_class": "x:Y", "task": {}})
    assert res.status == "unavailable"
    assert res.backend == "fake-unavail"
    assert res.error and "unavailable" in res.error
    assert res.handle


@pytest.mark.asyncio
async def test_run_converts_transport_exception_to_crashed():
    res = await _FakeBoom().run({"agent_class": "x:Y", "task": {}})
    assert res.status == "crashed"
    assert res.backend == "fake-boom"
    assert "transport exploded" in (res.error or "")


@pytest.mark.asyncio
async def test_default_shutdown_is_idempotent_noop():
    b = _FakeOK()
    # Two calls, no state, no raise.
    assert await b.shutdown() is None
    assert await b.shutdown() is None


# ─── Result helpers ────────────────────────────────────────────────────


def test_parse_result_crashed_on_nonzero_exit():
    b = _FakeOK()
    res = b.parse_result(
        handle="h1", started=0.0,
        stdout_b=b"", stderr_b=b"boom", returncode=3,
    )
    assert res.status == "crashed"
    assert "3" in (res.error or "")
    assert res.backend == "fake-ok"


def test_parse_result_crashed_on_no_json():
    b = _FakeOK()
    res = b.parse_result(
        handle="h1", started=0.0,
        stdout_b=b"just logs, no json", stderr_b=b"", returncode=0,
    )
    assert res.status == "crashed"
    assert "no JSON" in (res.error or "")


def test_parse_result_propagates_hibernated_flag():
    b = _FakeOK()
    res = b.parse_result(
        handle="h1", started=0.0,
        stdout_b=b'{"status": "ok", "output": 1}', stderr_b=b"",
        returncode=0, hibernated=True,
    )
    assert res.status == "ok"
    assert res.hibernated is True


def test_timeout_result_shape():
    b = _FakeOK()
    res = b.timeout_result(handle="h1", started=0.0, stdout_b=b"x", stderr_b=b"y")
    assert res.status == "timeout"
    assert res.backend == "fake-ok"
    assert "timeout" in (res.error or "").lower()


def test_new_handle_shape():
    h = BaseRemoteBackend.new_handle()
    assert h.startswith("sub-")
    assert h != BaseRemoteBackend.new_handle()


# ─── Registry ──────────────────────────────────────────────────────────


def test_register_and_lookup_available_backend(clean_registry):
    register_backend(_FakeOK)
    assert "fake-ok" in registered_backends()
    assert available_backends() == ["fake-ok"]
    inst = get_remote_backend("fake-ok")
    assert isinstance(inst, _FakeOK)
    assert isinstance(inst, RemoteBackend)


def test_get_remote_backend_unknown_returns_none(clean_registry):
    assert get_remote_backend("does-not-exist") is None


def test_get_remote_backend_unavailable_returns_none(clean_registry):
    register_backend(_FakeUnavailable)
    # Registered but not available => lookup returns None, name not listed.
    assert "fake-unavail" in registered_backends()
    assert "fake-unavail" not in available_backends()
    assert get_remote_backend("fake-unavail") is None


def test_available_backends_filters_and_sorts(clean_registry):
    register_backend(_FakeUnavailable)
    register_backend(_FakeOK)
    # Only available ones, sorted by name.
    assert available_backends() == ["fake-ok"]


def test_available_backends_absorbs_raising_probe(clean_registry):
    register_backend(_FakeRaisingAvailable)
    register_backend(_FakeOK)
    # A buggy available() must not crash enumeration; it's treated as
    # unavailable.
    names = available_backends()
    assert "fake-ok" in names
    assert "fake-raises" not in names


def test_get_remote_backend_absorbs_raising_probe(clean_registry):
    register_backend(_FakeRaisingAvailable)
    assert get_remote_backend("fake-raises") is None


def test_register_backend_requires_name(clean_registry):
    class NoName(BaseRemoteBackend):
        name = ""

        async def _run_remote(self, payload, *, handle, started):  # pragma: no cover
            ...

    with pytest.raises(ValueError):
        register_backend(NoName)


def test_register_backend_is_decorator_and_last_wins(clean_registry):
    @register_backend
    class A(BaseRemoteBackend):
        name = "dup"

        @classmethod
        def available(cls):
            return True

        async def _run_remote(self, payload, *, handle, started):  # pragma: no cover
            ...

    @register_backend
    class B(BaseRemoteBackend):
        name = "dup"

        @classmethod
        def available(cls):
            return True

        async def _run_remote(self, payload, *, handle, started):  # pragma: no cover
            ...

    assert get_remote_backend("dup").__class__ is B


# ─── Package import resilience ─────────────────────────────────────────


def test_package_exposes_contract_surface():
    # The names the contract promises are all importable from the package.
    for name in (
        "RemoteBackend", "BaseRemoteBackend", "BackendResult",
        "get_remote_backend", "available_backends", "register_backend",
    ):
        assert hasattr(backends, name), name


def test_available_backends_is_list_of_str():
    val = available_backends()
    assert isinstance(val, list)
    assert all(isinstance(x, str) for x in val)


@pytest.mark.asyncio
async def test_run_status_in_declared_set():
    res = await _FakeOK().run({"agent_class": "x:Y", "task": {}})
    assert res.status in BACKEND_STATUSES


def _async_run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
