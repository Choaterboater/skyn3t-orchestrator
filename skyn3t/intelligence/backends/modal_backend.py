"""Modal serverless execution backend.

Hermes parity: run a subagent in a Modal serverless sandbox. Modal spins
a container on demand, runs our one-JSON-line child contract
(``python -m skyn3t.intelligence.subagent_runner``), returns the result,
then the sandbox is torn down / hibernated so we pay nothing while idle.

Availability gate (the binding rule): both ``MODAL_TOKEN_ID`` and
``MODAL_TOKEN_SECRET`` must be set AND the ``modal`` SDK must import. The
SDK is NOT installed in the default venv, so ``available()`` returns
``False`` here and the base ``run()`` short-circuits to a
``BackendResult(status='unavailable')`` — never a crash, never a block.

All ``modal`` imports are lazy (inside ``available()`` / ``_run_remote()``
/ ``shutdown()``) so this module imports cleanly with no SDK present.
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


def _modal_creds_present() -> bool:
    return bool(os.getenv("MODAL_TOKEN_ID") and os.getenv("MODAL_TOKEN_SECRET"))


def _modal_importable() -> bool:
    """Lazy probe for the optional modal SDK; never raises."""
    try:
        import importlib.util

        return importlib.util.find_spec("modal") is not None
    except Exception:
        return False


@register_backend
class ModalBackend(BaseRemoteBackend):
    """Run a subagent in a Modal serverless sandbox; hibernate when idle.

    Availability gate: MODAL_TOKEN_ID + MODAL_TOKEN_SECRET set AND the
    modal SDK importable. Serverless => the sandbox is torn down after each
    run and the result is flagged ``hibernated=True``; ``shutdown()``
    hibernates an idle session.
    """

    name = "modal"

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
        return _modal_creds_present() and _modal_importable()

    async def _run_remote(
        self, payload: Dict[str, Any], *, handle: str, started: float
    ) -> BackendResult:
        wire = json.dumps({**payload, "_subagent_id": handle})
        timeout = float(payload.get("timeout_seconds") or self.timeout_seconds)
        try:
            # Lazy import — only reached when creds+SDK are present.
            import modal  # type: ignore

            app = modal.App.lookup("skyn3t-subagent", create_if_missing=True)
            image = modal.Image.debian_slim().pip_install("skyn3t")
            self._sandbox = modal.Sandbox.create(app=app, image=image, timeout=int(timeout))
            proc = self._sandbox.exec(
                "python", "-m", "skyn3t.intelligence.subagent_runner",
            )
            proc.stdin.write(wire.encode("utf-8") + b"\n")
            proc.stdin.write_eof()
            proc.wait()
            stdout_b = _as_bytes(proc.stdout.read())
            stderr_b = _as_bytes(proc.stderr.read())
            rc = getattr(proc, "returncode", 0)
        finally:
            # Serverless: tear the sandbox down + hibernate after the run,
            # even on failure (the base run() converts a raise to crashed).
            await self.shutdown()

        return self.parse_result(
            handle=handle,
            started=started,
            stdout_b=stdout_b,
            stderr_b=stderr_b,
            returncode=rc,
            hibernated=True,
        )

    async def shutdown(self) -> None:
        """Hibernate / tear down the idle sandbox. Idempotent."""
        sandbox = self._sandbox
        self._sandbox = None
        if sandbox is None:
            return None
        try:
            terminate = getattr(sandbox, "terminate", None)
            if terminate is not None:
                terminate()
        except Exception:
            pass
        return None


def _as_bytes(data: Any) -> bytes:
    if isinstance(data, bytes):
        return data
    if data is None:
        return b""
    return str(data).encode("utf-8", errors="replace")
