"""Regression tests for the sandbox audit fixes (group: sandbox).

Covers three confirmed bugs in ``skyn3t/security/sandbox.py``:

  C1  ``get_backend("auto")`` must NOT silently fall back to the insecure
      in-process ``InlineBackend`` when Docker is unavailable. It must raise
      unless the operator opts in via ``SKYN3T_ALLOW_INLINE_EXEC``.
  H1  The macOS seatbelt profile must deny-by-default (not allow-by-default)
      while still allowing system paths + the caller's allowed/temp dirs.
  H2  ``DockerBackend.execute`` must forcibly ``docker kill`` the container
      (and kill the local client process) when a run times out.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from skyn3t.security.sandbox import (
    DockerBackend,
    InlineBackend,
    Sandbox,
    SandboxConfig,
    get_backend,
)

# ---------------------------------------------------------------------------
# C1 — get_backend("auto") no longer silently runs untrusted code in-process
# ---------------------------------------------------------------------------


class TestAutoBackendNoSilentInline:
    @pytest.mark.asyncio
    async def test_auto_raises_when_docker_missing_and_not_opted_in(
        self, monkeypatch
    ) -> None:
        # Docker unavailable, no opt-in flag -> must refuse (the OLD buggy
        # behavior returned an InlineBackend here).
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.delenv("SKYN3T_ALLOW_INLINE_EXEC", raising=False)
        with pytest.raises(RuntimeError, match="in-process execution is disabled"):
            await get_backend("auto")

    @pytest.mark.asyncio
    async def test_auto_allows_inline_only_with_explicit_optin(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setenv("SKYN3T_ALLOW_INLINE_EXEC", "1")
        backend = await get_backend("auto")
        assert isinstance(backend, InlineBackend)

    @pytest.mark.asyncio
    async def test_explicit_inline_still_works(self) -> None:
        # Explicit opt-in backend must keep working regardless of the flag.
        backend = await get_backend("inline")
        assert isinstance(backend, InlineBackend)


# ---------------------------------------------------------------------------
# H1 — seatbelt profile denies by default
# ---------------------------------------------------------------------------


class TestSeatbeltDenyDefault:
    def test_profile_denies_by_default(self, tmp_path) -> None:
        cfg = SandboxConfig(allowed_dirs=[tmp_path])
        sandbox = Sandbox(cfg)
        profile = sandbox._build_seatbelt_profile(tmp_path)

        assert "(deny default)" in profile
        assert "(allow default)" not in profile
        # Still allows the system paths CLI agents need.
        assert '(allow file-read* (subpath "/usr"))' in profile
        assert '(allow process-exec (subpath "/usr")' in profile
        # The caller's allowed dir is allowed (read/write/exec).
        allowed = str(Path(tmp_path).resolve())
        assert f'process-exec (subpath "{allowed}")' in profile

    def test_credential_dirs_denied_after_allows(self, tmp_path) -> None:
        cfg = SandboxConfig(allowed_dirs=[tmp_path])
        sandbox = Sandbox(cfg)
        profile = sandbox._build_seatbelt_profile(tmp_path)

        # Belt-and-suspenders deny rules must come AFTER the broad allow rules
        # so that last-match-wins leaves credential dirs denied.
        deny_idx = profile.index("(deny default)")
        ssh_deny = str((Path.home() / ".ssh").resolve())
        assert ssh_deny in profile
        assert profile.index(ssh_deny) > deny_idx
        # The deny for the credential dir comes after the system allow rules.
        assert profile.index(ssh_deny) > profile.index('(subpath "/usr")')


# ---------------------------------------------------------------------------
# H2 — DockerBackend kills the container on timeout
# ---------------------------------------------------------------------------


class _FakeProc:
    """A subprocess stand-in whose communicate() never returns."""

    def __init__(self) -> None:
        self.killed = False
        self.returncode = None

    async def communicate(self, input=None):  # noqa: A002 - mirror stdlib
        await asyncio.sleep(3600)  # hang forever -> triggers wait_for timeout

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.returncode = -9
        return -9


class TestDockerTimeoutKillsContainer:
    @pytest.mark.asyncio
    async def test_timeout_invokes_docker_kill(self, monkeypatch) -> None:
        backend = DockerBackend()
        backend._available = True  # skip real docker probe

        run_proc = _FakeProc()
        kill_calls: list[list[str]] = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            if "run" in args:
                return run_proc
            if "kill" in args:
                kill_calls.append(list(args))
                return _KillProc()
            return _KillProc()

        monkeypatch.setattr(
            "asyncio.create_subprocess_exec", fake_create_subprocess_exec
        )

        result = await backend.execute("while True: pass", "python", timeout=1)

        assert result.success is False
        assert "timed out" in result.error
        # The local docker-run client was killed.
        assert run_proc.killed is True
        # docker kill <name> was invoked with the deterministic container name.
        assert kill_calls, "expected an awaited 'docker kill <name>' call"
        kill_args = kill_calls[0]
        assert "kill" in kill_args
        assert any(a.startswith("skyn3t-run-python-") for a in kill_args)


class _KillProc:
    """A trivial subprocess stand-in for the `docker kill` call."""

    def __init__(self) -> None:
        self.returncode = 0

    async def communicate(self, input=None):  # noqa: A002 - mirror stdlib
        return (b"", b"")

    async def wait(self) -> int:
        return 0
