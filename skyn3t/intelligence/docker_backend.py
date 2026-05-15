"""Docker execution backend — run a subagent inside a container.

Hermes ships 5 execution backends (local, Docker, SSH, Singularity,
Modal). We already have ``SubagentRunner`` for the local case (a fresh
Python subprocess); this module adds Docker on top of the same shape:
spawn a container, feed it the task payload on stdin, read the result
JSON off stdout, kill the container if it overruns its timeout.

Design notes:

  - The container runs ``python -m skyn3t.intelligence.subagent_runner``
    — the same in-tree child entrypoint used by SubagentRunner. So the
    contract is unchanged: one JSON line in, one JSON line out.
  - The default image is ``python:3.11-slim`` because SkyN3t's runtime
    requires Python 3.10+ and slim keeps spawn fast. Operators can
    override with any image that has Python and the project mounted.
  - The project source is bind-mounted read-only at /skyn3t inside the
    container; the container CWD is set so ``python -m`` resolves
    correctly.
  - We don't pull images automatically — the operator runs
    ``docker pull <image>`` once. If the image isn't local, ``docker
    run`` returns a clear error which we surface in the result.

What this does NOT do (yet):
  - GPU passthrough
  - Network policy beyond default bridge mode
  - Multi-container compose graphs
  - Pre-built skyn3t image with deps baked in (operators currently
    need to install dependencies inside the container)

Those are layered concerns and easy to add — the runner shape gives
us the right boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.intelligence.subagent_runner import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    SubagentResult,
    _parse_last_json_line,
    _truncate,
)

logger = logging.getLogger("skyn3t.intelligence.docker_backend")


# Reasonable default for SkyN3t — slim Python image, ~50 MB, has pip
# and the basics. Operators override via constructor kwarg or env.
DEFAULT_DOCKER_IMAGE = os.environ.get("SKYN3T_SUBAGENT_DOCKER_IMAGE", "python:3.11-slim")

# Path inside the container where we bind-mount the project source.
CONTAINER_SOURCE_MOUNT = "/skyn3t"


def docker_available() -> bool:
    """Cheap check: is the docker CLI on PATH? We don't call ``docker
    info`` (slow + requires daemon access) — the spawn path itself will
    fail cleanly when the daemon isn't running."""
    return shutil.which("docker") is not None


@dataclass
class DockerRunResult(SubagentResult):
    """Same shape as SubagentResult but with image info added."""

    image: str = ""
    container_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d["image"] = self.image
        d["container_id"] = self.container_id
        return d


class DockerSubagentRunner:
    """Drop-in replacement for SubagentRunner that runs in a container.

    Same API surface (``await runner.run(payload)`` → DockerRunResult)
    so callers can route a task to either backend without branching the
    code that handles the result.

    The container is run with:
      - ``--rm``                  : auto-delete after exit
      - ``-i``                    : keep stdin open for the JSON payload
      - ``--network``             : configurable, defaults to bridge
      - ``--memory``              : optional cap from constructor
      - ``--cpus``                : optional cap from constructor
      - ``-v <project>:/skyn3t``  : bind-mount the project source read-only
      - ``-w /skyn3t``            : set workdir so ``python -m`` resolves
      - ``-e SKYN3T_*``           : forward selected env vars
    """

    def __init__(
        self,
        *,
        image: str = DEFAULT_DOCKER_IMAGE,
        project_root: Optional[Path] = None,
        network: str = "bridge",
        memory_limit_mb: Optional[int] = None,
        cpu_limit: Optional[float] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        env_passthrough: Optional[List[str]] = None,
        env_overrides: Optional[Dict[str, str]] = None,
        extra_run_args: Optional[List[str]] = None,
    ):
        self.image = image
        self.project_root = (
            project_root.resolve() if project_root else Path(__file__).resolve().parents[2]
        )
        self.network = network
        self.memory_limit_mb = memory_limit_mb
        self.cpu_limit = cpu_limit
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)
        # By default only forward SKYN3T_* env vars so we don't leak
        # OPENAI_API_KEY / ANTHROPIC_API_KEY / GH tokens into a container.
        # Operators can opt them in explicitly via env_passthrough.
        self.env_passthrough = list(env_passthrough or [])
        self.env_overrides = dict(env_overrides or {})
        self.extra_run_args = list(extra_run_args or [])

    # ------------------------------------------------------------------
    # Public entry point.
    # ------------------------------------------------------------------

    async def run(self, payload: Dict[str, Any]) -> DockerRunResult:
        """Run one task in a Docker container. Same payload shape as
        SubagentRunner.run.
        """
        started = time.monotonic()
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        if not docker_available():
            return DockerRunResult(
                status="crashed",
                error="docker CLI not on PATH",
                duration_seconds=time.monotonic() - started,
                subagent_id=sub_id,
                image=self.image,
            )
        cmd = self._build_cmd(sub_id)
        wire = json.dumps({**payload, "_subagent_id": sub_id})
        logger.debug("DockerSubagentRunner: spawning %s", shlex.join(cmd))

        loop = asyncio.get_event_loop()
        try:
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ),
            )
        except Exception as exc:
            return DockerRunResult(
                status="crashed",
                error=f"docker spawn failed: {exc}",
                duration_seconds=time.monotonic() - started,
                subagent_id=sub_id,
                image=self.image,
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
            # docker run honors SIGKILL → container goes away.
            try:
                proc.kill()
            except Exception:
                pass
            try:
                stdout_b, stderr_b = proc.communicate(timeout=2)
            except Exception:
                stdout_b, stderr_b = b"", b""
            return DockerRunResult(
                status="timeout",
                error=f"docker subagent exceeded timeout {self.timeout_seconds}s",
                stdout=_truncate(stdout_b, self.max_output_bytes),
                stderr=_truncate(stderr_b, self.max_output_bytes),
                duration_seconds=time.monotonic() - started,
                subagent_id=sub_id,
                image=self.image,
                container_id=sub_id,
            )
        except Exception as exc:
            return DockerRunResult(
                status="crashed",
                error=f"docker communicate failed: {exc}",
                duration_seconds=time.monotonic() - started,
                subagent_id=sub_id,
                image=self.image,
            )

        elapsed = time.monotonic() - started
        stdout_text = _truncate(stdout_b, self.max_output_bytes)
        stderr_text = _truncate(stderr_b, self.max_output_bytes)
        rc = proc.returncode
        result_obj = _parse_last_json_line(stdout_text)
        if rc == 0 and isinstance(result_obj, dict):
            return DockerRunResult(
                status=str(result_obj.get("status") or "ok"),
                output=result_obj.get("output"),
                error=result_obj.get("error"),
                stdout=stdout_text,
                stderr=stderr_text,
                returncode=rc,
                duration_seconds=elapsed,
                subagent_id=sub_id,
                image=self.image,
                container_id=sub_id,
            )
        return DockerRunResult(
            status="crashed",
            error=f"docker non-zero exit {rc}" if rc != 0 else "no JSON result on stdout",
            stdout=stdout_text,
            stderr=stderr_text,
            returncode=rc,
            duration_seconds=elapsed,
            subagent_id=sub_id,
            image=self.image,
            container_id=sub_id,
        )

    # ------------------------------------------------------------------
    # Command construction (separate so tests can assert the shape).
    # ------------------------------------------------------------------

    def _build_cmd(self, sub_id: str) -> List[str]:
        cmd: List[str] = [
            "docker", "run", "--rm", "-i",
            "--name", f"skyn3t-{sub_id}",
            "--network", self.network,
            "-v", f"{self.project_root}:{CONTAINER_SOURCE_MOUNT}:ro",
            "-w", CONTAINER_SOURCE_MOUNT,
        ]
        if self.memory_limit_mb:
            cmd.extend(["--memory", f"{int(self.memory_limit_mb)}m"])
        if self.cpu_limit:
            cmd.extend(["--cpus", str(float(self.cpu_limit))])
        for key in self.env_passthrough:
            val = os.environ.get(key)
            if val is not None:
                cmd.extend(["-e", f"{key}={val}"])
        for key, val in self.env_overrides.items():
            cmd.extend(["-e", f"{key}={val}"])
        cmd.extend(self.extra_run_args)
        cmd.extend([
            self.image,
            "python", "-m", "skyn3t.intelligence.subagent_runner",
        ])
        return cmd
