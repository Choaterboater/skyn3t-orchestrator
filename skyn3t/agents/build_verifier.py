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
import socket
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

# Below this 0-100 design score, the visual gate hard-fails the build. Read
# from SKYN3T_VISUAL_MIN_SCORE; only consulted when a rubric score is present.
DEFAULT_VISUAL_MIN_SCORE = 35

# Viewports for the responsive screenshot pass: desktop then mobile.
_VISUAL_VIEWPORTS = {"desktop": (1280, 800), "mobile": (375, 812)}


def _env_flag_on(name: str, *, default: bool = True) -> bool:
    """Default-on env gate. Returns False only on an explicit off value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _visual_min_score() -> int:
    try:
        return int(os.environ.get("SKYN3T_VISUAL_MIN_SCORE", str(DEFAULT_VISUAL_MIN_SCORE)))
    except (TypeError, ValueError):
        return DEFAULT_VISUAL_MIN_SCORE


# In-page JS for the cheap visual heuristics. Walks a sample of rendered nodes
# and reports: a non-default page background, the count of distinct text/bg
# colors, whether any border-radius / box-shadow is in use, and horizontal
# overflow (scrollWidth wider than the viewport — the responsive failure).
_VISUAL_HEURISTICS_JS = r"""
() => {
  try {
    const bodyBg = getComputedStyle(document.body).backgroundColor || '';
    const htmlBg = getComputedStyle(document.documentElement).backgroundColor || '';
    const isDefaultBg = (c) => !c || c === 'rgba(0, 0, 0, 0)' || c === 'transparent'
      || c === 'rgb(255, 255, 255)' || c === 'rgb(0, 0, 0)';
    const non_default_bg = !(isDefaultBg(bodyBg) && isDefaultBg(htmlBg));

    const colors = new Set();
    let has_radius = false;
    let has_shadow = false;
    const nodes = Array.from(document.querySelectorAll('*')).slice(0, 800);
    for (const el of nodes) {
      const cs = getComputedStyle(el);
      if (cs.color && cs.color !== 'rgba(0, 0, 0, 0)') colors.add(cs.color);
      const bg = cs.backgroundColor;
      if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') colors.add(bg);
      if (!has_radius) {
        const r = parseFloat(cs.borderTopLeftRadius || '0');
        if (r > 0.5) has_radius = true;
      }
      if (!has_shadow) {
        if (cs.boxShadow && cs.boxShadow !== 'none') has_shadow = true;
      }
    }
    const doc = document.documentElement;
    const horizontal_overflow = (doc.scrollWidth - doc.clientWidth) > 4;
    return {
      non_default_bg,
      distinct_colors: colors.size,
      has_radius,
      has_shadow,
      horizontal_overflow,
    };
  } catch (e) {
    return {non_default_bg: false, distinct_colors: 0, has_radius: false,
            has_shadow: false, horizontal_overflow: false};
  }
}
"""


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

          # ---- Phase 3 gates (additive; never remove the keys above) ----
          "visual_verification": {       # Optional[Dict] — None when N/A
              "ran": bool,
              "verdict": "yes" | "no" | "skipped",
              "score": Optional[int],    # 0-100 design rubric, if vision CLI present
              "desktop_screenshot": Optional[str],
              "mobile_screenshot":  Optional[str],
              "heuristics": {
                  "non_default_bg": bool,
                  "distinct_colors": int,
                  "has_radius": bool,
                  "has_shadow": bool,
                  "horizontal_overflow": bool,
              },
              "a11y_violations": int,
              "reasons": [str, ...],
          },
          "test_run": {                  # Optional[Dict] — None when N/A
              "ran": bool,
              "passed": bool,
              "verdict": "yes" | "no" | "skipped",
              "summary": str,
              "stdout_tail": str,
          },
        }

    The visual gate is guarded by SKYN3T_VERIFY_VISUAL (default on) and the
    test gate by SKYN3T_VERIFY_TESTS (default on). Both DEGRADE to a
    'skipped' verdict that leaves the top-level verdict unchanged whenever
    their external tooling (node/npm, playwright/chromium, network) is
    unavailable — an absent gate never penalizes a build.
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

        # ---- Phase 3 gates (additive, fold-down only) -------------------
        # The visual + test gates only run when the base build PASSED — there
        # is nothing to serve or test if the build itself failed. They never
        # upgrade a verdict; they can only fold a passing verdict to 'no'.
        visual_verification: Optional[Dict[str, Any]] = None
        test_run: Optional[Dict[str, Any]] = None
        visual_hint: Optional[str] = None
        if verdict == "yes":
            test_run = await self._run_test_gate(
                scaffold_dir, probe, execution_profile=execution_profile,
            )
            if test_run and test_run.get("verdict") == "no":
                verdict = "no"
                stderr = ((stderr + "\n") if stderr else "") + (
                    "test gate: " + str(test_run.get("summary") or "tests failed")
                )

            visual_verification = await self._run_visual_gate(
                scaffold_dir, probe, execution_profile=execution_profile,
            )
            if visual_verification and visual_verification.get("verdict") == "no":
                verdict = "no"
                reasons = visual_verification.get("reasons") or []
                visual_hint = "visual gate: " + ("; ".join(reasons) or "below visual threshold")

        summary = self._summarize(probe, verdict, command)
        failure_hint = self._failure_hint(probe, verdict, stdout, stderr) if verdict == "no" else None
        if verdict == "no" and visual_hint:
            # Surface the visual reason in the retry hint even though it isn't
            # in stdout/stderr — the build itself compiled, it just looks bad.
            failure_hint = (failure_hint + "\n" + visual_hint) if failure_hint else visual_hint

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
                "visual_verification": visual_verification,
                "test_run": test_run,
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

    # ------------------------------------------------------------------
    # Phase 3 gate: test runner (npm test / vitest)
    # ------------------------------------------------------------------

    async def _run_test_gate(
        self,
        scaffold_dir: Path,
        probe: StackProbe,
        *,
        execution_profile: str = "balanced",
    ) -> Optional[Dict[str, Any]]:
        """Run the generated project's own test suite as part of the verdict.

        Returns a ``test_run`` dict, or ``None`` when not applicable (no
        package.json at all). DEGRADES to verdict 'skipped' (top-level verdict
        UNCHANGED) when:
          * SKYN3T_VERIFY_TESTS is off,
          * the stack is not node / has no package.json,
          * package.json has no ``test`` script (or only the npm-default
            "no test specified" stub),
          * node/npm is unavailable,
          * install is disabled / offline (SKYN3T_VERIFY_NPM_INSTALL=0).

        Only an actual test *failure* yields verdict 'no'.
        """
        if probe.kind != "node":
            return None
        pkg_path = scaffold_dir / "package.json"
        if not pkg_path.exists():
            return None

        def _skipped(summary: str) -> Dict[str, Any]:
            return {
                "ran": False, "passed": False, "verdict": "skipped",
                "summary": summary, "stdout_tail": "",
            }

        if not _env_flag_on("SKYN3T_VERIFY_TESTS"):
            return _skipped("test gate disabled (SKYN3T_VERIFY_TESTS=0)")

        try:
            pkg = json.loads(pkg_path.read_text())
        except Exception:
            return _skipped("package.json unreadable for test gate")
        scripts = (pkg.get("scripts") or {}) if isinstance(pkg, dict) else {}
        test_script = scripts.get("test")
        if not isinstance(test_script, str) or not test_script.strip():
            return _skipped("no test script in package.json")
        # The npm-default placeholder is not a real test suite.
        if "no test specified" in test_script.lower():
            return _skipped("only the npm-default placeholder test script")

        npm_bin = shutil.which("npm")
        node_bin = shutil.which("node")
        if not npm_bin or not node_bin:
            return _skipped("node/npm not available for test gate")

        install_disabled = os.environ.get("SKYN3T_VERIFY_NPM_INSTALL", "").lower() in (
            "0", "false", "no", "off",
        )
        if install_disabled:
            return _skipped("install disabled by env; test gate offline-skipped")

        # Tests need installed deps. If node_modules is absent and install is
        # allowed, _verify_node already installed in the common path — but be
        # defensive and ensure deps exist before invoking the runner.
        if not (scaffold_dir / "node_modules").exists():
            install_cmd = [
                npm_bin, "install", "--no-audit", "--no-fund", "--silent", "--prefer-offline",
            ]
            iproc = await self._run(install_cmd, scaffold_dir)
            if iproc["returncode"] != 0:
                network_error = any(
                    phrase in (iproc["stderr"] or "")
                    for phrase in ("ECONNREFUSED", "ENOTFOUND", "network", "timeout", "unable to connect")
                )
                # An install failure must not fail a build the test gate can't
                # even set up — degrade to skipped.
                return _skipped(
                    "deps install failed; test gate skipped"
                    + (" (network)" if network_error else "")
                )

        test_cmd = [npm_bin, "test", "--silent"]
        proc = await self._run(test_cmd, scaffold_dir)
        combined = ((proc.get("stdout") or "") + "\n" + (proc.get("stderr") or "")).strip()
        tail = "\n".join(combined.splitlines()[-30:])
        passed = proc["returncode"] == 0
        return {
            "ran": True,
            "passed": passed,
            "verdict": "yes" if passed else "no",
            "summary": (
                "tests passed" if passed
                else f"tests failed (exit {proc['returncode']})"
            ),
            "stdout_tail": tail[-4000:],
        }

    # ------------------------------------------------------------------
    # Phase 3 gate: visual verification (serve + screenshot + heuristics)
    # ------------------------------------------------------------------

    async def _run_visual_gate(
        self,
        scaffold_dir: Path,
        probe: StackProbe,
        *,
        execution_profile: str = "balanced",
    ) -> Optional[Dict[str, Any]]:
        """Screenshot-based visual gate over a freshly built node app.

        Returns a ``visual_verification`` dict, or ``None`` when not applicable
        (non-node stack with no build output). DEGRADES to verdict 'skipped'
        (top-level verdict UNCHANGED) when SKYN3T_VERIFY_VISUAL is off, node/npm
        is unavailable, there is no build output to serve, or
        Playwright/chromium can't render. Only the cheap structural heuristics
        (or a sub-threshold rubric score) ever yield verdict 'no'.
        """
        if probe.kind != "node":
            return None

        def _skipped(reason: str) -> Dict[str, Any]:
            return {
                "ran": False, "verdict": "skipped", "score": None,
                "desktop_screenshot": None, "mobile_screenshot": None,
                "heuristics": {
                    "non_default_bg": False, "distinct_colors": 0,
                    "has_radius": False, "has_shadow": False,
                    "horizontal_overflow": False,
                },
                "a11y_violations": 0, "reasons": [reason],
            }

        if not _env_flag_on("SKYN3T_VERIFY_VISUAL"):
            return _skipped("visual gate disabled (SKYN3T_VERIFY_VISUAL=0)")

        # Locate something to serve FIRST (cheap filesystem check): prefer a
        # built dist/ then a plain index.html. Bail before the expensive
        # chromium launch when there's nothing to render.
        serve_dir = self._locate_serve_dir(scaffold_dir)
        if serve_dir is None:
            return _skipped("no build output / index.html to serve")

        # Playwright must be importable AND chromium launchable; check before we
        # spin up a server we couldn't screenshot anyway.
        if not self._playwright_renderable():
            return _skipped("playwright/chromium unavailable")

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._visual_gate_blocking, serve_dir),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return _skipped("visual gate timed out")
        except Exception as exc:  # never let the gate crash the build
            logger.debug("visual gate failed", exc_info=True)
            return _skipped(f"visual gate error: {exc}")
        if result is None:
            return _skipped("playwright/chromium unavailable")

        # Optional vision rubric (owned by design_assets). Absent → heuristics
        # only. Run async because score_screenshot is a coroutine.
        score: Optional[int] = None
        rubric_reasons: List[str] = []
        desktop_shot = result.get("desktop_screenshot")
        if desktop_shot:
            score, rubric_reasons = await self._maybe_score_screenshot(
                Path(desktop_shot),
                brief=str((self.config or {}).get("brief", "")),
                mood=str((self.config or {}).get("mood", "")),
            )

        heuristics = result.get("heuristics") or {}
        a11y = int(result.get("a11y_violations") or 0)
        reasons: List[str] = list(result.get("reasons") or [])
        reasons.extend(rubric_reasons)

        verdict = "yes"
        # Cheap structural fails (always evaluated).
        if heuristics.get("horizontal_overflow"):
            verdict = "no"
            reasons.append("page overflows horizontally on mobile (375px)")
        if not heuristics.get("non_default_bg") and heuristics.get("distinct_colors", 0) <= 1:
            verdict = "no"
            reasons.append("page is effectively unstyled (default background, ~no distinct colors)")
        # Optional rubric fail (only when a score was produced).
        if score is not None and score < _visual_min_score():
            verdict = "no"
            reasons.append(f"design rubric score {score} < threshold {_visual_min_score()}")

        return {
            "ran": True,
            "verdict": verdict,
            "score": score,
            "desktop_screenshot": result.get("desktop_screenshot"),
            "mobile_screenshot": result.get("mobile_screenshot"),
            "heuristics": {
                "non_default_bg": bool(heuristics.get("non_default_bg", False)),
                "distinct_colors": int(heuristics.get("distinct_colors", 0)),
                "has_radius": bool(heuristics.get("has_radius", False)),
                "has_shadow": bool(heuristics.get("has_shadow", False)),
                "horizontal_overflow": bool(heuristics.get("horizontal_overflow", False)),
            },
            "a11y_violations": a11y,
            "reasons": reasons,
        }

    async def _maybe_score_screenshot(
        self, image_path: Path, *, brief: str = "", mood: str = "",
    ) -> tuple[Optional[int], List[str]]:
        """Call design_vision.score_screenshot if the module exposes it.

        Returns (score, reasons). DEGRADES to (None, []) when the optional
        rubric function is absent (owned by another agent and may not be
        present yet) or returns None / raises — heuristics-only, non-blocking.
        """
        try:
            from skyn3t.agents import design_vision  # local import: optional dep
        except Exception:
            return None, []
        scorer = getattr(design_vision, "score_screenshot", None)
        if scorer is None or not callable(scorer):
            return None, []
        try:
            res = scorer(image_path, brief=brief, mood=mood)
            if asyncio.iscoroutine(res):
                res = await res
        except Exception:
            logger.debug("design_vision.score_screenshot failed", exc_info=True)
            return None, []
        if not isinstance(res, dict):
            return None, []
        score = res.get("score")
        reasons = res.get("reasons") or []
        tells = res.get("generic_ai_tells") or []
        out_reasons: List[str] = []
        if isinstance(reasons, list):
            out_reasons.extend(str(r) for r in reasons[:5])
        if isinstance(tells, list) and tells:
            out_reasons.append("generic-AI tells: " + ", ".join(str(t) for t in tells[:5]))
        try:
            score_int: Optional[int] = int(score) if score is not None else None
        except (TypeError, ValueError):
            score_int = None
        return score_int, out_reasons

    @staticmethod
    def _playwright_renderable() -> bool:
        """True only when Playwright is importable AND chromium will launch.

        Cheap pre-check so the visual gate degrades to 'skipped' in CI/test
        environments without a browser binary — same contract as
        _render_smoke_test returning None.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return False
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            return True
        except Exception:
            return False

    @staticmethod
    def _locate_serve_dir(scaffold_dir: Path) -> Optional[Path]:
        """Find a directory containing an index.html to serve.

        Prefers a built output dir (dist/build/out/public), falling back to
        the scaffold root if it has an index.html. Returns None when there is
        nothing static to serve (e.g. a build that produced no output).
        """
        for candidate in ("dist", "build", "out", "public"):
            d = scaffold_dir / candidate
            if (d / "index.html").exists():
                return d
        if (scaffold_dir / "index.html").exists():
            return scaffold_dir
        # Some Vite/CRA builds nest one level deeper.
        for d in scaffold_dir.glob("*/"):
            if d.name == "node_modules":
                continue
            if (d / "index.html").exists():
                return d
        return None

    def _visual_gate_blocking(self, serve_dir: Path) -> Optional[Dict[str, Any]]:
        """Serve ``serve_dir`` on a local port, screenshot at both viewports,
        run heuristics + axe-core, and tear everything down.

        Runs synchronously inside asyncio.to_thread (the Playwright sync API
        cannot be reentered from a live event loop). Returns None when
        Playwright/chromium is unavailable (caller maps to 'skipped'); always
        shuts down the http server and browser, even on error.
        """
        import http.server
        import threading

        port = self._free_port()
        if port is None:
            return None

        handler_dir = str(serve_dir)

        class _Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, directory=handler_dir, **kwargs)

            def log_message(self, *_a: Any, **_kw: Any) -> None:  # silence
                return

        httpd: Optional[http.server.ThreadingHTTPServer] = None
        thread: Optional[threading.Thread] = None
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
            httpd.timeout = 1
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            serve_url = f"http://127.0.0.1:{port}/"

            shots_dir = serve_dir.parent / ".skyn3t_visual"
            try:
                shots_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                shots_dir = serve_dir

            desktop = self._visual_capture(
                serve_url, _VISUAL_VIEWPORTS["desktop"],
                screenshot_path=str(shots_dir / "desktop.png"),
                run_axe=True,
            )
            if desktop is None:
                return None  # playwright unavailable
            mobile = self._visual_capture(
                serve_url, _VISUAL_VIEWPORTS["mobile"],
                screenshot_path=str(shots_dir / "mobile.png"),
                run_axe=False,
            ) or {}

            heur = dict(desktop.get("heuristics") or {})
            # Horizontal overflow is the mobile-specific signal.
            heur["horizontal_overflow"] = bool(
                (mobile.get("heuristics") or {}).get("horizontal_overflow", False)
            )
            reasons: List[str] = []
            reasons.extend(desktop.get("reasons") or [])
            reasons.extend(mobile.get("reasons") or [])
            return {
                "desktop_screenshot": desktop.get("screenshot"),
                "mobile_screenshot": mobile.get("screenshot"),
                "heuristics": heur,
                "a11y_violations": int(desktop.get("a11y_violations") or 0),
                "reasons": reasons,
            }
        except Exception as exc:
            logger.debug("visual gate serve/capture failed", exc_info=True)
            return {
                "desktop_screenshot": None, "mobile_screenshot": None,
                "heuristics": {}, "a11y_violations": 0,
                "reasons": [f"visual capture error: {exc}"],
            }
        finally:
            if httpd is not None:
                try:
                    httpd.shutdown()
                except Exception:
                    pass
                try:
                    httpd.server_close()
                except Exception:
                    pass
            if thread is not None:
                try:
                    thread.join(timeout=2)
                except Exception:
                    pass

    @staticmethod
    def _free_port() -> Optional[int]:
        """Grab an ephemeral free localhost port. None on failure."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            return int(port)
        except Exception:
            return None

    @staticmethod
    def _visual_capture(
        serve_url: str,
        viewport: tuple[int, int],
        *,
        screenshot_path: Optional[str] = None,
        run_axe: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Navigate ``serve_url`` at ``viewport`` (w, h), screenshot it, and
        run cheap pixel/DOM heuristics + (optionally) axe-core.

        Returns None when Playwright/chromium is unavailable (the existing
        'skipped, don't penalize' contract). Otherwise returns:
            {
              'screenshot': Optional[str],
              'heuristics': {non_default_bg, distinct_colors, has_radius,
                             has_shadow, horizontal_overflow},
              'a11y_violations': int,
              'reasons': List[str],
            }
        Always tears down the browser.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return None
        width, height = viewport
        reasons: List[str] = []
        heuristics: Dict[str, Any] = {
            "non_default_bg": False, "distinct_colors": 0,
            "has_radius": False, "has_shadow": False, "horizontal_overflow": False,
        }
        a11y_violations = 0
        shot_out: Optional[str] = None
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(headless=True)
                except Exception as exc:
                    if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                        return None
                    return None
                try:
                    context = browser.new_context(viewport={"width": width, "height": height})
                    page = context.new_page()
                    try:
                        page.goto(serve_url, wait_until="networkidle", timeout=20000)
                    except Exception:
                        try:
                            page.goto(serve_url, wait_until="load", timeout=15000)
                        except Exception as exc:
                            reasons.append(f"goto failed: {exc}")
                    page.wait_for_timeout(400)
                    if screenshot_path:
                        try:
                            page.screenshot(path=screenshot_path, full_page=False)
                            shot_out = screenshot_path
                        except Exception:
                            shot_out = None
                    # DOM/pixel heuristics via in-page JS.
                    try:
                        metrics = page.evaluate(_VISUAL_HEURISTICS_JS)
                        if isinstance(metrics, dict):
                            heuristics["non_default_bg"] = bool(metrics.get("non_default_bg"))
                            heuristics["distinct_colors"] = int(metrics.get("distinct_colors") or 0)
                            heuristics["has_radius"] = bool(metrics.get("has_radius"))
                            heuristics["has_shadow"] = bool(metrics.get("has_shadow"))
                            heuristics["horizontal_overflow"] = bool(metrics.get("horizontal_overflow"))
                    except Exception as exc:
                        reasons.append(f"heuristics eval failed: {exc}")
                    # axe-core a11y (best-effort, optional).
                    if run_axe:
                        try:
                            a11y_violations = BuildVerifierAgent._inject_axe_count(page)
                        except Exception:
                            a11y_violations = 0
                finally:
                    browser.close()
        except Exception as exc:
            reasons.append(f"playwright session failed: {exc}")
        return {
            "screenshot": shot_out,
            "heuristics": heuristics,
            "a11y_violations": a11y_violations,
            "reasons": reasons,
        }

    @staticmethod
    def _inject_axe_count(page: Any) -> int:
        """Inject axe-core from the CDN and return the violation count.

        Best-effort: returns 0 if the CDN is unreachable or axe errors. Network
        is optional — a11y is a soft signal, never a hard block here.
        """
        try:
            page.add_script_tag(url="https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js")
        except Exception:
            return 0
        try:
            result = page.evaluate(
                """async () => {
                    if (typeof axe === 'undefined') return -1;
                    try {
                        const r = await axe.run(document, {resultTypes: ['violations']});
                        return (r && r.violations) ? r.violations.length : 0;
                    } catch (e) { return -1; }
                }"""
            )
            count = int(result)
            return count if count >= 0 else 0
        except Exception:
            return 0

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
