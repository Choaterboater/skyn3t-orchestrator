"""Daytona serverless execution backend.

Hermes parity: run a subagent in a Daytona dev-sandbox. Daytona
provisions a sandbox on demand, runs our one-JSON-line child contract
(``python -m skyn3t.intelligence.subagent_runner``), returns the result,
then the sandbox is stopped / hibernated so it costs nothing idle.

Availability gate (the binding rule): ``DAYTONA_API_KEY`` must be set AND
the Daytona SDK must import. The SDK is NOT installed in the default venv,
so ``available()`` returns ``False`` here and the base ``run()``
short-circuits to a ``BackendResult(status='unavailable')`` — never a
crash, never a block.

All Daytona SDK imports are lazy (inside ``available()`` /
``_run_remote()`` / ``shutdown()``) so this module imports cleanly with no
SDK present. We probe both the modern ``daytona`` package and the legacy
``daytona_sdk`` name so either install satisfies availability.
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


def _daytona_creds_present() -> bool:
    return bool(os.getenv("DAYTONA_API_KEY"))


def _daytona_importable() -> bool:
    """Lazy probe for the optional Daytona SDK; never raises. Accepts
    either the ``daytona`` or legacy ``daytona_sdk`` package name."""
    try:
        import importlib.util

        return (
            importlib.util.find_spec("daytona") is not None
            or importlib.util.find_spec("daytona_sdk") is not None
        )
    except Exception:
        return False


def _import_daytona():  # pragma: no cover - only when SDK installed
    """Return the importable Daytona module, preferring the modern name."""
    try:
        import daytona  # type: ignore

        return daytona
    except Exception:
        import daytona_sdk  # type: ignore

        return daytona_sdk


@register_backend
class DaytonaBackend(BaseRemoteBackend):
    """Run a subagent in a Daytona sandbox; hibernate when idle.

    Availability gate: DAYTONA_API_KEY set AND the Daytona SDK importable.
    Serverless => the sandbox is stopped after each run and the result is
    flagged ``hibernated=True``; ``shutdown()`` hibernates an idle session.
    """

    name = "daytona"

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)
        self._sandbox: Any = None
        self._client: Any = None

    @classmethod
    def available(cls) -> bool:
        return _daytona_creds_present() and _daytona_importable()

    async def _run_remote(
        self, payload: Dict[str, Any], *, handle: str, started: float
    ) -> BackendResult:
        wire = json.dumps({**payload, "_subagent_id": handle})
        try:
            # Lazy import — only reached when creds+SDK are present.
            mod = _import_daytona()
            Daytona = getattr(mod, "Daytona")
            self._client = Daytona()
            self._sandbox = self._client.create()
            # Run the child entrypoint, piping the JSON payload via stdin
            # so the contract stays one-line-in / one-line-out.
            cmd = (
                "printf %s "
                + _shquote(wire)
                + " | python -m skyn3t.intelligence.subagent_runner"
            )
            resp = self._sandbox.process.exec(cmd)
            stdout_b = _as_bytes(
                getattr(resp, "result", None) or getattr(resp, "stdout", None) or ""
            )
            rc = getattr(resp, "exit_code", 0)
        finally:
            await self.shutdown()

        return self.parse_result(
            handle=handle,
            started=started,
            stdout_b=stdout_b,
            stderr_b=b"",
            returncode=rc,
            hibernated=True,
        )

    async def shutdown(self) -> None:
        """Stop / hibernate the idle sandbox. Idempotent."""
        sandbox = self._sandbox
        client = self._client
        self._sandbox = None
        self._client = None
        if sandbox is None:
            return None
        try:
            # Prefer an explicit stop (hibernate); fall back to remove.
            if client is not None and hasattr(client, "stop"):
                client.stop(sandbox)
            elif hasattr(sandbox, "stop"):
                sandbox.stop()
            elif client is not None and hasattr(client, "remove"):
                client.remove(sandbox)
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
