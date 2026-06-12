"""Remote execution backends — protocol, base class, and registry.

SkyN3t's local execution path is ``SubagentRunner`` (a fresh Python
subprocess) and its Docker path is ``DockerSubagentRunner`` (a container).
This package generalizes that shape to *remote* backends — SSH hosts,
Modal functions, Daytona workspaces, e2b sandboxes — behind one small
protocol so the orchestrator can route a task to any of them without
branching the code that handles the result.

The boundary is deliberately the same as the existing runners:

  - One payload in  : ``{agent_class, task, timeout_seconds, ...}`` —
    identical to ``SubagentRunner.run`` / ``DockerSubagentRunner.run``.
  - One result out  : a ``BackendResult`` that *subclasses*
    ``SubagentResult`` so every piece of existing result-handling code
    (status branching, ``.to_dict()`` serialization, failure hints)
    keeps working unchanged. The only additions are ``backend`` (which
    adapter ran it), ``handle`` (the remote session/sandbox id), and
    ``hibernated`` (whether the remote session was suspended rather than
    torn down).

Three hard rules every adapter honors (mirroring ``docker_available``):

  1. ``available()`` is a **classmethod**, **pure**, and **never raises**.
     Missing creds, an unset opt-in flag, or an un-importable SDK all
     return ``False`` — they do not blow up the caller or the import.
  2. Optional SDKs are **lazy-imported inside methods**, never at module
     top level, so importing this package costs nothing and never fails
     because some cloud SDK isn't installed.
  3. ``run()`` always returns a structured ``BackendResult`` — including
     a ``status="unavailable"`` result when the backend can't run —
     rather than raising into the orchestrator.

Adapters register themselves into a module-level registry via
``register_backend``; consumers look them up with ``get_remote_backend``
and enumerate the usable ones with ``available_backends``.
"""

from __future__ import annotations

import abc
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Type, runtime_checkable

# Re-export the shared helpers/constants from subagent_runner so adapters
# import them from one place (and so result normalization stays identical
# to the local + docker runners).
from skyn3t.intelligence.subagent_runner import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    SubagentResult,
    _parse_last_json_line,
    _truncate,
)

logger = logging.getLogger("skyn3t.intelligence.backends")

# Valid backend status values. Mirrors SubagentResult's set
# ("ok"|"error"|"timeout"|"crashed") plus "unavailable" for the
# graceful-skip path when a backend's creds/SDK/flag are missing.
BACKEND_STATUSES = ("ok", "error", "timeout", "crashed", "unavailable")


# ── Result ────────────────────────────────────────────────────────────


@dataclass
class BackendResult(SubagentResult):
    """Result of one remote-backend invocation.

    Subclasses ``SubagentResult`` (status/output/error/stdout/stderr/
    returncode/duration_seconds/subagent_id) so existing result-handling
    code is reused unchanged, and adds three remote-specific fields:

      - ``backend``    : which adapter ran it ('ssh'|'modal'|'daytona'|'e2b')
      - ``handle``     : the remote sandbox/session id (for shutdown/reuse)
      - ``hibernated`` : True when the remote session was suspended rather
                         than destroyed (so it can be resumed cheaply).
    """

    backend: str = ""
    handle: str = ""
    hibernated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d["backend"] = self.backend
        d["handle"] = self.handle
        d["hibernated"] = self.hibernated
        return d


# ── Protocol ──────────────────────────────────────────────────────────


@runtime_checkable
class RemoteBackend(Protocol):
    """Structural interface every remote backend satisfies.

    ``runtime_checkable`` so callers can ``isinstance(obj, RemoteBackend)``
    duck-type a candidate. The concrete ABC ``BaseRemoteBackend`` provides
    shared helpers; adapters should subclass it rather than re-implementing
    the protocol from scratch.
    """

    name: str

    @classmethod
    def available(cls) -> bool:
        """True when this backend's creds/flag are present AND its SDK
        imports. Pure and non-raising — mirrors ``docker_available()``."""
        ...

    async def run(self, payload: Dict[str, Any]) -> BackendResult:
        """Run one task. Same payload contract as ``SubagentRunner.run``:
        ``{agent_class, task, timeout_seconds, ...}``. Returns a
        ``BackendResult`` whose ``status`` is one of ``BACKEND_STATUSES``;
        never raises into the caller."""
        ...

    async def shutdown(self) -> None:
        """Idempotent hibernate/teardown of the remote session."""
        ...


# ── Base class ────────────────────────────────────────────────────────


class BaseRemoteBackend(abc.ABC):
    """Shared scaffolding for concrete remote backends.

    Handles the boilerplate that's identical across SSH/Modal/Daytona/e2b:
    timeout/output budgets, a fresh per-run handle, result normalization
    from the child's "last JSON line" contract, and the graceful
    ``unavailable`` short-circuit. Adapters implement ``_run_remote`` (the
    transport) and override ``available``.
    """

    #: Stable id, e.g. 'ssh' | 'modal' | 'daytona' | 'e2b'. Overridden by
    #: each adapter.
    name: str = "base"

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)

    # -- availability ---------------------------------------------------

    @classmethod
    def available(cls) -> bool:
        """Default: unavailable. Concrete adapters override with a pure,
        non-raising creds/flag/import probe (mirror ``docker_available``).
        """
        return False

    # -- lifecycle ------------------------------------------------------

    async def run(self, payload: Dict[str, Any]) -> BackendResult:
        """Template method: guards availability, generates a handle, times
        the run, delegates the transport to ``_run_remote``, and converts
        any leaked exception into a structured ``crashed`` result so the
        orchestrator never has to wrap calls in try/except.
        """
        started = time.monotonic()
        handle = self.new_handle()
        if not type(self).available():
            return self.unavailable_result(
                handle=handle,
                started=started,
                reason=f"{self.name} backend unavailable (missing creds/flag/SDK)",
            )
        try:
            result = await self._run_remote(payload, handle=handle, started=started)
        except Exception as exc:  # transport blew up — never propagate
            logger.exception("%s backend: run failed", self.name)
            return BackendResult(
                status="crashed",
                error=f"{self.name} backend run failed: {exc}",
                duration_seconds=time.monotonic() - started,
                subagent_id=handle,
                backend=self.name,
                handle=handle,
            )
        # Stamp the backend identity on whatever the adapter returned.
        if not result.backend:
            result.backend = self.name
        if not result.handle:
            result.handle = handle
        return result

    @abc.abstractmethod
    async def _run_remote(
        self, payload: Dict[str, Any], *, handle: str, started: float
    ) -> BackendResult:
        """Transport-specific execution. Adapters implement this. Should
        run ``python -m skyn3t.intelligence.subagent_runner`` on the
        remote, feed ``payload`` as one JSON line, and parse the result
        via ``self.parse_result``."""
        raise NotImplementedError

    async def shutdown(self) -> None:
        """Idempotent by default (no remote session held). Adapters that
        keep a long-lived session override this to hibernate/teardown."""
        return None

    # -- helpers (shared by adapters) -----------------------------------

    @staticmethod
    def new_handle() -> str:
        """Fresh remote session id, same shape as SubagentRunner's."""
        return f"sub-{uuid.uuid4().hex[:8]}"

    def unavailable_result(
        self, *, handle: str, started: float, reason: str
    ) -> BackendResult:
        """The graceful-skip result: status='unavailable', never an error
        the orchestrator must special-case as a failure."""
        return BackendResult(
            status="unavailable",
            error=reason,
            duration_seconds=time.monotonic() - started,
            subagent_id=handle,
            backend=self.name,
            handle=handle,
        )

    def parse_result(
        self,
        *,
        handle: str,
        started: float,
        stdout_b: bytes,
        stderr_b: bytes,
        returncode: Optional[int],
        hibernated: bool = False,
    ) -> BackendResult:
        """Normalize the remote child's output into a BackendResult using
        the same "last JSON line is the structured result" contract as the
        local + docker runners. Reuses ``_truncate`` / ``_parse_last_json_line``.
        """
        elapsed = time.monotonic() - started
        stdout_text = _truncate(stdout_b, self.max_output_bytes)
        stderr_text = _truncate(stderr_b, self.max_output_bytes)
        result_obj = _parse_last_json_line(stdout_text)
        if returncode == 0 and isinstance(result_obj, dict):
            return BackendResult(
                status=str(result_obj.get("status") or "ok"),
                output=result_obj.get("output"),
                error=result_obj.get("error"),
                stdout=stdout_text,
                stderr=stderr_text,
                returncode=returncode,
                duration_seconds=elapsed,
                subagent_id=handle,
                backend=self.name,
                handle=handle,
                hibernated=hibernated,
            )
        return BackendResult(
            status="crashed",
            error=(
                f"{self.name} non-zero exit {returncode}"
                if returncode not in (0, None)
                else "no JSON result on stdout"
            ),
            stdout=stdout_text,
            stderr=stderr_text,
            returncode=returncode,
            duration_seconds=elapsed,
            subagent_id=handle,
            backend=self.name,
            handle=handle,
            hibernated=hibernated,
        )

    def timeout_result(
        self,
        *,
        handle: str,
        started: float,
        stdout_b: bytes = b"",
        stderr_b: bytes = b"",
    ) -> BackendResult:
        """Structured timeout result (the adapter has already killed the
        remote transport)."""
        return BackendResult(
            status="timeout",
            error=f"{self.name} backend exceeded timeout {self.timeout_seconds}s",
            stdout=_truncate(stdout_b, self.max_output_bytes),
            stderr=_truncate(stderr_b, self.max_output_bytes),
            duration_seconds=time.monotonic() - started,
            subagent_id=handle,
            backend=self.name,
            handle=handle,
        )


# ── Registry ──────────────────────────────────────────────────────────

# name -> RemoteBackend class. Populated lazily by the package __init__
# (each adapter import is guarded so a missing optional SDK can't break
# the package import). Lookups instantiate on demand.
_REGISTRY: Dict[str, Type[BaseRemoteBackend]] = {}


def register_backend(cls: Type[BaseRemoteBackend]) -> Type[BaseRemoteBackend]:
    """Register a backend class under its ``.name``. Usable as a decorator.

    Idempotent: re-registering the same name overwrites (last-wins) so
    operators can monkeypatch/replace an adapter in tests.
    """
    name = getattr(cls, "name", None)
    if not name or not isinstance(name, str):
        raise ValueError(f"backend {cls!r} must define a non-empty str 'name'")
    _REGISTRY[name] = cls
    logger.debug("registered remote backend %r -> %s", name, cls.__name__)
    return cls


def get_remote_backend(name: str) -> Optional[RemoteBackend]:
    """Look up a backend by name and return an *instance* if it's both
    registered and currently available. Returns ``None`` for unknown or
    unavailable backends — never raises.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        return None
    try:
        if not cls.available():
            return None
        return cls()  # type: ignore[abstract]
    except Exception:
        # available()/__init__ are supposed to be safe, but be defensive:
        # a broken adapter must not take down the lookup.
        logger.exception("get_remote_backend(%r): instantiation failed", name)
        return None


def available_backends() -> List[str]:
    """Names of all registered backends whose ``.available()`` is True.

    Pure and non-raising: a backend whose ``available()`` misbehaves is
    treated as unavailable rather than propagating the error.
    """
    names: List[str] = []
    for name, cls in _REGISTRY.items():
        try:
            if cls.available():
                names.append(name)
        except Exception:
            logger.debug("available_backends: %r.available() raised", name, exc_info=True)
            continue
    return sorted(names)


def registered_backends() -> List[str]:
    """All registered backend names regardless of availability (for
    diagnostics/status endpoints)."""
    return sorted(_REGISTRY.keys())
