"""E2B sandbox execution backend.

Hermes parity: run a subagent in an E2B cloud sandbox. E2B gives a
fast-booting microVM; we run our one-JSON-line child contract
(``python -m skyn3t.intelligence.subagent_runner``) inside it, read the
result, then kill the sandbox.

Availability gate (the binding rule): ``E2B_API_KEY`` must be set AND the
e2b SDK must import. The SDK is NOT installed in the default venv, so
``available()`` returns ``False`` here and the base ``run()``
short-circuits to a ``BackendResult(status='unavailable')`` — never a
crash, never a block.

All e2b SDK imports are lazy (inside ``available()`` / ``_run_remote()`` /
``shutdown()``) so this module imports cleanly with no SDK present. We
probe both the modern ``e2b_code_interpreter`` and base ``e2b`` package
names so either install satisfies availability.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from skyn3t.intelligence.backends.base import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    BackendResult,
    BaseRemoteBackend,
    register_backend,
)


def _e2b_creds_present() -> bool:
    return bool(os.getenv("E2B_API_KEY"))


def _e2b_importable() -> bool:
    """Lazy probe for the optional e2b SDK; never raises. Accepts the base
    ``e2b`` package or the ``e2b_code_interpreter`` helper."""
    try:
        import importlib.util

        return (
            importlib.util.find_spec("e2b") is not None
            or importlib.util.find_spec("e2b_code_interpreter") is not None
        )
    except Exception:
        return False


def _import_e2b_sandbox():  # pragma: no cover - only when SDK installed
    """Return an E2B Sandbox class, preferring the code-interpreter one."""
    try:
        from e2b_code_interpreter import Sandbox  # type: ignore

        return Sandbox
    except Exception:
        from e2b import Sandbox  # type: ignore

        return Sandbox


@register_backend
class E2BBackend(BaseRemoteBackend):
    """Run a subagent in an E2B cloud sandbox.

    Availability gate: E2B_API_KEY set AND the e2b SDK importable. E2B
    sandboxes are ephemeral (killed after each run, not hibernating), so
    ``hibernated`` stays ``False``; ``shutdown()`` kills an idle sandbox.
    """

    name = "e2b"

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)
        self._sandbox: Any = None

    @classmethod
    def available(cls) -> bool:
        return _e2b_creds_present() and _e2b_importable()

    async def _run_remote(
        self, payload: Dict[str, Any], *, handle: str, started: float
    ) -> BackendResult:
        wire = json.dumps({**payload, "_subagent_id": handle})
        timeout = float(payload.get("timeout_seconds") or self.timeout_seconds)
        try:
            # Lazy import — only reached when creds+SDK are present.
            Sandbox = _import_e2b_sandbox()
            self._sandbox = Sandbox(timeout=int(timeout))
            cmd = (
                "printf %s "
                + _shquote(wire)
                + " | python -m skyn3t.intelligence.subagent_runner"
            )
            execution = self._sandbox.commands.run(cmd)
            stdout_b = _as_bytes(getattr(execution, "stdout", None) or "")
            stderr_b = _as_bytes(getattr(execution, "stderr", None) or "")
            rc = getattr(execution, "exit_code", 0)
        finally:
            await self.shutdown()

        return self.parse_result(
            handle=handle,
            started=started,
            stdout_b=stdout_b,
            stderr_b=stderr_b,
            returncode=rc,
            hibernated=False,
        )

    async def shutdown(self) -> None:
        """Kill the sandbox. Idempotent."""
        sandbox = self._sandbox
        self._sandbox = None
        if sandbox is None:
            return None
        try:
            kill = getattr(sandbox, "kill", None)
            if kill is not None:
                kill()
        except Exception:
            pass
        return None


def _shquote(s: str) -> str:
    """Minimal POSIX shell single-quote for embedding the JSON payload."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _as_bytes(data: Any) -> bytes:
    if isinstance(data, bytes):
        return data
    if data is None:
        return b""
    return str(data).encode("utf-8", errors="replace")
