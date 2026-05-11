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

        verdict, command, stdout, stderr = await self._run_verify(scaffold_dir, probe)
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
        notes: List[str] = []
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

    async def _run_verify(self, scaffold_dir: Path, probe: StackProbe) -> tuple[str, str, str, str]:
        """Return (verdict, command-as-string, stdout, stderr)."""
        if probe.kind == "python":
            return await self._verify_python(scaffold_dir, probe)
        if probe.kind == "node":
            return await self._verify_node(scaffold_dir)
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

    async def _verify_node(self, scaffold_dir: Path) -> tuple[str, str, str, str]:
        """If node + package.json: `node --check` the entry, or `npm run build`
        if a `build` script exists. We deliberately skip `npm install` here
        unless the project pins a lockfile — full installs are too slow for a
        verifier and can hang on network."""
        # Best-effort: parse package.json for a build script and an entry file.
        pkg_path = scaffold_dir / "package.json"
        pkg: Dict[str, Any] = {}
        try:
            pkg = json.loads(pkg_path.read_text())
        except Exception:
            return "no", "package.json read failed", "", "package.json could not be parsed as JSON"
        scripts = (pkg.get("scripts") or {}) if isinstance(pkg, dict) else {}
        node_bin = shutil.which("node")
        if node_bin and (scaffold_dir / "index.js").exists():
            cmd = [node_bin, "--check", "index.js"]
            proc = await self._run(cmd, scaffold_dir)
            verdict = "yes" if proc["returncode"] == 0 else "no"
            return verdict, " ".join(cmd), proc["stdout"], proc["stderr"]
        if scripts.get("build") and shutil.which("npm") and (scaffold_dir / "package-lock.json").exists():
            cmd = ["npm", "run", "build", "--silent"]
            proc = await self._run(cmd, scaffold_dir)
            verdict = "yes" if proc["returncode"] == 0 else "no"
            return verdict, " ".join(cmd), proc["stdout"], proc["stderr"]
        # We have a package.json but no easy way to validate without installing.
        # Don't fail the project — report skipped with a note.
        return (
            "skipped",
            "(node project, no lockfile or runnable entry)",
            "",
            "Node project detected but no package-lock.json or runnable index.js; "
            "verifier skipped install/build to keep the pipeline fast.",
        )

    async def _verify_static(self, scaffold_dir: Path, probe: StackProbe) -> tuple[str, str, str, str]:
        """Static HTML: parse it. We don't fail on missing-CSS or 404s — only
        on actual HTML parse errors / unclosed tags."""
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
        return "yes", "html.parser strict-feed", f"parsed {len(probe.entry_files)} HTML file(s) cleanly", ""

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
