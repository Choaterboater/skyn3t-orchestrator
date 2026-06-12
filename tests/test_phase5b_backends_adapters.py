"""Tests for the Phase 5B remote-execution backend adapters.

Covers SSH / Modal / Daytona / E2B (owner: BACKENDS_ADAPTERS). None of
the cloud SDKs (paramiko/modal/daytona/e2b) are installed in CI and no
creds are set, so every adapter must:

  - import cleanly with NO SDK present (lazy imports inside methods),
  - report ``available() is False`` (never raise),
  - self-register so ``get_remote_backend`` / ``available_backends`` see
    them, returning ``None`` / excluding them while unavailable,
  - return ``BackendResult(status='unavailable')`` from a *forced* run
    rather than crashing or blocking,
  - expose a stable ``backend`` name and carry ``backend/handle/
    hibernated`` through ``to_dict()``.

These tests NEVER touch the network or spawn a real sandbox: the cloud
paths are unreachable without creds+SDK, and SSH command shape is
asserted via ``_build_cmd`` (no connection made).

Skips gracefully if BACKENDS_CORE's ``base.py`` is not yet present.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip(
    "skyn3t.intelligence.backends.base",
    reason="backends.base (owned by BACKENDS_CORE) not present yet",
)

from skyn3t.intelligence.backends import (  # noqa: E402
    daytona_backend,
    e2b_backend,
    modal_backend,
    ssh_backend,
)
from skyn3t.intelligence.backends.base import (  # noqa: E402
    BackendResult,
    BaseRemoteBackend,
    available_backends,
    get_remote_backend,
)
from skyn3t.intelligence.subagent_runner import SubagentResult  # noqa: E402

ALL_ADAPTERS = [
    ("ssh", ssh_backend.SSHBackend),
    ("modal", modal_backend.ModalBackend),
    ("daytona", daytona_backend.DaytonaBackend),
    ("e2b", e2b_backend.E2BBackend),
]

CRED_ENV = [
    "SKYN3T_SSH_HOST",
    "SKYN3T_SSH_USER",
    "SKYN3T_SSH_PORT",
    "SKYN3T_SSH_KEY",
    "SKYN3T_SSH_REMOTE_ROOT",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "DAYTONA_API_KEY",
    "E2B_API_KEY",
]


@pytest.fixture(autouse=True)
def _scrub_creds(monkeypatch):
    """Guarantee a no-creds environment so the graceful path is tested
    regardless of the operator's shell."""
    for key in CRED_ENV:
        monkeypatch.delenv(key, raising=False)
    yield


# ─── Subclassing / contract identity ───────────────────────────────────


def test_backend_result_subclasses_subagent_result():
    assert issubclass(BackendResult, SubagentResult)


@pytest.mark.parametrize("name,cls", ALL_ADAPTERS)
def test_adapter_subclasses_base_and_has_stable_name(name, cls):
    assert issubclass(cls, BaseRemoteBackend)
    assert cls.name == name


# ─── available() is pure + non-raising + False without creds ───────────


@pytest.mark.parametrize("name,cls", ALL_ADAPTERS)
def test_available_returns_bool_and_is_false_without_creds(name, cls):
    val = cls.available()
    assert isinstance(val, bool)
    assert val is False


def test_ssh_available_true_when_host_set(monkeypatch):
    """SSH counts the system ssh CLI toward availability, so setting the
    host gate flips it on (the CLI is present on macOS/Linux CI)."""
    import shutil

    if shutil.which("ssh") is None:
        pytest.skip("ssh CLI not on PATH in this environment")
    monkeypatch.setenv("SKYN3T_SSH_HOST", "host.example.com")
    assert ssh_backend.SSHBackend.available() is True


def test_modal_available_false_with_creds_but_no_sdk(monkeypatch):
    """Creds present but SDK absent => still unavailable (no crash)."""
    monkeypatch.setenv("MODAL_TOKEN_ID", "id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "secret")
    assert modal_backend.ModalBackend.available() is False


def test_daytona_available_false_with_creds_but_no_sdk(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "key")
    assert daytona_backend.DaytonaBackend.available() is False


def test_e2b_available_false_with_creds_but_no_sdk(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "key")
    assert e2b_backend.E2BBackend.available() is False


# ─── Registry behavior ─────────────────────────────────────────────────


@pytest.mark.parametrize("name,cls", ALL_ADAPTERS)
def test_get_remote_backend_returns_none_when_unavailable(name, cls):
    assert get_remote_backend(name) is None


def test_get_remote_backend_unknown_returns_none():
    assert get_remote_backend("does-not-exist") is None


def test_available_backends_excludes_unavailable():
    names = available_backends()
    assert isinstance(names, list)
    for name, _cls in ALL_ADAPTERS:
        assert name not in names


# ─── Forced run => unavailable, never crash, never block ───────────────


@pytest.mark.parametrize("name,cls", ALL_ADAPTERS)
def test_forced_run_returns_unavailable_result(name, cls):
    inst = cls()
    result = asyncio.run(inst.run({"agent_class": "x:Y", "task": {}}))
    assert isinstance(result, BackendResult)
    assert result.status == "unavailable"
    assert result.backend == name
    assert result.handle  # non-empty remote handle id
    assert result.error  # human-readable reason
    d = result.to_dict()
    assert d["backend"] == name
    assert d["status"] == "unavailable"
    assert "handle" in d and "hibernated" in d


@pytest.mark.parametrize("name,cls", ALL_ADAPTERS)
def test_shutdown_is_idempotent(name, cls):
    inst = cls()

    async def _go():
        await inst.shutdown()
        await inst.shutdown()  # second call must also be a no-op

    asyncio.run(_go())


# ─── SSH command shape (no network) ────────────────────────────────────


def test_ssh_build_cmd_shape(monkeypatch):
    monkeypatch.setenv("SKYN3T_SSH_HOST", "host.example.com")
    monkeypatch.setenv("SKYN3T_SSH_USER", "deploy")
    monkeypatch.setenv("SKYN3T_SSH_PORT", "2200")
    monkeypatch.setenv("SKYN3T_SSH_KEY", "/keys/id_ed25519")
    monkeypatch.setenv("SKYN3T_SSH_REMOTE_ROOT", "/opt/skyn3t")
    cmd = ssh_backend.SSHBackend()._build_cmd("sub-test")
    assert cmd[0] == "ssh"
    assert cmd[cmd.index("-p") + 1] == "2200"
    assert "BatchMode=yes" in cmd
    assert cmd[cmd.index("-i") + 1] == "/keys/id_ed25519"
    assert "deploy@host.example.com" in cmd
    # The remote command runs the in-tree child entrypoint after cd.
    assert cmd[-1] == "cd /opt/skyn3t && python3 -m skyn3t.intelligence.subagent_runner"


def test_ssh_build_cmd_without_optional_params(monkeypatch):
    """Only the host is required; everything else has defaults and the
    cmd still resolves the child entrypoint."""
    monkeypatch.setenv("SKYN3T_SSH_HOST", "bare.example.com")
    cmd = ssh_backend.SSHBackend()._build_cmd("sub-x")
    assert "bare.example.com" in cmd
    assert "-i" not in cmd  # no key set
    assert cmd[cmd.index("-p") + 1] == "22"  # default port
    assert cmd[-1] == "python3 -m skyn3t.intelligence.subagent_runner"


# ─── Serverless backends flag hibernation on teardown ──────────────────


def test_modal_and_daytona_marked_serverless_hibernating():
    """Contract: Modal/Daytona hibernate when idle. The unavailable
    fast-path keeps hibernated False (nothing was provisioned); the flag
    exists and is a bool on every result."""
    for cls in (modal_backend.ModalBackend, daytona_backend.DaytonaBackend):
        r = asyncio.run(cls().run({"agent_class": "x:Y", "task": {}}))
        assert isinstance(r.hibernated, bool)
        assert r.hibernated is False  # never provisioned => nothing to hibernate
