"""BootVerifierAgent — does the scaffold actually BOOT?

BuildVerifierAgent answers "do these files parse?" — `node --check`,
`python -m py_compile`, etc. That's a syntax gate. It says nothing
about whether the files **agree with each other** at runtime.

Real failures we've shipped past BuildVerifier and reviewer:

  - server/index.js uses CJS `require()` but adapters use ESM
    `export default` (both individually valid; mutually broken)
  - .env.example contains the CLI's tool-call narration at the top
    (server crashes parsing it as env vars)
  - dotenv looks in `server/` but `.env.example` ships in
    `scaffold/` (server boots with no config)
  - Frontend reads `CORS_ORIGIN` (singular); server reads
    `CORS_ORIGINS` (plural). 403 on every request.

Every one of those is "file A says one thing, file B says another."
A reviewer can't catch them because they look fine in isolation.
A `node --check` can't catch them because both files parse.
The only thing that catches them is **actually starting the program**.

That's this agent. It:

  1. Detects the entry point (Node Express server, Python FastAPI,
     Flask, etc.) from package.json / pyproject / etc.
  2. Installs deps with strict timeout (`npm install --silent`).
  3. Synthesizes a minimal `.env` from `.env.example` placeholders so
     missing-env-var crashes don't get misread as code bugs.
  4. Boots the server in a subprocess with a hard timeout, watches
     for "listening on port N" log lines, captures crashes.
  5. Curls `http://localhost:N/api/health` (or `/health`, or `/`) to
     confirm the server actually serves a request.
  6. Kills the subprocess, returns a structured verdict.

Output schema mirrors BuildVerifier so the runner's fix-loop and
auto-retry hooks slot in unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.boot_verifier")

# Hard limits — each phase has its own ceiling. Tuned so a hung
# `npm install` or a crashloop can't wedge the pipeline.
DEFAULT_INSTALL_TIMEOUT = 240   # 4 min for npm install
DEFAULT_BOOT_TIMEOUT = 45       # 45s for the server to start listening
DEFAULT_HEALTH_TIMEOUT = 10     # 10s for health-check curl to return
DEFAULT_TOTAL_TIMEOUT = 360     # 6 min absolute ceiling for the whole flow

# Health endpoints to try, in order. The first one that returns 200
# (or any 2xx/3xx) is good enough — we're checking the server lives,
# not testing every route.
HEALTH_ENDPOINTS: Tuple[str, ...] = ("/api/health", "/health", "/healthz", "/")


@dataclass
class BootProbe:
    """What we figured out about how to boot this scaffold."""

    kind: str             # 'node-express' | 'python-fastapi' | 'python-flask' | 'unknown'
    entry: str            # relative path to the entry file (server/index.js, app.py, ...)
    install_cmd: Optional[List[str]]  # ['npm','install','--silent'] etc.; None when no install needed
    boot_cmd: List[str]   # ['node', 'server/index.js'] etc.
    cwd: str              # subdirectory to run from (e.g. 'server' or '.')
    port: int             # port we expect the server to bind
    env_file: Optional[str]  # path to a usable .env (we may synthesize one)
    notes: List[str]


class BootVerifierAgent(BaseAgent):
    """Runs the scaffold and reports whether it BOOTS.

    Output schema (mirrors BuildVerifierAgent):

        {
          "verdict":     "yes" | "no" | "skipped",
          "kind":        "node-express" | ...,
          "command":     "the boot command we ran",
          "port":        3100,
          "health_url":  "http://127.0.0.1:3100/api/health",
          "http_status": 200,   # actual response code if reached
          "stdout":      "last 4000 chars of server stdout",
          "stderr":      "last 4000 chars of server stderr",
          "summary":     "human-readable one-liner",
          "scaffold_dir": "...",
          "failure_hint": "concrete cross-file mismatch description"
        }
    """

    def __init__(
        self,
        name: str = "boot_verifier",
        *,
        event_bus: Optional[EventBus] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="verifier",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(AgentCapability(
            name="boot_verification",
            description="Actually starts the scaffold and confirms it serves a request.",
            parameters={"scaffold_dir": "str"},
        ))
        cfg = config or {}
        self.install_timeout = int(cfg.get("install_timeout", DEFAULT_INSTALL_TIMEOUT))
        self.boot_timeout = int(cfg.get("boot_timeout", DEFAULT_BOOT_TIMEOUT))
        self.health_timeout = int(cfg.get("health_timeout", DEFAULT_HEALTH_TIMEOUT))
        self.total_timeout = int(cfg.get("total_timeout", DEFAULT_TOTAL_TIMEOUT))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        data = task.input_data or {}
        scaffold_dir_raw = (
            data.get("scaffold_dir")
            or (str(Path(data.get("artifact_dir", "")) / "scaffold")
                if data.get("artifact_dir") else None)
        )
        if not scaffold_dir_raw:
            return TaskResult(
                task_id=task.task_id, success=False,
                error="scaffold_dir required",
            )
        scaffold_dir = Path(scaffold_dir_raw).expanduser().resolve()
        if not scaffold_dir.exists() or not scaffold_dir.is_dir():
            return TaskResult(
                task_id=task.task_id, success=False,
                error=f"scaffold_dir does not exist: {scaffold_dir}",
            )

        probe = self._detect_boot(scaffold_dir)
        await self.think(
            f"boot probe: kind={probe.kind} entry={probe.entry} "
            f"port={probe.port} cwd={probe.cwd}"
        )

        if probe.kind == "unknown":
            return TaskResult(
                task_id=task.task_id, success=True,
                output={
                    "verdict": "skipped",
                    "kind": "unknown",
                    "command": None,
                    "port": 0,
                    "health_url": "",
                    "http_status": 0,
                    "stdout": "",
                    "stderr": "",
                    "summary": (
                        "Boot verifier: no recognized server entry point "
                        "(no Express server, no FastAPI/Flask app). Static "
                        "frontend or CLI tool — no boot check needed."
                    ),
                    "scaffold_dir": str(scaffold_dir),
                    "failure_hint": None,
                },
            )

        # Synthesize .env if needed BEFORE install (some pkg scripts
        # read env at install time).
        self._ensure_runnable_env(scaffold_dir, probe)

        start = time.monotonic()
        # Step 1: install deps.
        if probe.install_cmd:
            install_cwd = scaffold_dir / probe.cwd
            install_ok, install_out, install_err = await self._run_with_timeout(
                probe.install_cmd, install_cwd, self.install_timeout,
                env=os.environ.copy(),
            )
            install_log = (install_err or "") + "\n" + (install_out or "")
            if not install_ok:
                relaxed_cmd = self._relaxed_install_cmd(probe.install_cmd)
                if relaxed_cmd is not None:
                    relaxed_ok, relaxed_out, relaxed_err = await self._run_with_timeout(
                        relaxed_cmd, install_cwd, self.install_timeout,
                        env=os.environ.copy(),
                    )
                    install_ok = relaxed_ok
                    install_out = relaxed_out
                    install_err = relaxed_err
                    install_log = (install_err or "") + "\n" + (install_out or "")
            if not install_ok:
                summary = f"npm install failed in {probe.cwd}/"
                return TaskResult(
                    task_id=task.task_id, success=True,
                    output=self._fail_output(
                        probe, scaffold_dir,
                        command=" ".join(probe.install_cmd),
                        stdout=install_out, stderr=install_err,
                        summary=summary,
                        failure_hint=self._diagnose_install_failure(install_log),
                    ),
                )

        if time.monotonic() - start > self.total_timeout:
            return TaskResult(
                task_id=task.task_id, success=True,
                output=self._fail_output(
                    probe, scaffold_dir,
                    command=" ".join(probe.install_cmd or []),
                    stdout="", stderr="install phase consumed the total timeout",
                    summary="boot verifier: install phase exceeded total budget",
                    failure_hint="The install step is too slow. Reduce dependencies.",
                ),
            )

        # Step 2: pick a free port if the default is taken (someone may be
        # running v15 or another scaffold locally).
        actual_port = self._free_port(probe.port)
        if actual_port != probe.port:
            await self.think(
                f"port {probe.port} busy; rebinding to {actual_port} for boot test"
            )

        # Step 3: boot the server, wait for it to listen.
        boot_env = self._build_boot_env(scaffold_dir, probe, actual_port)
        boot_cwd = scaffold_dir / probe.cwd
        boot_ok, server_proc, boot_out, boot_err = await self._boot_and_wait(
            probe.boot_cmd, boot_cwd, boot_env, actual_port,
        )

        if not boot_ok:
            # Server failed to start. Capture diagnostics and bail.
            stdout = boot_out
            stderr = boot_err
            return TaskResult(
                task_id=task.task_id, success=True,
                output=self._fail_output(
                    probe, scaffold_dir,
                    command=" ".join(probe.boot_cmd),
                    stdout=stdout, stderr=stderr,
                    summary=f"server failed to start within {self.boot_timeout}s",
                    failure_hint=self._diagnose_boot_failure(stderr, scaffold_dir, probe),
                ),
            )

        # Step 4: health-check.
        health_status, health_url = await self._health_check(actual_port)

        # Step 5: kill the server.
        await self._kill_proc(server_proc)
        # Grab any final output the process emitted between health-check
        # and kill so the log tail is complete.
        try:
            tail_out, tail_err = await asyncio.wait_for(
                server_proc.communicate() if server_proc is not None else asyncio.sleep(0, result=(b"", b"")),
                timeout=2.0,
            )
            if tail_out:
                boot_out += tail_out.decode(errors="replace")
            if tail_err:
                boot_err += tail_err.decode(errors="replace")
        except Exception:
            pass

        if health_status and 200 <= health_status < 400:
            summary = (
                f"server booted and {health_url} returned HTTP {health_status} "
                f"(stack: {probe.kind})"
            )
            try:
                await self.share_learning(
                    f"boot_verifier: {probe.kind} → yes ({health_status})",
                    scope="build",
                )
            except Exception:
                logger.debug("share_learning(boot_verifier) failed", exc_info=True)
            return TaskResult(
                task_id=task.task_id, success=True,
                output={
                    "verdict": "yes",
                    "kind": probe.kind,
                    "command": " ".join(probe.boot_cmd),
                    "port": actual_port,
                    "health_url": health_url,
                    "http_status": health_status,
                    "stdout": boot_out[-4000:],
                    "stderr": boot_err[-4000:],
                    "summary": summary,
                    "scaffold_dir": str(scaffold_dir),
                    "failure_hint": None,
                },
            )

        # Server booted but health-check failed.
        return TaskResult(
            task_id=task.task_id, success=True,
            output=self._fail_output(
                probe, scaffold_dir,
                command=" ".join(probe.boot_cmd),
                stdout=boot_out, stderr=boot_err,
                summary=(
                    f"server booted but no health endpoint responded "
                    f"(tried {', '.join(HEALTH_ENDPOINTS)} on port {actual_port})"
                ),
                failure_hint=(
                    f"Server process started but didn't respond to any of "
                    f"{', '.join(HEALTH_ENDPOINTS)}. Add a GET /api/health route "
                    f"returning JSON {{ok: true}}, or check CORS / route "
                    f"prefix mismatches between the frontend's expected paths "
                    f"and what the server actually mounts."
                ),
            ),
        )

    # ── stack detection ───────────────────────────────────────────

    def _detect_boot(self, scaffold_dir: Path) -> BootProbe:
        """Identify how to boot whatever's in `scaffold_dir`.

        Order matters: we check for a server/ subdir FIRST since
        full-stack scaffolds (frontend + Node proxy) put the server
        there and the top-level package.json is just Vite.
        """
        notes: List[str] = []
        # Case 1: scaffold/server/index.js + scaffold/server/package.json
        # (the shape augmentation produces for extensible dashboards).
        server_dir = scaffold_dir / "server"
        server_pkg = server_dir / "package.json"
        server_entry_candidates = [
            "index.js", "index.mjs", "index.cjs",
            "server.js", "app.js", "main.js",
        ]
        if server_dir.is_dir() and server_pkg.is_file():
            for entry in server_entry_candidates:
                p = server_dir / entry
                if p.is_file():
                    notes.append(f"using server/{entry}")
                    return BootProbe(
                        kind="node-express",
                        entry=f"server/{entry}",
                        install_cmd=["npm", "install", "--silent",
                                     "--no-audit", "--no-fund",
                                     "--prefer-offline"],
                        boot_cmd=["node", entry],
                        cwd="server",
                        port=self._guess_port_from_files(server_dir) or 3100,
                        env_file=None,
                        notes=notes,
                    )

        # Case 2: top-level Node server (no server/ subdir).
        top_pkg = scaffold_dir / "package.json"
        if top_pkg.is_file():
            try:
                pkg = json.loads(top_pkg.read_text(encoding="utf-8"))
            except Exception:
                pkg = {}
            deps = ((pkg.get("dependencies") or {})
                    if isinstance(pkg, dict) else {})
            # Heuristic: presence of express/fastify deps signals a
            # server-style top-level package.json (not just a Vite SPA).
            is_server = any(
                k in deps for k in ("express", "fastify", "koa", "hapi", "polka")
            )
            if is_server:
                for entry in server_entry_candidates:
                    p = scaffold_dir / entry
                    if p.is_file():
                        notes.append(f"using top-level {entry}")
                        return BootProbe(
                            kind="node-express",
                            entry=entry,
                            install_cmd=["npm", "install", "--silent",
                                         "--no-audit", "--no-fund",
                                         "--prefer-offline"],
                            boot_cmd=["node", entry],
                            cwd=".",
                            port=self._guess_port_from_files(scaffold_dir) or 3100,
                            env_file=None,
                            notes=notes,
                        )

        # Case 3: Python FastAPI/Flask.
        for py_entry, kind, port in (
            ("src/main.py", "python-fastapi", 8000),
            ("main.py", "python-fastapi", 8000),
            ("app.py", "python-flask", 5000),
        ):
            p = scaffold_dir / py_entry
            if p.is_file():
                notes.append(f"using python {py_entry}")
                # For FastAPI we need uvicorn. For Flask we run app.py
                # directly (assuming app.run()) or via `flask run`.
                if kind == "python-fastapi":
                    module = py_entry.replace("/", ".").rsplit(".py", 1)[0]
                    boot = ["python", "-m", "uvicorn",
                            f"{module}:app", "--host", "127.0.0.1",
                            "--port", str(port)]
                else:
                    boot = ["python", py_entry]
                return BootProbe(
                    kind=kind,
                    entry=py_entry,
                    install_cmd=(
                        ["pip", "install", "-q", "-r", "requirements.txt"]
                        if (scaffold_dir / "requirements.txt").is_file()
                        else None
                    ),
                    boot_cmd=boot,
                    cwd=".",
                    port=port,
                    env_file=None,
                    notes=notes,
                )

        notes.append("no server entry point detected")
        return BootProbe(
            kind="unknown", entry="",
            install_cmd=None, boot_cmd=[], cwd=".",
            port=0, env_file=None, notes=notes,
        )

    def _guess_port_from_files(self, root: Path) -> Optional[int]:
        """Skim the server's main file + .env.example for a PORT default."""
        candidates: List[Path] = []
        for name in ("index.js", "server.js", "app.js", "index.mjs"):
            p = root / name
            if p.is_file():
                candidates.append(p)
        candidates.extend([
            root / ".env.example",
            root.parent / ".env.example",
        ])
        import re as _re
        port_re = _re.compile(
            r"(?:PORT\s*[:=]\s*['\"]?(\d{4,5})|listen\s*\(\s*(\d{4,5}))",
            _re.IGNORECASE,
        )
        for p in candidates:
            if not p.is_file():
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for m in port_re.finditer(txt):
                port_str = m.group(1) or m.group(2)
                if not port_str:
                    continue
                try:
                    port = int(port_str)
                    if 1024 < port < 65536:
                        return port
                except Exception:
                    continue
        return None

    # ── env handling ──────────────────────────────────────────────

    def _ensure_runnable_env(self, scaffold_dir: Path, probe: BootProbe) -> None:
        """Make sure the scaffold has a `.env` the server can read.

        Two cases the v15 boot bug class:
          (a) `.env.example` exists at scaffold root but server runs from
              scaffold/server/ where dotenv looks. Copy to where the
              server actually expects it.
          (b) `.env.example` lines have `replace_with_*` placeholders
              that crash strict validators. Synthesize bland defaults.
        """
        # Where does dotenv look? Default is `<cwd>/.env`. The server
        # runs from `scaffold_dir / probe.cwd`.
        run_cwd = scaffold_dir / probe.cwd
        target_env = run_cwd / ".env"
        if target_env.is_file():
            return  # caller already provided one
        # Find any .env.example we can use as a template.
        candidates = [
            run_cwd / ".env.example",
            scaffold_dir / ".env.example",
        ]
        template: Optional[Path] = None
        for c in candidates:
            if c.is_file():
                template = c
                break
        if template is None:
            # No template — write a tiny placeholder so dotenv doesn't error.
            try:
                target_env.write_text("NODE_ENV=development\n", encoding="utf-8")
            except Exception:
                pass
            return
        try:
            raw = template.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        # Replace `replace_with_*` placeholders with sentinels that
        # at least don't crash JSON parsers / API auth headers.
        cleaned_lines: List[str] = []
        for line in raw.splitlines():
            if not line or line.startswith("#"):
                cleaned_lines.append(line)
                continue
            if "=" not in line:
                # Probably tool-trace pollution at the top — skip it.
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if "replace_with" in value.lower() or value == "" or value.startswith("<"):
                # Synthesize something the server can parse without crashing.
                if key.endswith("_URL"):
                    value = "http://127.0.0.1:9"
                elif key.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASS", "_PASSWORD")):
                    value = "boot_verifier_placeholder"
                elif key.endswith("_USER"):
                    value = "verifier"
                elif key.endswith("_PORT"):
                    value = "9"
                elif key.endswith("_HOST"):
                    value = "127.0.0.1"
                else:
                    value = "placeholder"
            cleaned_lines.append(f"{key}={value}")
        try:
            target_env.write_text("\n".join(cleaned_lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _build_boot_env(
        self, scaffold_dir: Path, probe: BootProbe, port: int,
    ) -> Dict[str, str]:
        env = os.environ.copy()
        env["PORT"] = str(port)
        env["NODE_ENV"] = env.get("NODE_ENV", "development")
        return env

    # ── subprocess helpers ────────────────────────────────────────

    async def _run_with_timeout(
        self, cmd: List[str], cwd: Path, timeout: int,
        env: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as e:
            return False, "", f"failed to spawn {cmd[0]}: {e}"
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return False, "", f"{cmd[0]} exceeded {timeout}s timeout"
        out = stdout.decode(errors="replace") if stdout else ""
        err = stderr.decode(errors="replace") if stderr else ""
        return proc.returncode == 0, out, err

    async def _boot_and_wait(
        self, cmd: List[str], cwd: Path, env: Dict[str, str], port: int,
    ) -> Tuple[bool, Optional[asyncio.subprocess.Process], str, str]:
        """Start the server, poll the port until it's listening.

        Returns (ok, proc, stdout_so_far, stderr_so_far). On failure
        the proc is already killed; on success it's still running and
        the caller must kill it after the health check.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as e:
            return False, None, "", f"failed to spawn {cmd[0]}: {e}"

        deadline = time.monotonic() + self.boot_timeout
        out_buf: List[str] = []
        err_buf: List[str] = []

        async def _drain_streams() -> None:
            """Pull bytes off stdout/stderr in the background so they
            never block. Stops when the proc exits.
            """
            async def pull(stream, buf):
                while True:
                    line = await stream.readline()
                    if not line:
                        return
                    buf.append(line.decode(errors="replace"))
            await asyncio.gather(
                pull(proc.stdout, out_buf),
                pull(proc.stderr, err_buf),
                return_exceptions=True,
            )

        drainer = asyncio.create_task(_drain_streams())

        while time.monotonic() < deadline:
            if proc.returncode is not None:
                # Process died before binding the port.
                drainer.cancel()
                try:
                    await drainer
                except Exception:
                    pass
                return (
                    False, None,
                    "".join(out_buf),
                    "".join(err_buf) +
                    f"\nserver exited with code {proc.returncode} before binding port {port}",
                )
            if self._port_is_listening(port):
                # Server is ready. Leave the drainer running; caller
                # will await it after the kill.
                return True, proc, "".join(out_buf), "".join(err_buf)
            await asyncio.sleep(0.3)

        # Timeout.
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        drainer.cancel()
        try:
            await drainer
        except Exception:
            pass
        return (
            False, None,
            "".join(out_buf),
            "".join(err_buf) +
            f"\nserver did not bind port {port} within {self.boot_timeout}s",
        )

    async def _kill_proc(self, proc: Optional[asyncio.subprocess.Process]) -> None:
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass

    def _port_is_listening(self, port: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _free_port(self, preferred: int) -> int:
        """Return preferred if free, else pick an OS-assigned free port."""
        if not self._port_is_listening(preferred):
            return preferred
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])
        finally:
            s.close()

    # ── health check ──────────────────────────────────────────────

    async def _health_check(self, port: int) -> Tuple[int, str]:
        """Try each candidate endpoint; return first 2xx/3xx status."""
        import urllib.error
        import urllib.request
        last_status = 0
        last_url = ""
        for path in HEALTH_ENDPOINTS:
            url = f"http://127.0.0.1:{port}{path}"
            last_url = url
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "skyn3t-boot-verifier",
                    "Accept": "application/json, text/html, */*",
                })
                # Run in executor so urllib's blocking I/O doesn't
                # stall the event loop.
                loop = asyncio.get_event_loop()
                resp = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: urllib.request.urlopen(req, timeout=self.health_timeout),
                    ),
                    timeout=self.health_timeout + 1,
                )
                last_status = resp.getcode()
                if 200 <= last_status < 400:
                    return last_status, url
            except urllib.error.HTTPError as e:
                # An HTTP error (e.g. 404) at least means the server
                # is responding. Note it and try the next endpoint.
                last_status = e.code
            except Exception:
                # Connection refused, timeout, etc. — try the next path.
                continue
        return last_status, last_url

    # ── diagnostics ───────────────────────────────────────────────

    @staticmethod
    def _relaxed_install_cmd(cmd: List[str]) -> Optional[List[str]]:
        if not cmd or cmd[0] != "npm" or "install" not in cmd:
            return None
        trimmed = [part for part in cmd if part not in {"--silent", "--prefer-offline"}]
        return trimmed if trimmed != cmd else None

    def _diagnose_install_failure(self, install_log: str) -> str:
        s = install_log or ""
        sl = s.lower()
        if "ENOENT" in s:
            return ("npm install failed: a referenced file or directory "
                    "is missing. Likely a workspace/path mismatch in "
                    "package.json or a missing nested package.json.")
        if "ETARGET" in s or "No matching version" in s:
            return ("npm install failed: a dependency version pin in "
                    "package.json doesn't exist on the registry. "
                    "Loosen the version range (use ^X.Y.Z instead of "
                    "an exact pin that doesn't exist).")
        if "peer dep" in s.lower():
            return ("npm install failed on peer dependency conflict. "
                    "Run with --legacy-peer-deps, or adjust the pinned "
                    "versions so peer requirements are satisfiable.")
        if "exceeded" in sl and "timeout" in sl:
            return (
                "npm install timed out in verifier. Dependencies may still be valid; "
                "retry without --silent/--prefer-offline or increase install timeout."
            )
        if not s.strip():
            return (
                "npm install failed but emitted no diagnostics. Retry with verbose "
                "install flags to surface the underlying npm error."
            )
        return ("npm install failed. See stderr tail for the exact "
                "diagnostic. Common cause: a syntax error in "
                "package.json, or a script in `prepare`/`postinstall` "
                "that itself fails.")

    def _diagnose_boot_failure(
        self, stderr: str, scaffold_dir: Path, probe: BootProbe,
    ) -> str:
        """Return a concrete, actionable description of the boot crash.

        Patterns we recognize have proven to be the most common failure
        modes from real runs (v15 manual-fix history):
          * CJS/ESM module mismatch
          * Module not found
          * Port already in use
          * dotenv missing the .env file
          * Syntax error inside index.js (slipped past node --check
            because of e.g. unclosed template literal)
        """
        s = stderr or ""
        if "require is not defined" in s or "Cannot use import statement outside a module" in s:
            return (
                "CJS/ESM module mismatch. The server entry uses one style "
                "(import / export OR require / module.exports) but "
                "package.json's `\"type\": \"module\"` setting disagrees, "
                "OR the adapter files use a different style than the entry. "
                "Fix: pick ONE module system for the whole server/ tree. "
                "ESM is recommended — set `\"type\": \"module\"` in "
                "package.json, use `import` / `export default` everywhere, "
                "and include the .js extension in import paths."
            )
        if "Cannot find module" in s or "MODULE_NOT_FOUND" in s:
            # Pull the actual module name out of the error.
            import re as _re
            m = _re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", s)
            mod = m.group(1) if m else "<unknown>"
            if mod.startswith("."):
                return (
                    f"Server entry imports a local file ({mod}) that "
                    f"doesn't exist. Likely a path typo, or an adapter "
                    f"file was renamed but the import wasn't updated. "
                    f"List the files under server/adapters/ and adjust "
                    f"the import path."
                )
            return (
                f"Missing npm dependency: {mod}. Add it to "
                f"server/package.json `dependencies` and re-install."
            )
        if "EADDRINUSE" in s or "address already in use" in s:
            return (
                "Port already in use — likely a stale process from a "
                "previous run. Not a code bug. The boot verifier picks a "
                "free port automatically; if this is showing, the server "
                "hardcoded a port instead of reading PORT from env. Fix: "
                "use `const port = Number(process.env.PORT) || 3100`."
            )
        if "Router.use() requires a middleware function" in s:
            return (
                "Express adapter export shape doesn't match what the "
                "server imports. The server does "
                "`app.use('/api/X', adapter)` and Express needs `adapter` "
                "to be a function (a Router instance), but the adapter "
                "is exporting an object/module. Make sure every "
                "server/adapters/*.js does `export default router;` "
                "(ESM) or `module.exports = router;` (CJS) — NOT "
                "`export { router }` or `export const router`."
            )
        if "SyntaxError" in s:
            # node may catch a syntax error at runtime that escaped
            # `node --check` (template-literal patterns, etc).
            return (
                "Runtime syntax error in the server entry or an adapter "
                "(slipped past `node --check`). Common causes: unclosed "
                "template literal, mismatched quote inside a regex, or "
                "leftover markdown fences. See stderr tail."
            )
        if "dotenv" in s.lower() and ("ENOENT" in s or "no such file" in s.lower()):
            return (
                "dotenv can't find .env. The server runs from "
                f"{probe.cwd}/ but the .env lives elsewhere. Either "
                f"move .env to {probe.cwd}/ or call "
                f"`dotenv.config({{path: '../.env'}})` explicitly."
            )
        # Fallback: return the last meaningful stderr line so the LLM
        # has *something* concrete to act on.
        meaningful = [
            ln for ln in s.splitlines()
            if ln.strip() and not ln.startswith(" ")
            and "node:internal" not in ln
        ]
        tail = " ".join(meaningful[-3:]) if meaningful else "(no diagnostic captured)"
        return f"Server failed to start. Last error: {tail}"

    def _fail_output(
        self, probe: BootProbe, scaffold_dir: Path, *,
        command: str, stdout: str, stderr: str, summary: str,
        failure_hint: str,
    ) -> Dict[str, Any]:
        try:
            asyncio.create_task(self.share_learning(
                f"boot_verifier: {probe.kind} → no", scope="build",
            ))
        except Exception:
            pass
        return {
            "verdict": "no",
            "kind": probe.kind,
            "command": command,
            "port": probe.port,
            "health_url": "",
            "http_status": 0,
            "stdout": (stdout or "")[-4000:],
            "stderr": (stderr or "")[-4000:],
            "summary": summary,
            "scaffold_dir": str(scaffold_dir),
            "failure_hint": failure_hint,
        }
