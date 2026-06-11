"""BuildVerifierAgent — does the scaffold actually run?

Existing VerifierAgent reads markdown and grades it for prose quality.
That's useless for code: a scaffold can be "well-described" and still
not run. BuildVerifierAgent does the thing that closes the loop:

- Walks the scaffold dir, sniffs the stack (Python, Node, static HTML,
  Swift, etc.) from files present.
- Runs the stack-appropriate verify command (``python -m py_compile``,
  ``npm install --silent && npm run build``, ``html5validator``, etc.)
  with strict timeouts.
- Returns a structured result with stdout/stderr captured so the next
  retry attempt can use the failure log as a hint.

Why this matters: until the system has ground-truth "does it run?"
signal, every other learning loop is training on the wrong gradient
(reviewer prose grades, lesson scoreboard, meta-agent patterns). The
verifier turns "the LLM wrote some files" into "the files form a
working program."
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.build_verifier")

# Hard ceiling — no verify step should ever take longer than this. We don't
# want a hung `npm install` to wedge the pipeline.
DEFAULT_VERIFY_TIMEOUT_SECONDS = 240


@dataclass
class StackProbe:
    """Detected stack signature for a scaffold dir."""

    kind: str                       # 'python' | 'node' | 'static' | 'swift' | 'unknown'
    entry_files: List[str]
    notes: List[str]


class BuildVerifierAgent(BaseAgent):
    """Runs the scaffold and reports whether it builds.

    Output schema:

        {
          "verdict": "yes" | "no" | "skipped",
          "stack":   "python" | "node" | "static" | "swift" | ...,
          "command": "the command we actually ran",
          "stdout":  "last 4000 chars of stdout",
          "stderr":  "last 4000 chars of stderr",
          "summary": "human-readable one-liner",
          "scaffold_dir": "...",
          "failure_hint": "..."   # only when verdict=='no'; designed to be
                                  # injected as the auto-retry's lesson.
        }
    """

    def __init__(
        self,
        name: str = "build_verifier",
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
            name="build_verification",
            description="Detects the scaffold's stack and runs the appropriate build/compile check.",
            parameters={"scaffold_dir": "str", "stack": "str (optional)"},
        ))
        self.timeout_seconds = int(
            (config or {}).get("verify_timeout_seconds", DEFAULT_VERIFY_TIMEOUT_SECONDS)
        )

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        data = task.input_data or {}
        # Accept either an explicit scaffold_dir or fall back to
        # `<artifact_dir>/scaffold` (where CodeAgent writes by default).
        scaffold_dir_raw = (
            data.get("scaffold_dir")
            or (str(Path(data.get("artifact_dir", "")) / "scaffold")
                if data.get("artifact_dir") else None)
        )
        if not scaffold_dir_raw:
            return TaskResult(
                task_id=task.task_id, success=False,
                error="scaffold_dir required (or artifact_dir with a scaffold/ subdir)",
            )
        scaffold_dir = Path(scaffold_dir_raw).expanduser().resolve()
        if not scaffold_dir.exists() or not scaffold_dir.is_dir():
            return TaskResult(
                task_id=task.task_id, success=False,
                error=f"scaffold_dir does not exist: {scaffold_dir}",
            )

        forced_stack = (data.get("stack") or "").lower().strip() or None
        execution_profile = str(data.get("execution_profile") or "balanced").strip().lower()
        probe = self._detect_stack(scaffold_dir, forced=forced_stack)
        await self.think(f"detected stack: {probe.kind} ({len(probe.entry_files)} entry files)")

        if probe.kind == "unknown":
            return TaskResult(
                task_id=task.task_id, success=True,
                output={
                    "verdict": "skipped",
                    "stack": "unknown",
                    "command": None,
                    "stdout": "",
                    "stderr": "",
                    "summary": "Could not detect a known stack; skipping build verification.",
                    "scaffold_dir": str(scaffold_dir),
                    "failure_hint": None,
                },
            )

        verdict, command, stdout, stderr = await self._run_verify(
            scaffold_dir, probe, execution_profile=execution_profile,
        )
        summary = self._summarize(probe, verdict, command)
        failure_hint = self._failure_hint(probe, verdict, stdout, stderr) if verdict == "no" else None

        try:
            await self.share_learning(
                f"build_verifier: {probe.kind} → {verdict}",
                scope="build",
            )
        except Exception:
            logger.debug("share_learning(build_verifier) failed", exc_info=True)

        return TaskResult(
            task_id=task.task_id, success=True,
            output={
                "verdict": verdict,
                "stack": probe.kind,
                "command": command,
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "summary": summary,
                "scaffold_dir": str(scaffold_dir),
                "failure_hint": failure_hint,
                "entry_files": probe.entry_files,
            },
        )

    # ------------------------------------------------------------------
    # Stack detection
    # ------------------------------------------------------------------

    def _detect_stack(self, scaffold_dir: Path, *, forced: Optional[str] = None) -> StackProbe:
        if forced:
            return StackProbe(kind=forced, entry_files=[], notes=[f"forced via kwarg: {forced}"])
        names = {p.name for p in scaffold_dir.iterdir() if p.is_file()}
        # Swift / Xcode project
        if any(p.suffix == ".xcodeproj" for p in scaffold_dir.iterdir() if p.is_dir()) or "Package.swift" in names:
            entries = [n for n in names if n.endswith(".swift") or n == "Package.swift"]
            return StackProbe(kind="swift", entry_files=entries, notes=["Swift project detected"])
        # Node / browser bundlers
        if "package.json" in names:
            entries = ["package.json"]
            return StackProbe(kind="node", entry_files=entries, notes=["package.json present"])
        # Python
        py_files = [n for n in names if n.endswith(".py")]
        if py_files or "pyproject.toml" in names or "requirements.txt" in names:
            return StackProbe(
                kind="python",
                entry_files=py_files or ["pyproject.toml"] if "pyproject.toml" in names else py_files,
                notes=["Python files present" if py_files else "Python build files present"],
            )
        # Static web (HTML+CSS+JS sitting at the root)
        html_files = [n for n in names if n.endswith(".html") or n.endswith(".htm")]
        if html_files:
            return StackProbe(kind="static", entry_files=html_files, notes=["HTML files present"])
        return StackProbe(kind="unknown", entry_files=sorted(names)[:10], notes=["no recognizable stack signature"])

    # ------------------------------------------------------------------
    # Verify runners
    # ------------------------------------------------------------------

    async def _run_verify(
        self,
        scaffold_dir: Path,
        probe: StackProbe,
        *,
        execution_profile: str = "balanced",
    ) -> tuple[str, str, str, str]:
        """Return (verdict, command-as-string, stdout, stderr)."""
        if probe.kind == "python":
            return await self._verify_python(scaffold_dir, probe)
        if probe.kind == "node":
            return await self._verify_node(scaffold_dir, execution_profile=execution_profile)
        if probe.kind == "static":
            return await self._verify_static(scaffold_dir, probe)
        if probe.kind == "swift":
            return await self._verify_swift(scaffold_dir)
        return "skipped", "", "", ""

    async def _verify_python(self, scaffold_dir: Path, probe: StackProbe) -> tuple[str, str, str, str]:
        """py_compile every .py file. Fast, no network, catches syntax errors
        + missing imports at compile time."""
        py_files = [str(p) for p in scaffold_dir.rglob("*.py") if "__pycache__" not in p.parts]
        if not py_files:
            return "skipped", "(no .py files)", "", ""
        cmd = ["python3", "-m", "py_compile", *py_files]
        proc = await self._run(cmd, scaffold_dir)
        verdict = "yes" if proc["returncode"] == 0 else "no"
        return verdict, " ".join(cmd[:6]) + (" …" if len(cmd) > 6 else ""), proc["stdout"], proc["stderr"]

    async def _verify_node(
        self,
        scaffold_dir: Path,
        *,
        execution_profile: str = "balanced",
    ) -> tuple[str, str, str, str]:
        """Node verifier — three escalating gates:

        Gate 1 — package.json shape: must parse as JSON, must have valid
                 ``scripts``/``dependencies`` types if present.

        Gate 2 — syntax check: run ``node --check`` on every .js/.mjs/.cjs
                 file. Catches syntax errors without an install.

        Gate 3 — install + build (DEFAULT ON): runs
                 ``npm install --no-audit --no-fund --silent
                 --prefer-offline`` then ``npm run build`` if a build
                 script is present. This catches missing dependencies,
                 import resolution errors, and JSX/TypeScript compile
                 errors that ``node --check`` misses.

                 Can be disabled via ``SKYN3T_VERIFY_NPM_INSTALL=0`` for
                 air-gapped environments where npm registry is unreachable.
        """
        pkg_path = scaffold_dir / "package.json"
        try:
            pkg_text = pkg_path.read_text()
            pkg = json.loads(pkg_text)
        except FileNotFoundError:
            return "no", "package.json missing", "", "package.json file missing"
        except Exception as exc:
            return "no", "package.json parse", "", f"package.json could not be parsed as JSON: {exc}"

        # Gate 1: shape validation.
        shape_errors: List[str] = []
        if not isinstance(pkg, dict):
            shape_errors.append("package.json root must be an object")
        else:
            scripts_val = pkg.get("scripts")
            if scripts_val is not None and not isinstance(scripts_val, dict):
                shape_errors.append("`scripts` must be an object of {name: command}")
            deps_val = pkg.get("dependencies")
            if deps_val is not None and not isinstance(deps_val, dict):
                shape_errors.append("`dependencies` must be an object of {name: version}")
            dev_deps_val = pkg.get("devDependencies")
            if dev_deps_val is not None and not isinstance(dev_deps_val, dict):
                shape_errors.append("`devDependencies` must be an object of {name: version}")
        if shape_errors:
            return "no", "package.json shape", "", "\n".join(shape_errors)

        scripts = (pkg.get("scripts") or {}) if isinstance(pkg, dict) else {}
        node_bin = shutil.which("node")

        # Gate 2: syntax check via `node --check` on every .js/.mjs/.cjs.
        if node_bin:
            js_files = [
                str(p) for p in scaffold_dir.rglob("*")
                if p.is_file()
                and p.suffix in (".js", ".mjs", ".cjs")
                and "node_modules" not in p.parts
            ]
            for f in js_files:
                proc = await self._run([node_bin, "--check", f], scaffold_dir)
                if proc["returncode"] != 0:
                    return (
                        "no",
                        f"node --check {Path(f).name}",
                        proc["stdout"],
                        proc["stderr"],
                    )

        # Gate 3: install + build (default ON since v40).
        install_disabled = os.environ.get("SKYN3T_VERIFY_NPM_INSTALL", "").lower() in ("0", "false", "no", "off")
        npm_bin = shutil.which("npm")
        if not install_disabled and npm_bin:
            install_cmd = [
                npm_bin, "install",
                "--no-audit", "--no-fund", "--silent", "--prefer-offline",
            ]
            proc = await self._run(install_cmd, scaffold_dir)
            if proc["returncode"] != 0:
                # Network failure is NOT a build failure — fall back to
                # parse + syntax so air-gapped hosts don't get false negatives.
                network_error = any(
                    phrase in (proc["stderr"] or "")
                    for phrase in ("ECONNREFUSED", "ENOTFOUND", "network", "timeout", "unable to connect")
                )
                offline_ok = os.environ.get("SKYN3T_VERIFY_OFFLINE", "").lower() in (
                    "1", "true", "yes", "on",
                )
                if network_error and (
                    offline_ok or execution_profile == "fast"
                ):
                    return (
                        "yes",
                        " ".join(install_cmd) + " (network failure — falling back to syntax check)",
                        proc["stdout"],
                        proc["stderr"],
                    )
                if network_error:
                    return (
                        "no",
                        " ".join(install_cmd) + " (network failure — npm install required)",
                        proc["stdout"],
                        proc["stderr"],
                    )
                return (
                    "no",
                    " ".join(install_cmd),
                    proc["stdout"],
                    proc["stderr"],
                )
            if scripts.get("build"):
                build_cmd = [npm_bin, "run", "build", "--silent"]
                proc = await self._run(build_cmd, scaffold_dir)
                if proc["returncode"] != 0:
                    return (
                        "no",
                        " ".join(build_cmd),
                        proc["stdout"],
                        proc["stderr"],
                    )
                return (
                    "yes",
                    " && ".join([" ".join(install_cmd), " ".join(build_cmd)]),
                    proc["stdout"],
                    "",
                )
            return (
                "yes",
                " ".join(install_cmd),
                proc["stdout"],
                "",
            )

        # Fallback: no npm available or install explicitly disabled.
        gate_summary = []
        if isinstance(pkg, dict):
            gate_summary.append("shape ok")
        if node_bin:
            gate_summary.append("node --check ok")
        if install_disabled:
            gate_summary.append("install disabled by env")
        return "yes", " · ".join(gate_summary) or "node parse", "node project verified", ""

    async def _verify_static(self, scaffold_dir: Path, probe: StackProbe) -> tuple[str, str, str, str]:
        """Static HTML: two gates.

        Gate 1 — parse: run html.parser strict-feed on every entry HTML
        file. Catches unclosed tags, malformed markup. Fast, no network,
        no external deps.

        Gate 2 — render (optional): if Playwright is installed AND a
        browser binary is available, open the first entry file in a
        headless Chromium, wait for `load`, and capture any console-
        error or pageerror events. Catches JS syntax errors, missing
        script files, runtime exceptions on page load — the failure
        class the parser-only gate misses entirely.

        Render gate is best-effort: if Playwright isn't installed or the
        browser isn't available, the static verdict reflects the parse
        gate alone.
        """
        from html.parser import HTMLParser

        class _StrictParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.errors: List[str] = []

            def error(self, message: str) -> None:  # pragma: no cover - py>=3.5 stub
                self.errors.append(message)

        any_errors = False
        details: List[str] = []
        for entry in probe.entry_files:
            target = scaffold_dir / entry
            try:
                html = target.read_text(encoding="utf-8")
            except Exception as e:
                any_errors = True
                details.append(f"{entry}: read failed ({e})")
                continue
            p = _StrictParser()
            try:
                p.feed(html)
                p.close()
            except Exception as e:
                any_errors = True
                details.append(f"{entry}: parse failed ({e})")
                continue
            if p.errors:
                any_errors = True
                details.append(f"{entry}: {p.errors}")
        if any_errors:
            return "no", "html.parser strict-feed", "", "\n".join(details)

        # Parse gate passed. Try the optional render gate.
        render_result = await asyncio.to_thread(
            self._render_smoke_test, scaffold_dir, probe.entry_files[0],
        )
        if render_result is None:
            return (
                "yes",
                "html.parser strict-feed (render gate skipped: playwright unavailable)",
                f"parsed {len(probe.entry_files)} HTML file(s) cleanly",
                "",
            )
        rendered_ok, rendered_errors, rendered_stdout = render_result
        if not rendered_ok:
            return (
                "no",
                "playwright headless render",
                rendered_stdout,
                "\n".join(rendered_errors),
            )
        return (
            "yes",
            "playwright headless render + html.parser",
            rendered_stdout,
            "",
        )

    @staticmethod
    def _render_smoke_test(
        scaffold_dir: Path, entry_html: str,
    ) -> Optional[tuple[bool, List[str], str]]:
        """Open ``entry_html`` in a headless Chromium and report errors.

        Returns ``None`` when Playwright (or its bundled Chromium) isn't
        installed — caller treats that as "skipped, don't penalize." Returns
        ``(ok, errors, stdout)`` when the test actually ran. ok=False means
        at least one console error or pageerror fired during page load.

        Runs synchronously (we're already inside asyncio.to_thread). The
        Playwright async API can't be reentered from inside an existing
        event loop, so the sync_api is the right choice here.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return None  # Playwright not installed.
        target = (scaffold_dir / entry_html).resolve()
        if not target.exists():
            return False, [f"render: entry HTML missing: {entry_html}"], ""
        file_url = target.as_uri()
        errors: List[str] = []
        stdout_lines: List[str] = []
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(headless=True)
                except Exception as exc:
                    # Chromium binary not installed — Playwright lib present
                    # but `playwright install` was never run. Treat as
                    # skipped so we don't fail otherwise-good scaffolds.
                    if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                        return None
                    return False, [f"render: chromium launch failed: {exc}"], ""
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    page.on("console", lambda msg: (
                        errors.append(f"console.{msg.type}: {msg.text}")
                        if msg.type == "error"
                        else stdout_lines.append(f"console.{msg.type}: {msg.text}")
                    ))
                    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
                    page.on("requestfailed", lambda req: errors.append(
                        f"requestfailed: {req.url} → {(req.failure or 'unknown')}"
                    ))
                    try:
                        page.goto(file_url, wait_until="load", timeout=15000)
                    except Exception as exc:
                        errors.append(f"goto: {exc}")
                    # Brief settle window for any deferred init scripts.
                    page.wait_for_timeout(250)
                finally:
                    browser.close()
        except Exception as exc:
            errors.append(f"playwright session failed: {exc}")
        ok = len(errors) == 0
        return ok, errors, "\n".join(stdout_lines[-20:])

    async def _verify_swift(self, scaffold_dir: Path) -> tuple[str, str, str, str]:
        """Swift: `swift build` if a Package.swift exists and `swift` is on
        PATH. iOS Xcode projects can't be verified without xcodebuild + a
        full SDK install, so those report skipped."""
        if not shutil.which("swift"):
            return "skipped", "(swift not on PATH)", "", "swift toolchain not installed"
        if (scaffold_dir / "Package.swift").exists():
            cmd = ["swift", "build", "-c", "debug"]
            proc = await self._run(cmd, scaffold_dir)
            verdict = "yes" if proc["returncode"] == 0 else "no"
            return verdict, " ".join(cmd), proc["stdout"], proc["stderr"]
        return "skipped", "(Xcode project; needs xcodebuild)", "", "Xcode-only project; full SDK verify out of scope"

    # ------------------------------------------------------------------
    # Subprocess wrapper
    # ------------------------------------------------------------------

    async def _run(self, cmd: List[str], cwd: Path) -> Dict[str, Any]:
        """Run a command with a timeout. Returns {returncode, stdout, stderr}.

        We use asyncio.to_thread so a slow subprocess doesn't block the event
        loop. Timeout failures are surfaced as returncode 124 (the exit code
        the `timeout` shell utility uses).
        """
        try:
            proc = await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    env={**os.environ, "CI": "1"},
                    timeout=self.timeout_seconds,
                ),
                timeout=self.timeout_seconds + 5,
            )
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "returncode": 124,
                "stdout": (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, (bytes, bytearray)) else (exc.stdout or ""),
                "stderr": f"timed out after {self.timeout_seconds}s",
            }
        except Exception as exc:
            return {"returncode": -1, "stdout": "", "stderr": str(exc)}

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize(probe: StackProbe, verdict: str, command: str) -> str:
        if verdict == "yes":
            return f"Build verified ({probe.kind}): {command}"
        if verdict == "no":
            return f"Build FAILED ({probe.kind}): {command}"
        return f"Build verification skipped ({probe.kind}): {command}"

    @staticmethod
    def _failure_hint(probe: StackProbe, verdict: str, stdout: str, stderr: str) -> str:
        """Compact hint suitable for injection into the retry's brief.

        Carries the verifier's command + a short tail of stderr — enough
        signal for the next code-generation attempt to do something
        different without re-pasting megabytes of build log.
        """
        if verdict != "no":
            return ""
        tail = (stderr or stdout or "").strip().splitlines()[-12:]
        return (
            f"Previous build attempt failed during {probe.kind} verification. "
            f"Last log lines:\n" + "\n".join(tail)
        )
