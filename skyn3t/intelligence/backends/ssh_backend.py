"""SSH remote execution backend.

Hermes parity: run a subagent on a *remote host* over SSH using the
same one-JSON-line-in / one-JSON-line-out contract as the local
``SubagentRunner`` and the ``DockerSubagentRunner``. The remote host
runs ``python -m skyn3t.intelligence.subagent_runner`` (the in-tree
child entrypoint); we feed the task payload on stdin and read the
result JSON off stdout.

Design notes:

  - Transport is the system ``ssh`` CLI (ships on macOS / Linux).
    ``paramiko`` is *optional* — if present it counts toward availability
    too, but we prefer the CLI because it Just Works with the operator's
    existing ``~/.ssh/config``, agent, and keys.
  - Connection params come from env (``SKYN3T_SSH_*``) so nothing is
    hardcoded and no secret lives in the repo. ``SKYN3T_SSH_HOST`` is the
    single required gate; everything else has sane defaults.
  - The remote is expected to already have the project source on its
    PYTHONPATH (operator provisions it once, or sets
    ``SKYN3T_SSH_REMOTE_ROOT`` so we ``cd`` there first). We do NOT rsync
    the tree — that's a separate provisioning concern.

Graceful degradation (the binding rule): if ``SKYN3T_SSH_HOST`` is unset,
or neither the ssh CLI nor paramiko is importable, ``available()`` returns
``False`` (never raises). The base ``run()`` then short-circuits to a
``BackendResult(status='unavailable')`` — never a crash, never a block.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from skyn3t.intelligence.backends.base import (
    BackendResult,
    BaseRemoteBackend,
    register_backend,
)


def _ssh_cli_available() -> bool:
    """Is the ssh CLI on PATH? Cheap, non-raising."""
    return shutil.which("ssh") is not None


def _paramiko_importable() -> bool:
    """Lazy probe for the optional paramiko SDK; never raises."""
    try:
        import importlib.util

        return importlib.util.find_spec("paramiko") is not None
    except Exception:
        return False


@register_backend
class SSHBackend(BaseRemoteBackend):
    """Run a subagent on a remote host over SSH.

    Availability gate: ``SKYN3T_SSH_HOST`` set AND (ssh CLI on PATH OR
    paramiko importable). All connection params are read from env at
    ``run()`` time so the operator can rotate hosts without restarting.
    SSH sessions are per-command, so there is no long-lived session to
    hibernate and ``shutdown()`` is a no-op.
    """

    name = "ssh"

    @classmethod
    def available(cls) -> bool:
        if not os.getenv("SKYN3T_SSH_HOST"):
            return False
        return _ssh_cli_available() or _paramiko_importable()

    # -- connection params (env-driven) ---------------------------------

    @staticmethod
    def _host() -> Optional[str]:
        return os.getenv("SKYN3T_SSH_HOST") or None

    @staticmethod
    def _user() -> Optional[str]:
        return os.getenv("SKYN3T_SSH_USER") or None

    @staticmethod
    def _port() -> str:
        return os.getenv("SKYN3T_SSH_PORT", "22")

    @staticmethod
    def _key() -> Optional[str]:
        return os.getenv("SKYN3T_SSH_KEY") or None

    @staticmethod
    def _remote_python() -> str:
        return os.getenv("SKYN3T_SSH_PYTHON", "python3")

    @staticmethod
    def _remote_root() -> Optional[str]:
        return os.getenv("SKYN3T_SSH_REMOTE_ROOT") or None

    def _build_cmd(self, handle: str) -> List[str]:
        """Construct the ssh argv. Separate so tests can assert the shape
        without touching the network."""
        host = self._host() or ""
        target = f"{self._user()}@{host}" if self._user() else host
        cmd: List[str] = [
            "ssh",
            "-p", str(self._port()),
            # Non-interactive, fail fast, never block on a host-key prompt
            # in an automated context.
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        key = self._key()
        if key:
            cmd.extend(["-i", key])
        cmd.append(target)
        # Remote command: optionally cd into the project root, then run the
        # in-tree child entrypoint reading one JSON line off stdin.
        runner = f"{self._remote_python()} -m skyn3t.intelligence.subagent_runner"
        remote_root = self._remote_root()
        cmd.append(f"cd {shlex.quote(remote_root)} && {runner}" if remote_root else runner)
        return cmd

    # -- transport ------------------------------------------------------

    async def _run_remote(
        self, payload: Dict[str, Any], *, handle: str, started: float
    ) -> BackendResult:
        cmd = self._build_cmd(handle)
        wire = json.dumps({**payload, "_subagent_id": handle})
        timeout = float(payload.get("timeout_seconds") or self.timeout_seconds)

        loop = asyncio.get_event_loop()
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ),
        )
        try:
            stdout_b, stderr_b = await loop.run_in_executor(
                None,
                lambda: proc.communicate(
                    input=wire.encode("utf-8") + b"\n",
                    timeout=timeout,
                ),
            )
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                stdout_b, stderr_b = proc.communicate(timeout=2)
            except Exception:
                stdout_b, stderr_b = b"", b""
            return self.timeout_result(
                handle=handle, started=started, stdout_b=stdout_b, stderr_b=stderr_b
            )

        return self.parse_result(
            handle=handle,
            started=started,
            stdout_b=stdout_b,
            stderr_b=stderr_b,
            returncode=proc.returncode,
        )

    # SSH sessions are per-command and already gone; shutdown is the base
    # no-op (kept idempotent for protocol parity).
