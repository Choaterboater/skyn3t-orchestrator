"""Isolated-subprocess subagent execution.

Hermes spawns "isolated subagents" — child processes with their own
conversation, terminal, and Python RPC. The point: zero context-cost
parallelism. The parent's prompt budget isn't burned by the subagent's
internal scratch; the subagent fails in isolation and gets a fresh
process on retry; one subagent can crash without taking down the
swarm.

We get most of that with a small wrapper around ``subprocess.Popen``:

  1. The parent calls ``SubagentRunner.run(task)`` with a TaskRequest-
     shaped dict. The runner serializes it to JSON on the child's stdin.
  2. The child is ``python -m skyn3t.intelligence.subagent_runner``
     (this same module's ``main()``). It reads one JSON line, runs the
     task in a fresh asyncio loop, writes one JSON line of result, exits.
  3. The parent reads the result JSON and returns it. Errors are caught,
     timeouts kill the child, and the response always has a ``status``
     field so the caller can branch cleanly.

What this gives us that the in-process orchestrator doesn't:

  - **Isolation**: a hung CLI or runaway tool call can't wedge the
    parent's event loop. Kill the PID; carry on.
  - **Per-subagent env**: the parent can scrub or override env vars
    (different API keys, different model defaults, different cwd) for
    the child without touching its own process state.
  - **Resource caps**: setrlimit + timeout on the child are real
    OS-level guarantees; in-process limits are best-effort.
  - **Crash recovery**: subagent SIGSEGV is just a non-zero exit code.

Designed to be transport-only — the host wires up which agent the
child runs by setting the ``agent_class`` field in the task payload.
This module doesn't import any specific agent so it stays cheap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger("skyn3t.intelligence.subagent_runner")


# Default budget on the child process. Generous enough for a real
# scaffold/build attempt; tight enough that a runaway can't burn the
# host. Tune per-task via SubagentRunner(..., timeout_seconds=...).
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024  # 4 MiB stdout/stderr cap


@dataclass
class SubagentResult:
    """Structured result of one subagent invocation."""

    status: str             # "ok" | "error" | "timeout" | "crashed"
    output: Any = None      # the child's result.output verbatim
    error: Optional[str] = None
    stdout: str = ""        # captured but separate from output
    stderr: str = ""
    returncode: Optional[int] = None
    duration_seconds: float = 0.0
    subagent_id: str = field(default_factory=lambda: f"sub-{uuid.uuid4().hex[:8]}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
            "duration_seconds": self.duration_seconds,
            "subagent_id": self.subagent_id,
        }


class SubagentRunner:
    """Spawn a Python subprocess and run one task in it.

    Usage:

        runner = SubagentRunner()
        result = await runner.run({
            "agent_class": "skyn3t.agents.code_agent:CodeAgent",
            "task": {"task_type": "code_execution", "code": "print(1+1)"},
            "timeout_seconds": 30,
        })
        if result.status == "ok":
            ...
    """

    def __init__(
        self,
        *,
        python_bin: Optional[str] = None,
        cwd: Optional[Path] = None,
        env_overrides: Optional[Mapping[str, str]] = None,
        env_strip: Optional[List[str]] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        memory_limit_mb: Optional[int] = None,
    ):
        self.python_bin = python_bin or sys.executable
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.env_overrides = dict(env_overrides or {})
        self.env_strip = list(env_strip or [])
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)
        self.memory_limit_mb = memory_limit_mb

    def _build_env(self) -> Dict[str, str]:
        """Inherit the parent's env, strip the explicit deny-list, then
        apply per-subagent overrides. Used by both real and dry runs."""
        env = dict(os.environ)
        for key in self.env_strip:
            env.pop(key, None)
        env.update(self.env_overrides)
        return env

    def _preexec(self) -> None:  # pragma: no cover - runs in the child
        """Apply rlimits in the child before exec.

        Memory limit uses RLIMIT_AS so a runaway allocation gets killed
        rather than swap-thrashing the host. Wrapped in try/except so
        macOS (which doesn't always honor RLIMIT_AS) doesn't fail the
        whole spawn.
        """
        if self.memory_limit_mb:
            try:
                bytes_cap = int(self.memory_limit_mb) * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (bytes_cap, bytes_cap))
            except Exception:
                pass

    async def run(self, payload: Dict[str, Any]) -> SubagentResult:
        """Run a task in a fresh subprocess.

        Returns a SubagentResult with status set to:
          - "ok"      : child exited 0, valid JSON result on stdout
          - "error"   : child returned an error payload
          - "timeout" : we killed the child after timeout_seconds
          - "crashed" : child exited non-zero with no/garbage JSON
        """
        started = time.monotonic()
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        # Encode payload + a subagent_id so the child can echo it for
        # log correlation.
        wire = json.dumps({**payload, "_subagent_id": sub_id})

        cmd: List[str] = [
            self.python_bin, "-m", "skyn3t.intelligence.subagent_runner",
        ]
        logger.debug("SubagentRunner: spawning %s", shlex.join(cmd))

        loop = asyncio.get_event_loop()
        try:
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.Popen(
                    cmd,
                    cwd=str(self.cwd),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self._build_env(),
                    preexec_fn=self._preexec if os.name != "nt" else None,
                ),
            )
        except Exception as exc:
            logger.exception("SubagentRunner: spawn failed")
            return SubagentResult(
                status="crashed",
                error=f"spawn failed: {exc}",
                duration_seconds=time.monotonic() - started,
                subagent_id=sub_id,
            )

        try:
            stdout_b, stderr_b = await loop.run_in_executor(
                None,
                lambda: proc.communicate(
                    input=wire.encode("utf-8") + b"\n",
                    timeout=self.timeout_seconds,
                ),
            )
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            # Best-effort drain of the partial output so we have
            # something to surface in the failure_hint path.
            try:
                stdout_b, stderr_b = proc.communicate(timeout=2)
            except Exception:
                stdout_b, stderr_b = b"", b""
            return SubagentResult(
                status="timeout",
                error=f"subagent exceeded timeout {self.timeout_seconds}s",
                stdout=_truncate(stdout_b, self.max_output_bytes),
                stderr=_truncate(stderr_b, self.max_output_bytes),
                returncode=None,
                duration_seconds=time.monotonic() - started,
                subagent_id=sub_id,
            )
        except Exception as exc:
            return SubagentResult(
                status="crashed",
                error=f"communicate failed: {exc}",
                duration_seconds=time.monotonic() - started,
                subagent_id=sub_id,
            )

        elapsed = time.monotonic() - started
        stdout_text = _truncate(stdout_b, self.max_output_bytes)
        stderr_text = _truncate(stderr_b, self.max_output_bytes)
        rc = proc.returncode

        # The child writes one line of JSON to stdout containing the
        # result. Try to parse that out (it'll be the LAST non-empty line
        # — anything before is from print() noise we want to preserve in
        # stdout but not in the structured result).
        result_obj = _parse_last_json_line(stdout_text)
        if rc == 0 and isinstance(result_obj, dict):
            return SubagentResult(
                status=str(result_obj.get("status") or "ok"),
                output=result_obj.get("output"),
                error=result_obj.get("error"),
                stdout=stdout_text,
                stderr=stderr_text,
                returncode=rc,
                duration_seconds=elapsed,
                subagent_id=sub_id,
            )
        return SubagentResult(
            status="crashed",
            error=f"non-zero exit {rc}" if rc != 0 else "no JSON result on stdout",
            stdout=stdout_text,
            stderr=stderr_text,
            returncode=rc,
            duration_seconds=elapsed,
            subagent_id=sub_id,
        )


def _truncate(data: bytes, cap: int) -> str:
    """Decode + truncate at byte cap. Replace undecodable bytes so we
    never crash on a binary blob in the output stream."""
    text = data.decode("utf-8", errors="replace")
    if len(text) > cap:
        text = text[:cap] + "\n…[truncated]"
    return text


def _parse_last_json_line(text: str) -> Optional[Dict[str, Any]]:
    """Find the LAST line that parses as a JSON object and return it.

    We deliberately scan from the bottom because the child may have
    print()'d log noise before the final result line. The contract is
    "last JSON line is the structured result" — same shape Hermes uses
    for its RPC bridge.
    """
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


# ── Child-side entry point ────────────────────────────────────────────


async def _child_run(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one task inside the subprocess. Resolves the agent class
    via dotted path + class name, builds a TaskRequest, runs it.

    Returns the JSON-shaped result dict the parent unmarshals. This is
    the contract the parent's _parse_last_json_line keys off of.
    """
    agent_class_path = payload.get("agent_class")
    task_payload = payload.get("task") or {}
    if not agent_class_path:
        return {"status": "error", "error": "agent_class is required"}
    try:
        module_name, _, class_name = agent_class_path.partition(":")
        if not class_name:
            return {"status": "error", "error": f"agent_class must be 'module:ClassName', got {agent_class_path!r}"}
        import importlib
        mod = importlib.import_module(module_name)
        cls = getattr(mod, class_name)
    except Exception as exc:
        return {"status": "error", "error": f"could not import agent class: {exc}"}

    try:
        from skyn3t.core.agent import TaskRequest
        from skyn3t.core.events import EventBus
        bus = EventBus()
        agent = cls(event_bus=bus)
        if hasattr(agent, "initialize"):
            init = agent.initialize()
            if asyncio.iscoroutine(init):
                await init
        task = TaskRequest(
            title=task_payload.get("title", "subagent task"),
            description=task_payload.get("description", ""),
            input_data=task_payload,
        )
        result = await agent.execute(task)
    except Exception as exc:
        return {"status": "error", "error": f"agent execution failed: {exc}"}

    if getattr(result, "success", False):
        return {"status": "ok", "output": getattr(result, "output", None)}
    return {
        "status": "error",
        "output": getattr(result, "output", None),
        "error": getattr(result, "error", "unknown error"),
    }


def main() -> int:
    """Subagent process entry point.

    Reads one JSON line from stdin, runs it via _child_run, writes one
    JSON line to stdout, exits. Errors caught at every layer so the
    parent always gets a structured response on the wire.
    """
    try:
        raw = sys.stdin.readline()
        payload = json.loads(raw) if raw else {}
    except Exception as exc:
        sys.stdout.write(json.dumps({"status": "error", "error": f"bad input: {exc}"}) + "\n")
        return 0  # still exit 0 — error is in band
    try:
        result = asyncio.run(_child_run(payload))
    except Exception as exc:
        result = {"status": "error", "error": f"top-level failure: {exc}"}
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
