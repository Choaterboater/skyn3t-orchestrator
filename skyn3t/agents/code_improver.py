from __future__ import annotations

import asyncio
import contextlib
import difflib
import fcntl  # cortex audit 2026-06-13: cross-process git-apply lock
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.code_improver")

REPO_ROOT = Path(__file__).resolve().parents[2]   # /.../jarvis

# Serialize patch applies: each apply does a `git checkout -b` + `git apply`
# against a shared working tree, so concurrent applies (or an apply racing a
# manual checkout) would corrupt each other. A single module-level lock makes
# the whole branch/apply/checks/rollback sequence atomic per process.
_APPLY_LOCK = asyncio.Lock()


# cortex audit 2026-06-13: the asyncio.Lock above is PROCESS-LOCAL, so respawned
# / duplicate code_improver instances each got their own lock and serialized
# nothing across processes -> `cannot lock ref 'refs/heads/skyn3t/auto/...':
# reference already exists` collisions. Add a CROSS-PROCESS advisory file lock,
# keyed on the repo, so concurrent processes serialize their git-apply critical
# sections on the shared working tree.
_LOCK_DIR = REPO_ROOT / "data" / "locks"


def _repo_lock_path(repo_root: Path) -> Path:
    """Per-repo lock file under data/locks/. Distinct repos get distinct locks
    (so unrelated target repos don't serialize), while the same repo across
    processes shares one lock file."""
    key = hashlib.sha1(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return _LOCK_DIR / f"skyn3t-self-edit-{key}.lock"


@contextlib.contextmanager
def _cross_process_apply_lock(repo_root: Path):
    """Blocking advisory file lock (fcntl.flock) that serializes the git-apply
    critical section across PROCESSES for a given repo. Released on every exit
    path via try/finally. Best-effort: if the lock file can't be created/locked
    (e.g. unusual FS), we proceed without it rather than block the apply."""
    lock_path = _repo_lock_path(repo_root)
    fd = None
    try:
        os.makedirs(_LOCK_DIR, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        logger.exception("could not acquire cross-process apply lock at %s; "
                         "proceeding without it", lock_path)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass




def _prevalidate_diff(diff: str) -> Optional[str]:
    """Catch common LLM diff errors before hitting git apply.

    Returns an actionable error string (for LLM feedback), or None if OK.
    """
    lines = diff.splitlines(keepends=True)
    in_hunk = False
    hunk_start = 0
    for i, line in enumerate(lines, 1):
        stripped = line.rstrip("\n")
        if stripped.startswith("@@"):
            in_hunk = True
            hunk_start = i
            m = re.match(r"^@@\s*-(\d+)(?:(,\d+))?\s*\+(\d+)(?:(,\d+))?\s*@@", stripped)
            if not m:
                return (
                    f"Line {i}: malformed hunk header (need "
                    f"'@@ -start,count +start,count @@'): {stripped[:80]}"
                )
            expected_old = int(m.group(2) or "1")
            expected_new = int(m.group(4) or "1")
            actual_old = 0
            actual_new = 0
            for j in range(i, len(lines)):
                line = lines[j].rstrip("\n")
                if line.startswith("@@"):
                    break
                if line.startswith("-"):
                    actual_old += 1
                elif line.startswith("+"):
                    actual_new += 1
                else:
                    actual_old += 1
                    actual_new += 1
            if actual_old != expected_old or actual_new != expected_new:
                return (
                    f"Line {i}: hunk header claims {expected_old} old/{expected_new} "
                    f"new lines but actually has {actual_old} old/{actual_new} new "
                    f"lines. Fix the counts in: {stripped[:80]}"
                )
        elif stripped.startswith("--- ") or stripped.startswith("+++ "):
            in_hunk = False
        elif in_hunk and stripped and stripped[0] not in (" ", "-", "+"):
            return (
                f"Line {i}: unexpected character '{stripped[0]}' inside hunk "
                f"(started at line {hunk_start}). Hunk lines must start with "
                f"' ', '-', or '+'."
            )
    return None


class CodePatchApplyError(RuntimeError):
    """Raised by ``_apply_patch`` when the diff cannot be applied.

    Carries a structured ``info`` dict so the caller can recover the branch /
    rejected-diff path / error message without re-parsing the message.
    """
    def __init__(self, message: str, info: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.info: Dict[str, Any] = info or {}


class CodeImproverAgent(BaseAgent):
    """Reads recent failures + lessons; proposes a small targeted code change.

    Output: a Proposal(kind='code_patch') containing a unified diff. The apply
    handler is registered with the proposal store at startup (or on first execute):
    on approval, it creates a branch `skyn3t/auto/<id>`, applies the diff with
    `git apply`, runs `pytest`, and:
      - if tests pass:  leaves the branch in place and returns success.
      - if tests fail:  resets the branch (deletes it) and returns failure.

    For user-initiated runs (Studio frontend_redesign / studio_debug / studio_run
    or ``user_initiated=True``), ``execute()`` waits for the apply to actually
    happen, surfaces a structured ``{applied, branch, error, rejected_diff_path}``
    in TaskResult.output, and publishes a ``SYSTEM_ALERT`` event with
    ``kind=CODE_PATCH_RESULT`` so the dashboard can show a toast.
    """

    def __init__(self, name: str = "code_improver", *,
                 event_bus: Optional[EventBus] = None,
                 config: Optional[Dict[str, Any]] = None):
        super().__init__(name=name, agent_type="code_improver", provider="local",
                         event_bus=event_bus or EventBus(), config=config)
        self.add_capability(AgentCapability(
            name="code_improvement", description="proposes targeted code patches",
            parameters={"target_file": "str", "rationale": "str"}))
        self.add_capability(AgentCapability(
            name="self_modification", description="review-gated self-improvement",
            parameters={}))
        self._handler_registered = False

    async def initialize(self) -> None:
        self._register_handler()
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        # treat git availability as the health signal
        try:
            r = await asyncio.to_thread(subprocess.run, ["git", "--version"],
                                         capture_output=True, timeout=3)
            return r.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _effective_repo_root(value: Any) -> Path:
        try:
            if value:
                return Path(str(value)).expanduser().resolve()
        except Exception:
            pass
        return REPO_ROOT.resolve()

    @staticmethod
    def _resolve_target_path(repo_root: Path, target_file: str) -> Optional[Path]:
        if not target_file:
            return None
        try:
            path = (repo_root / target_file).resolve()
            path.relative_to(repo_root.resolve())
        except Exception:
            return None
        if not path.exists() or not path.is_file():
            return None
        return path

    @staticmethod
    def _relative_target(repo_root: Path, path: Path) -> str:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()

    @staticmethod
    def _proposal_dir(repo_root: Path) -> Path:
        base_root = REPO_ROOT.resolve()
        if repo_root.resolve() == base_root:
            return base_root / "data" / "proposals" / "code"
        return repo_root / ".skyn3t" / "proposals" / "code"

    @staticmethod
    def _normalize_review_risks(risks: List[str]) -> List[str]:
        from skyn3t.cortex.review_utils import normalize_review_risks

        return normalize_review_risks(risks)

    @classmethod
    def _review_risks_from_rationale(cls, rationale: str) -> List[str]:
        if "Risks to address:" not in rationale:
            return []
        risks: List[str] = []
        in_risks = False
        for raw_line in rationale.splitlines():
            line = raw_line.strip()
            lower = line.lower()
            if lower.startswith("risks to address:"):
                in_risks = True
                continue
            if not in_risks:
                continue
            if line.startswith("- "):
                risks.append(line[2:].strip())
                continue
            if line:
                break
        return cls._normalize_review_risks(risks)

    @staticmethod
    def _run_check_commands(
        repo_root: Path,
        commands: List[Tuple[List[str], str]],
        *,
        timeout: int = 1200,
        idle_timeout: int = 180,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run check commands with a streaming-idle timeout.

        Mirrors the two-tier timeout from ``LLMClient._run_capture``:
        * HARD: ``timeout`` is the absolute wall-clock cap per command.
        * IDLE: ``idle_timeout`` kills a command that emits no bytes on
          stdout+stderr for that long. Catches genuine hangs without
          punishing slow-but-working processes (npm install on a large
          tree, pytest on a big suite, etc.).

        Hard default 1200s + idle 180s matches the established contract
        for CLI subprocess calls (see user memory
        feedback_cli_timeout_streaming.md). Never go back to a flat
        ``subprocess.run(..., timeout=180)`` — that killed real builds
        whenever a step took >3 min, even when it was making progress.
        """
        stdout_parts: List[str] = []
        stderr_parts: List[str] = []
        executed: List[str] = []
        for args, display in commands:
            try:
                stdout, stderr, returncode = CodeImproverAgent._stream_capture(
                    args=args,
                    cwd=str(repo_root),
                    hard_timeout=timeout,
                    idle_timeout=idle_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                # Surface a TimeoutExpired with the partial output so
                # callers (and tests) keep the existing exception
                # contract.
                raise subprocess.TimeoutExpired(
                    cmd=args,
                    timeout=exc.timeout,
                    output=exc.output,
                    stderr=exc.stderr,
                )
            executed.append(display)
            stdout_parts.append(f"$ {display}\n{stdout}".rstrip())
            stderr_parts.append(f"$ {display}\n{stderr}".rstrip())
            if returncode != 0:
                return {
                    "ran": True,
                    "ok": False,
                    "command": " && ".join(executed),
                    "stdout": "\n\n".join(part for part in stdout_parts if part),
                    "stderr": "\n\n".join(part for part in stderr_parts if part),
                    "note": note,
                }
        return {
            "ran": bool(executed),
            "ok": True,
            "command": " && ".join(executed) if executed else None,
            "stdout": "\n\n".join(part for part in stdout_parts if part),
            "stderr": "\n\n".join(part for part in stderr_parts if part),
            "note": note,
        }

    @staticmethod
    def _stream_capture(
        *,
        args: List[str],
        cwd: str,
        hard_timeout: float,
        idle_timeout: float,
    ) -> Tuple[str, str, int]:
        """Run a subprocess with streaming-idle timeout. Returns
        ``(stdout, stderr, returncode)``.

        Uses ``Popen`` + per-stream reader threads so we can observe
        progress without blocking. The hard timeout caps total wall
        time; the idle timeout fires if neither stream has produced
        any new bytes for that long. Either kind of timeout raises
        ``subprocess.TimeoutExpired`` carrying the partial output.
        """
        import threading

        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            text=True,
            bufsize=1,  # line-buffered, so progress is observable
        )

        stdout_chunks: List[str] = []
        stderr_chunks: List[str] = []
        progress_lock = threading.Lock()
        last_progress = time.monotonic()

        def _drain(stream, sink: List[str]) -> None:
            nonlocal last_progress
            try:
                for line in iter(stream.readline, ""):
                    sink.append(line)
                    with progress_lock:
                        last_progress = time.monotonic()
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(
            target=_drain, args=(proc.stdout, stdout_chunks), daemon=True
        )
        t_err = threading.Thread(
            target=_drain, args=(proc.stderr, stderr_chunks), daemon=True
        )
        t_out.start()
        t_err.start()

        start = time.monotonic()
        # Poll every 0.5s — granular enough that an idle timeout fires
        # close to its nominal value, light enough that the loop
        # itself doesn't burn CPU on a long-running healthy process.
        poll_interval = 0.5
        timeout_kind: Optional[str] = None
        while True:
            if proc.poll() is not None:
                break
            now = time.monotonic()
            if now - start >= hard_timeout:
                timeout_kind = f"hard timeout after {int(now - start)}s"
                break
            with progress_lock:
                idle_for = now - last_progress
            if idle_for >= idle_timeout:
                timeout_kind = f"idle timeout ({int(idle_timeout)}s no output)"
                break
            time.sleep(poll_interval)

        if timeout_kind is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            # Drain whatever the threads collected before raising.
            t_out.join(timeout=2.0)
            t_err.join(timeout=2.0)
            raise subprocess.TimeoutExpired(
                cmd=args,
                timeout=hard_timeout,
                output="".join(stdout_chunks),
                stderr="".join(stderr_chunks) + f"\n[{timeout_kind}]",
            )

        # Process exited on its own — let the reader threads finish
        # flushing the pipes before we report.
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)
        return "".join(stdout_chunks), "".join(stderr_chunks), proc.returncode

    @staticmethod
    def _node_package_manager(repo_root: Path, package_data: Dict[str, Any]) -> Optional[str]:
        declared = str(package_data.get("packageManager") or "").strip().lower()
        for name in ("pnpm", "yarn", "npm"):
            if declared.startswith(name) and shutil.which(name):
                return name
        lockfiles = (
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("package-lock.json", "npm"),
        )
        for lockfile, manager in lockfiles:
            if (repo_root / lockfile).exists() and shutil.which(manager):
                return manager
        for manager in ("pnpm", "yarn", "npm"):
            if shutil.which(manager):
                return manager
        return None

    @staticmethod
    def _node_script_command(manager: str, script: str) -> Tuple[List[str], str]:
        if manager == "npm":
            if script == "test":
                return (["npm", "test"], "npm test")
            return (["npm", "run", script], f"npm run {script}")
        return ([manager, script], f"{manager} {script}")

    @staticmethod
    def _run_repo_checks(repo_root: Path) -> Dict[str, Any]:
        markers = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.py", "requirements.txt")
        has_python_checks = any((repo_root / marker).exists() for marker in markers) or (
            repo_root / "tests"
        ).exists()
        if has_python_checks:
            return CodeImproverAgent._run_check_commands(
                repo_root,
                [(["python3", "-m", "pytest", "-q", "--tb=line"], "python3 -m pytest -q --tb=line")],
            )

        package_json = repo_root / "package.json"
        if package_json.exists():
            try:
                package_data = json.loads(package_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                package_data = {}
            scripts = package_data.get("scripts")
            if not isinstance(scripts, dict):
                scripts = {}
            manager = CodeImproverAgent._node_package_manager(repo_root, package_data)
            if manager is None:
                return {
                    "ran": False,
                    "ok": False,
                    "command": None,
                    "stdout": "",
                    "stderr": "",
                    "note": "package.json detected but no compatible package manager is installed",
                }
            commands = []
            for script in ("test", "lint", "build"):
                script_value = scripts.get(script)
                if isinstance(script_value, str) and script_value.strip():
                    commands.append(CodeImproverAgent._node_script_command(manager, script))
            if commands:
                return CodeImproverAgent._run_check_commands(
                    repo_root,
                    commands,
                    timeout=240,
                    note=f"validated Node repo with {manager}",
                )
            return {
                "ran": False,
                "ok": False,
                "command": None,
                "stdout": "",
                "stderr": "",
                "note": "package.json detected but no test, lint, or build script is available",
            }

        if (repo_root / "go.mod").exists():
            return CodeImproverAgent._run_check_commands(
                repo_root,
                [(["go", "test", "./..."], "go test ./...")],
                timeout=240,
            )

        if (repo_root / "Cargo.toml").exists():
            return CodeImproverAgent._run_check_commands(
                repo_root,
                [(["cargo", "test"], "cargo test")],
                timeout=240,
            )

        return {
            "ran": False,
            "ok": True,
            "command": None,
            "stdout": "",
            "stderr": "",
            "note": "no repo validation command detected",
        }

    def _register_handler(self) -> None:
        if self._handler_registered:
            return
        try:
            from skyn3t.cortex import get_store
            get_store().register_handler("code_patch", self._apply_patch)
            self._handler_registered = True
        except Exception:
            logger.exception("could not register code_patch handler")

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        if hasattr(self, "think"):
            try:
                await self.think("code_improver scanning")
            except Exception:
                logger.debug("think() failed at code_improver scan start", exc_info=True)
        self._register_handler()  # idempotent

        input_data = task.input_data or {}
        target = input_data.get("target_file")  # optional explicit override
        rationale = input_data.get("rationale", "")
        diff = input_data.get("diff")  # optional pre-built diff
        repo_root = self._effective_repo_root(input_data.get("repo_root"))

        # Skip cleanly when no target was specified — the brief didn't ask for
        # a code change. Don't invent a target or file an empty diff.
        if not target and not diff:
            ui = (input_data.get("intent") in {"frontend_redesign", "studio_run", "rewrite"}
                  or input_data.get("user_initiated"))
            try:
                self._publish_result(applied=False, branch=None,
                                      error="code stage skipped — no target file in brief",
                                      target="(none)", user_initiated=bool(ui))
            except Exception:
                pass
            return TaskResult(
                task_id=task.task_id, success=True,
                output={"proposed": False, "skipped": True,
                        "reason": "no target_file in brief; code stage not applicable",
                        "applied": False, "branch": None, "error": None,
                        "rejected_diff_path": None,
                        "summary": "Code stage skipped: brief didn't specify a target file."})

        # Detect intent up-front so we can pick the right LLM mode (minimal-fix
        # vs sweeping rewrite) before we draft the patch.
        intent = (input_data or {}).get("intent", "")
        # rewrite mode: large-scale changes encouraged
        mode = "rewrite" if intent in {"frontend_redesign", "studio_run", "rewrite"} else "minimal"
        # If we're being called FROM a studio_debug handler approval, this is a
        # recursive retry — don't file ANOTHER studio_debug proposal on failure.
        from_studio_debug = intent == "studio_debug"
        if from_studio_debug:
            review_risks = self._normalize_review_risks(input_data.get("review_risks") or [])
            if not review_risks:
                review_risks = self._review_risks_from_rationale(rationale)
            if not review_risks:
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={
                        "proposed": False,
                        "skipped": True,
                        "reason": "review flagged no actionable risks",
                        "applied": False,
                        "branch": None,
                        "error": None,
                        "rejected_diff_path": None,
                        "summary": "Code patch skipped: reviewer did not identify actionable risks.",
                    },
                )

        # ─── MCP-mode path ──────────────────────────────────────────────
        # When the caller hasn't pre-supplied a diff, try the MCP tool-loop
        # first: the LLM directly calls read_file/apply_replacement/git_*
        # tools to make the change, instead of emitting a fragile unified
        # diff. Falls back to the diff-mode path below if MCP fails.
        use_mcp = bool(input_data.get("use_mcp", True)) and repo_root == REPO_ROOT.resolve()
        if use_mcp and target and rationale and not diff:
            try:
                from skyn3t.adapters.mcp_client import MCPDriver
                from skyn3t.adapters.mcp_tools import TOOLS
                client = self.get_llm() if hasattr(self, "get_llm") else None
                if client is None:
                    from skyn3t.adapters import LLMClient
                    client = LLMClient(default_model=self.config.get("model"),
                                       backend=self.config.get("backend"),
                                       event_bus=self.event_bus, caller_name=self.name)
                driver = MCPDriver(llm_client=client, tools=TOOLS, max_rounds=8)
                # Honest success check: snapshot target hash so the driver can
                # verify the LLM actually changed the file (not just claimed DONE).
                if target:
                    try:
                        driver.set_target_for_change_check(target)
                    except Exception:
                        logger.exception("set_target_for_change_check failed")
                import time as _t
                mcp_branch = f"skyn3t/auto/mcp-{int(_t.time())}"
                goal = (
                    f"Make the change requested below in a NEW branch.\n\n"
                    f"Target file: {target}\n"
                    f"Branch name to create: {mcp_branch}\n"
                    f"Change: {rationale}\n\n"
                    f"Steps:\n"
                    f"1. read_file({target!r}) to see current contents.\n"
                    f"2. git_branch({mcp_branch!r}) to create the working branch.\n"
                    f"3. Use apply_replacement (preferred — exact match) or write_file "
                    f"(full replace) to make the change.\n"
                    f"4. run_pytest() to confirm nothing is broken.\n"
                    f"5. git_commit('chore(auto): <one-line summary>').\n"
                    f"6. Reply DONE.\n\n"
                    f"If apply_replacement fails because the find string doesn't appear, "
                    f"re-read the file and try a different approach. Don't invent text."
                )
                result = await driver.run(goal=goal)
                if result.get("ok"):
                    self._publish_result(applied=True, branch=mcp_branch, error=None,
                                         target=target, user_initiated=True)
                    return TaskResult(
                        task_id=task.task_id, success=True,
                        output={
                            "applied": True, "branch": mcp_branch, "mode": "mcp",
                            "rounds": result.get("rounds"), "error": None,
                            "rejected_diff_path": None,
                            "target": target,
                        },
                    )
                # MCP claimed success but didn't actually change anything — fall through.
                mcp_err = result.get("error", "MCP path did not produce changes")
                logger.info("MCP path didn't apply: %s; falling back to diff mode", mcp_err)
            except Exception:
                logger.exception("MCP path failed; falling back to diff mode")

        if diff and target:
            patch_text = diff
        else:
            target, patch_text, rationale = await self._draft_patch(
                target=target, rationale=rationale, repo_root=repo_root, mode=mode)

        # When the caller marked this run as user-initiated (Studio project, REPL, etc.),
        # skip the approval modal and auto-apply to a branch.
        # NOTE: studio_debug intent is NOT user-initiated — it's the cortex's response
        # to a Reviewer flag, which is a *system* suggestion. We keep auto-approval ONLY
        # for user-driven Studio runs.
        user_initiated = intent in {"frontend_redesign", "studio_run"} \
                         or (input_data or {}).get("user_initiated", False)

        if not patch_text or not target:
            # Nothing actionable. Still emit a toast for user-initiated runs so it's
            # never silent.
            attempts = list(getattr(self, "_last_attempts", []) or [])

            # If the LLM was actually tried and every backend failed, file a
            # studio_debug proposal so the swarm can self-analyze why we can't
            # produce a working diff for this target.
            # SKIP filing if we're already inside a studio_debug retry — that
            # would create an infinite loop (failed retry → new debug proposal
            # → approve → fail → new debug → ...).
            if attempts and not from_studio_debug and target:
                try:
                    from skyn3t.cortex import get_store
                    attempts_summary = "\n".join(
                        f"- {a.get('backend','?')}/{a.get('model','?')} "
                        f"attempt {a.get('attempt',0)}: {a.get('error','?')[:160]}"
                        for a in attempts
                    )
                    get_store().create(
                        kind="studio_debug",
                        title=f"All LLM attempts failed for {target}",
                        summary=(
                            f"Could not produce a valid unified diff for "
                            f"`{target}` after {len(attempts)} attempts."
                        ),
                        detail=(
                            f"## Target\n`{target}`\n\n"
                            f"## Rationale\n{rationale}\n\n"
                            f"## Attempts\n{attempts_summary}\n\n"
                            f"_Possible causes: target file structure is unusual, "
                            f"rationale is ambiguous, or model isn't matching "
                            f"unified-diff format. Consider rephrasing the brief or "
                            f"breaking it into smaller changes._"
                        ),
                        payload={
                            "target_file": target,
                            "rationale": rationale,
                            "attempts": attempts,
                        },
                        source="code_improver",
                        requires_approval=True,
                    )
                except Exception:
                    logger.exception("could not file studio_debug proposal")
            elif attempts and not from_studio_debug:
                logger.info(
                    "skipping studio_debug proposal because diff drafting ended without a target"
                )

            self._publish_result(applied=False, branch=None,
                                  error="no actionable diff produced", target=target or "(unknown)",
                                  user_initiated=bool(user_initiated))
            success = not bool(attempts)
            return TaskResult(task_id=task.task_id, success=success,
                              error=None if success else "no actionable diff produced",
                              output={"proposed": False, "reason": "no actionable diff",
                                      "applied": False, "branch": None,
                                      "error": "no actionable diff produced",
                                      "rejected_diff_path": None,
                                      "attempts": attempts})

        # File the proposal
        try:
            from skyn3t.cortex import get_store
        except Exception as e:
            self._publish_result(applied=False, branch=None,
                                  error=f"proposal store unavailable: {e}",
                                  target=target, user_initiated=bool(user_initiated))
            return TaskResult(task_id=task.task_id, success=False,
                              error=f"proposal store unavailable: {e}",
                              output={"applied": False, "branch": None,
                                      "error": f"proposal store unavailable: {e}",
                                      "rejected_diff_path": None})

        # We always create the proposal with requires_approval=True so the store
        # does NOT kick off its fire-and-forget background _auto_apply task. If
        # this is user-initiated we then call approve() ourselves and await the
        # actual result, so we can surface success/failure synchronously.
        proposal = get_store().create(
            kind="code_patch",
            title=f"Patch {target}",
            summary=rationale or "auto-generated improvement proposal",
            detail=(
                f"Target: `{target}`\n\n"
                f"Repo root: `{repo_root}`\n\n"
                f"Reason: {rationale}\n\n```diff\n{patch_text}\n```"
            ),
            payload={"target_file": target, "patch": patch_text, "rationale": rationale,
                     "user_initiated": user_initiated, "repo_root": str(repo_root)},
            source="code_improver",
            requires_approval=True,
        )

        if not user_initiated:
            # Background / proposal-only path — apply happens later via the
            # approval modal. Return now.
            return TaskResult(task_id=task.task_id, success=True,
                              output={"proposed": True, "proposal_id": proposal.id,
                                      "target": target,
                                      "applied": False, "branch": None,
                                      "error": None, "rejected_diff_path": None})

        # User-initiated: drive the apply ourselves so we can await the outcome.
        applied: bool = False
        branch: Optional[str] = None
        err: Optional[str] = None
        rejected_dir: Optional[str] = None
        try:
            approve_res = await get_store().approve(proposal.id)
        except Exception as e:
            approve_res = {"ok": False, "error": f"approve raised: {e}"}

        if approve_res.get("ok"):
            inner = approve_res.get("result") or {}
            applied = bool(inner.get("applied"))
            branch_value = inner.get("branch")
            branch = str(branch_value) if branch_value is not None else None
            err = inner.get("error") if not applied else None
            rejected_dir = inner.get("rejected_diff_path")
        else:
            # approve() caught an exception inside _apply_patch — recover the
            # structured info we stashed on the agent instance.
            err = approve_res.get("error") or "apply failed"
            info = getattr(self, "_last_apply_info", {}) or {}
            rejected_dir = info.get("rejected_diff_path")

        self._publish_result(applied=applied, branch=branch, error=err,
                              target=target, user_initiated=True)

        return TaskResult(
            task_id=task.task_id,
            success=applied,
            error=None if applied else (err or "apply failed"),
            output={
                "proposed": True,
                "proposal_id": proposal.id,
                "target": target,
                "applied": applied,
                "branch": branch,
                "error": err,
                "rejected_diff_path": rejected_dir,
            },
        )

    def _publish_result(self, *, applied: bool, branch: Optional[str],
                          error: Optional[str], target: str,
                          user_initiated: bool) -> None:
        """Publish a SYSTEM_ALERT carrying CODE_PATCH_RESULT so the dashboard
        can render a toast notification.
        """
        try:
            from skyn3t.core.events import Event, EventType
            self.event_bus.publish(Event(
                event_type=EventType.SYSTEM_ALERT,
                source="code_improver",
                payload={
                    "kind": "CODE_PATCH_RESULT",
                    "applied": bool(applied),
                    "branch": branch,
                    "error": error,
                    "target": target,
                    "user_initiated": bool(user_initiated),
                },
            ))
        except Exception:
            logger.exception("publish CODE_PATCH_RESULT failed")

    async def _draft_patch(self, *, target: Optional[str], rationale: str,
                               repo_root: Path,
                               mode: str = "minimal") -> tuple[Optional[str], Optional[str], str]:
        """Draft a patch. If the caller supplied an explicit ``target`` + ``rationale``,
        try the LLM first. Falls back to a deterministic, heuristic transform that
        looks for ``datetime.utcnow()`` / TODO / FIXME markers in ``skyn3t/`` and
        produces a tiny clean-up patch. Returns ``(target, diff, rationale)``.

        ``mode`` selects between minimal-change (default) and rewrite (large
        sweeping redesign) prompting.
        """
        # 1. LLM-driven path: only when the user gave a specific file + rationale.
        if target and rationale:
            llm_diff = await self._llm_draft(target, rationale, repo_root=repo_root, mode=mode)
            if llm_diff:
                return target, llm_diff, rationale

        # 2. Deterministic fallback (unchanged behavior).
        candidates: List[Path] = []
        if target:
            p = self._resolve_target_path(repo_root, target)
            if p is not None:
                candidates = [p]
        else:
            if repo_root.resolve() != REPO_ROOT.resolve():
                return None, None, rationale or "no focus file selected for the target repo"
            for p in (REPO_ROOT / "skyn3t").rglob("*.py"):
                try:
                    text = p.read_text(encoding="utf-8")
                except Exception:
                    continue
                if "datetime.utcnow()" in text or "TODO" in text or "FIXME" in text:
                    candidates.append(p)
            candidates = candidates[:1]
        if not candidates:
            return None, None, rationale or "nothing to improve"
        target_path = candidates[0]
        try:
            text = target_path.read_text(encoding="utf-8")
        except Exception:
            return None, None, "could not read target"
        new_text = text
        # safe deterministic transform: utcnow() → now(timezone.utc)
        if "datetime.utcnow()" in new_text:
            new_text = new_text.replace("datetime.utcnow()", "datetime.now(timezone.utc)")
            if "from datetime import" in new_text and "timezone" not in new_text.split("from datetime import",1)[1].split("\n",1)[0]:
                new_text = new_text.replace(
                    "from datetime import datetime", "from datetime import datetime, timezone", 1)
            rationale = rationale or "replace deprecated datetime.utcnow() with timezone-aware now()"
        if new_text == text:
            return None, None, rationale or "no safe transform applied"
        rel = self._relative_target(repo_root, target_path)
        diff = "".join(difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        return rel, diff, rationale

    async def _llm_draft(self, target_file: str, rationale: str, *,
                            repo_root: Path,
                            mode: str = "minimal") -> Optional[str]:
        """Ask the LLM for a unified diff that addresses ``rationale`` on ``target_file``.

        Tries the configured backend/model first (3 attempts), then falls back
        through copilot_cli/gpt-5.3-codex, claude_cli/sonnet, and claude_cli/opus
        (1 attempt each). Records every attempt on ``self._last_attempts`` so
        ``execute()`` can surface it / file a studio_debug proposal if everything
        fails. Returns a diff string suitable for ``git apply``, or ``None``.

        ``mode`` is forwarded to the per-backend retry loop so the system prompt
        can switch between minimal-fix and rewrite framings.
        """
        self._last_attempts: List[Dict[str, str]] = []
        try:
            path = self._resolve_target_path(repo_root, target_file)
            if path is None:
                return None

            diff, attempts = await self._llm_draft_with_fallback(
                target_file=target_file,
                rationale=rationale,
                repo_root=repo_root,
                mode=mode,
            )
            self._last_attempts = attempts
            return diff
        except Exception:
            logger.exception("_llm_draft failed")
            return None

    def _system_prompt(self, mode: str) -> str:
        """Build the system prompt for the LLM based on ``mode``.

        ``mode == "rewrite"`` permits/encourages large sweeping diffs (used for
        redesigns); anything else clamps to the SMALLEST possible change.
        """
        base = (
            "You are CodeImproverAgent. Output a STRICT unified diff that applies cleanly with `git apply`. "
            "REQUIREMENTS: "
            "- Single fenced ```diff block, nothing else. "
            "- Exactly two header lines: `--- a/<path>` and `+++ b/<path>`. "
            "- Hunk headers MUST include line counts: `@@ -<start>,<count> +<start>,<count> @@` (NOT bare `@@`). "
            "- Include 3 lines of unchanged context above and below each change so the patch anchors. "
            "- The path MUST be exactly: <target>. Don't invent files."
        )
        if mode == "rewrite":
            return base + (
                " You are doing a REDESIGN, not a bug fix. The diff CAN and SHOULD be large — "
                "rewrite entire blocks (CSS palettes, component styles, whole sections) where it serves "
                "the rationale. DO NOT minimize the change just for safety. Replace the requested "
                "block FULLY with the new design. Preserve all DOM IDs, JS handlers, and structural "
                "attributes — only style/visual content should change unless the brief says otherwise."
                " Think step-by-step:"
                " 1. What's the structural intent of the brief?"
                " 2. Which sections of the file map to that intent?"
                " 3. Plan the rewrite scope — a contiguous block, multiple non-contiguous blocks, or full replacement."
                " 4. Construct the diff with proper line numbers."
                " After reasoning, output ONLY the diff."
            )
        return base + (
            " Make the SMALLEST possible change that addresses the issue. "
            "Preserve all existing behavior unless the rationale says otherwise. Do not invent files or add unrelated edits."
            " Think step-by-step BEFORE writing the diff:"
            " 1. Identify the exact lines that need to change."
            " 2. Note the line numbers (count from 1)."
            " 3. Plan the hunk: a/path, +/path, then `@@ -<start>,<count> +<start>,<count> @@`."
            " 4. Include 3 lines of unchanged context above and below."
            " 5. Verify mentally that `git apply` would accept it."
            " After reasoning, output ONLY the fenced ```diff block — your reasoning stays internal."
        )

    async def _self_consistency_draft(self, *, target_file: str, rationale: str,
                                        repo_root: Path,
                                        mode: str, backend: Optional[str],
                                        model: Optional[str], n: int = 3) -> Optional[str]:
        """Sample N diffs in parallel at varied temperatures; return first one that
        passes ``git apply --check``. None if all fail.

        Catches the "almost-valid diff" failure mode (model produces something
        plausible but with off-by-one line counts) more reliably than retrying
        sequentially with error feedback — three independent samples at spread
        temperatures are likely to include at least one that lands cleanly.
        """
        try:
            from skyn3t.adapters import LLMClient
            path = self._resolve_target_path(repo_root, target_file)
            if path is None:
                return None
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                return None
            if len(text) > 200_000:
                text = text[:200_000] + "\n…[truncated]…"
            rel = self._relative_target(repo_root, path)
            system = self._system_prompt(mode).replace("<target>", rel)
            prompt = (
                f"Target file: `{rel}`\n"
                f"Rationale (what to change and why):\n{rationale}\n\n"
                f"Current file contents:\n```\n{text}\n```\n\n"
                f"Reply ONLY with a fenced ```diff block."
            )

            async def _one(temp: float) -> Optional[str]:
                try:
                    client = LLMClient(default_model=model, backend=backend,
                                       event_bus=self.event_bus, caller_name=self.name)
                    out = await client.complete(prompt, system=system,
                                                  max_tokens=12000 if mode == "rewrite" else 8000,
                                                  temperature=temp)
                    return self._extract_diff(out, rel)
                except Exception:
                    return None

            # Spread temperatures so the samples actually diverge
            temps = [0.2, 0.4, 0.6][:n]
            if hasattr(self, "think"):
                try:
                    await self.think(f"self-consistency: {n} parallel samples ({backend}/{model})")
                except Exception:
                    pass
            diffs = await asyncio.gather(*[_one(t) for t in temps], return_exceptions=False)
            # First valid diff wins
            for d in diffs:
                if not d:
                    continue
                ok, _err = self._validate_diff_with_git_check(d, repo_root=repo_root)
                if ok:
                    return d
            return None
        except Exception:
            logger.exception("self-consistency failed")
            return None

    async def _llm_draft_with_backend(self, *, target_file: str, rationale: str,
                                        repo_root: Path,
                                        backend: Optional[str], model: Optional[str],
                                        max_attempts: int = 3,
                                        mode: str = "minimal") -> Tuple[Optional[str], List[Dict[str, str]]]:
        """Try to draft a valid diff using the given backend/model.

        Returns ``(diff, attempts_log)`` — ``diff`` is ``None`` if all
        ``max_attempts`` failed git apply --check. ``attempts_log`` is a list of
        ``{backend, model, attempt, error}`` dicts (success entry omitted).

        ``mode`` selects the system-prompt framing (minimal-fix vs rewrite).
        """
        attempts_log: List[Dict[str, str]] = []
        try:
            path = self._resolve_target_path(repo_root, target_file)
            if path is None:
                return None, attempts_log
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                return None, attempts_log
            if len(text) > 200_000:
                text = text[:200_000] + "\n…[truncated for LLM context window]…"

            try:
                from skyn3t.adapters import LLMClient
                client = LLMClient(default_model=model, backend=backend)
            except Exception as e:
                attempts_log.append({
                    "backend": str(backend), "model": str(model),
                    "attempt": "0",
                    "error": f"could not construct LLMClient: {e}",
                })
                return None, attempts_log

            rel = self._relative_target(repo_root, path)
            system = self._system_prompt(mode).replace("<target>", rel)

            base_prompt = (
                f"Target file: `{rel}`\n"
                f"Rationale (what to change and why):\n{rationale}\n\n"
                f"Current file contents:\n```\n{text}\n```\n\n"
                f"Reply ONLY with a fenced ```diff block containing the unified diff."
            )
            try:
                from skyn3t.adapters.few_shot import few_shot_block
                shots = few_shot_block("code_diff", count=2)
                if shots:
                    base_prompt = shots + "\n\n# Now the new task:\n" + base_prompt
            except Exception:
                logger.exception("few_shot_block(code_diff) failed; continuing without shots")

            # Self-consistency: try N parallel samples at spread temperatures
            # FIRST. Catches the "almost-valid diff" failure mode much more
            # reliably than the sequential retry-with-feedback loop below.
            sc_diff = await self._self_consistency_draft(
                target_file=target_file, rationale=rationale, repo_root=repo_root, mode=mode,
                backend=backend, model=model, n=3)
            if sc_diff:
                return sc_diff, []   # empty attempts log; we succeeded on first volley

            last_error = ""
            for attempt in range(1, max_attempts + 1):
                prompt = base_prompt
                if attempt > 1 and last_error:
                    prompt = (
                        base_prompt
                        + f"\n\nPrevious attempt failed `git apply --check` with: {last_error}\n"
                        + "Try again, fix the line counts, ensure 3 lines of context."
                    )
                try:
                    out = await client.complete(
                        prompt, system=system,
                        max_tokens=8000 if mode == "rewrite" else 4000,
                        temperature=0.1,
                    )
                except Exception as e:
                    logger.exception("_llm_draft_with_backend: client.complete raised "
                                      "(backend=%s model=%s attempt=%d)", backend, model, attempt)
                    last_error = f"client.complete raised: {e}"
                    attempts_log.append({
                        "backend": str(backend), "model": str(model),
                        "attempt": str(attempt), "error": last_error[:300],
                    })
                    # client.complete blew up — no point retrying same backend
                    return None, attempts_log

                diff = self._extract_diff(out, rel)
                if diff is None:
                    last_error = "no fenced diff block / no @@ hunk header in response"
                    logger.warning("_llm_draft attempt %d (%s/%s): %s",
                                    attempt, backend, model, last_error)
                    attempts_log.append({
                        "backend": str(backend), "model": str(model),
                        "attempt": str(attempt), "error": last_error,
                    })
                    continue

                ok, gerr = self._validate_diff_with_git_check(diff, repo_root=repo_root)
                if ok:
                    # success → don't append a log entry for this attempt
                    return diff, attempts_log
                last_error = gerr or "git apply --check rejected the diff"
                logger.warning("_llm_draft attempt %d (%s/%s) rejected by git apply --check: %s",
                                attempt, backend, model, last_error)
                attempts_log.append({
                    "backend": str(backend), "model": str(model),
                    "attempt": str(attempt), "error": last_error[:300],
                })

            return None, attempts_log
        except Exception as e:
            logger.exception("_llm_draft_with_backend failed (backend=%s model=%s)", backend, model)
            attempts_log.append({
                "backend": str(backend), "model": str(model),
                "attempt": "0", "error": f"unexpected: {e}"[:300],
            })
            return None, attempts_log

    # Default fallback chain. Each entry is (backend, model). Override via
    # config["fallback_chain"] to suit a specific deployment. Any entry whose
    # (backend, model) is not present in the live model catalog is silently
    # skipped at runtime to avoid spamming errors for nonexistent models.
    DEFAULT_FALLBACK_CHAIN: List[Tuple[str, str]] = [
        ("claude_cli", "sonnet"),
        ("claude_cli", "opus"),
        ("openai_cli", "gpt-5"),
    ]

    async def _llm_draft_with_fallback(self, *, target_file: str,
                                          rationale: str,
                                          repo_root: Path,
                                          mode: str = "minimal") -> Tuple[Optional[str], List[Dict[str, str]]]:
        """Multi-backend fallback chain. Returns ``(diff, all_attempts_log)``.

        Order:
          1. Configured backend/model — 3 attempts.
          2. Each entry from ``config["fallback_chain"]`` (or
             ``DEFAULT_FALLBACK_CHAIN``) that is present in the live model
             catalog — 1 attempt each.

        Entries not registered in the catalog are silently dropped so we don't
        waste time on models the deployment can't actually call.
        """
        chain: List[Tuple[Optional[str], Optional[str], int]] = []
        primary_backend = None
        primary_model = None
        try:
            primary_backend = self.config.get("backend")
            primary_model = self.config.get("model")
        except Exception:
            pass
        chain.append((primary_backend, primary_model, 3))

        configured_chain = []
        try:
            configured_chain = list(self.config.get("fallback_chain") or [])
        except Exception:
            configured_chain = []
        raw_fallbacks: List[Tuple[str, str]] = []
        for entry in configured_chain:
            try:
                be, mdl = entry
                raw_fallbacks.append((str(be), str(mdl)))
            except Exception:
                continue
        if not raw_fallbacks:
            raw_fallbacks = list(self.DEFAULT_FALLBACK_CHAIN)

        # Filter out entries whose model isn't registered in the live catalog
        # for that backend; spares retries on models that can't run anyway.
        try:
            from skyn3t.adapters.model_catalog import list_models
        except Exception:
            list_models = None  # type: ignore[assignment]
        validated: List[Tuple[str, str]] = []
        if list_models is not None:
            for be, mdl in raw_fallbacks:
                try:
                    items = await list_models(be)
                    names = {str(it.get("id") or it.get("name") or "") for it in items}
                    if not names or mdl in names or any(n.endswith("/" + mdl) or n.startswith(mdl) for n in names):
                        validated.append((be, mdl))
                except Exception:
                    # If we can't reach the catalog, fall back to including the
                    # entry rather than silently dropping the entire chain.
                    validated.append((be, mdl))
        else:
            validated = raw_fallbacks

        seen = {(primary_backend, primary_model)}
        for be, mdl in validated:
            if (be, mdl) not in seen:
                chain.append((be, mdl, 1))
                seen.add((be, mdl))

        all_attempts: List[Dict[str, Any]] = []
        for backend_name, model_name, max_n in chain:
            if hasattr(self, "think"):
                try:
                    await self.think(f"trying {backend_name}/{model_name}")
                except Exception:
                    pass
            diff, attempts = await self._llm_draft_with_backend(
                target_file=target_file, rationale=rationale,
                repo_root=repo_root,
                backend=backend_name,
                model=model_name,
                max_attempts=max_n,
                mode=mode,
            )
            all_attempts.extend(attempts)
            if diff:
                return diff, all_attempts
        return None, all_attempts

    @staticmethod
    def _validate_diff_with_git_check(diff: str, *, repo_root: Path) -> Tuple[bool, str]:
        """Run `git apply --check` to verify the diff would apply. Returns (ok, error)."""
        # Pre-validate: catch common LLM diff errors with actionable messages.
        pre_error = _prevalidate_diff(diff)
        if pre_error:
            return (False, pre_error)
        try:
            proc = subprocess.run(
                ["git", "apply", "--check", "-"],
                input=diff, text=True, capture_output=True,
                cwd=str(repo_root), timeout=30,
            )
            return (proc.returncode == 0, proc.stderr.strip()[:300])
        except Exception as e:
            return (False, str(e)[:200])


    def _extract_diff(self, raw: str, rel_path: str) -> Optional[str]:
        """Pull a unified diff out of an LLM response. Returns ``None`` if no
        usable diff is present (e.g. deterministic stub, missing hunks, etc).

        Handles:
        - Markdown fenced blocks (```diff ... ```)
        - Raw unified diff output
        - Extra prose before/after the diff
        - Missing or malformed hunk headers
        """
        if not raw or "[deterministic-stub]" in raw:
            return None

        # Step 1: Extract the diff block from the response.
        # Try fenced block first, then look for diff starting with --- or +++
        body = raw.strip()

        # Remove any thinking/reasoning blocks (some models wrap in <think> tags)
        body = re.sub(r"<think>.*?</think>", "", body, flags=re.S)

        # Try to extract from markdown fenced block
        m = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", body, re.S)
        if m:
            body = m.group(1).strip()

        # If we still don't have a diff, try to find where the diff starts
        if not body.startswith("--- ") and "@@" not in body[:200]:
            # Look for the first --- or +++ line
            lines = body.splitlines()
            start = 0
            for i, line in enumerate(lines):
                if line.startswith("--- ") or line.startswith("+++ "):
                    start = i
                    break
            if start > 0:
                body = "\n".join(lines[start:]).strip()

        if not body:
            return None

        # Step 2: Require at least one hunk with proper @@ header
        if "@@" not in body:
            return None

        # Step 3: Ensure file headers exist. If missing, inject minimal ones.
        has_a = body.startswith("--- a/") or f"\n--- a/{rel_path}" in body
        has_b = body.startswith("+++ b/") or f"\n+++ b/{rel_path}" in body
        if not has_a and not has_b:
            if not body.startswith("--- "):
                body = f"--- a/{rel_path}\n+++ b/{rel_path}\n{body}"

        # Step 4: Validate and fix hunk headers
        # Each hunk should look like: @@ -start,count +start,count @@
        def _fix_hunk_header(m: re.Match) -> str:
            header = str(m.group(0))
            # Ensure there's a trailing @@
            if not header.endswith("@@"):
                # Try to find where the @@ should end
                rest = m.string[m.end():]
                end_m = re.match(r"(.*?)@@", rest, re.S)
                if end_m:
                    return header + " " + str(end_m.group(1)).strip() + " @@"
            return header

        body = re.sub(r"@@\s*-?\d+,?\d*\s*\+?\d+,?\d*\s*@@?", _fix_hunk_header, body)

        # Step 5: Ensure trailing newline
        if not body.endswith("\n"):
            body += "\n"

        return body

    # ---------------- apply handler ----------------
    async def _apply_patch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Wrapper that runs ``_do_apply`` and raises on failure.

        Raising on failure is required so the proposal store correctly marks
        the proposal as ``status=failed`` (returning ``{ok:False}`` would leave
        the proposal stuck on ``status=applied``, which is exactly the silent
        failure mode this fix targets).
        """
        result = await self._do_apply(payload)
        # Stash structured info so execute() can recover rejected_diff_path
        # even when approve() catches our exception.
        try:
            self._last_apply_info = dict(result)
        except Exception:
            self._last_apply_info = {}
        if result.get("ok"):
            return result
        # cortex audit 2026-06-13: AUTO-APPLIED (not user-initiated) patch
        # failures were swallowed here -- only the synchronous user-initiated
        # path in execute() called _publish_result, so background auto-apply
        # failures (incl. branch-ref collisions) never raised a toast. Mirror
        # that SYSTEM_ALERT for the auto-apply path. (User-initiated applies are
        # alerted by execute(); skip them here to avoid a duplicate toast.)
        if not payload.get("user_initiated"):
            self._publish_result(
                applied=False,
                branch=result.get("branch"),
                error=result.get("error") or "git apply failed",
                target=str(payload.get("target_file") or "(unknown)"),
                user_initiated=False,
            )
        raise CodePatchApplyError(result.get("error") or "git apply failed", info=result)

    async def _do_apply(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        target_file: str = payload.get("target_file", "")
        patch: str = payload.get("patch", "")
        repo_root = self._effective_repo_root(payload.get("repo_root"))
        if not target_file or not patch:
            return {"ok": False, "applied": False, "branch": None,
                    "error": "missing target_file or patch",
                    "rejected_diff_path": None}
        if self._resolve_target_path(repo_root, target_file) is None:
            return {"ok": False, "applied": False, "branch": None,
                    "error": "target_file does not exist inside the target repo",
                    "rejected_diff_path": None}

        # Are we in a git repo?
        in_git = await asyncio.to_thread(
            self._run_git, ["rev-parse", "--is-inside-work-tree"], repo_root
        )
        if not in_git["ok"]:
            # No git: write the proposed file straight to data/proposals/code/<id>/ as a preview, do NOT touch repo.
            preview_dir = self._proposal_dir(repo_root) / f"{int(time.time())}"
            preview_dir.mkdir(parents=True, exist_ok=True)
            (preview_dir / "patch.diff").write_text(patch)
            return {"ok": True, "applied": False, "branch": None,
                    "preview_dir": str(preview_dir),
                    "error": None,
                    "rejected_diff_path": None,
                    "note": "not a git repo; saved patch as preview only"}

        # Serialize the whole branch/apply/checks/rollback sequence and guard the
        # shared working tree: concurrent applies (or an apply racing a manual
        # checkout) would corrupt each other. The lock makes it atomic per
        # process; the stash handling below tolerates a dirty tree without
        # losing the user's uncommitted work.
        # cortex audit 2026-06-13: ALSO take a cross-process file lock so that
        # respawned / duplicate code_improver PROCESSES serialize their git
        # branch/apply on the shared working tree (the asyncio.Lock alone is
        # process-local and let `refs/heads/skyn3t/auto/...` collide). flock
        # blocks; run it off the event loop so we don't stall other coroutines.
        async with _APPLY_LOCK:
            cm = _cross_process_apply_lock(repo_root)
            await asyncio.to_thread(cm.__enter__)
            try:
                return await self._do_apply_git(payload, repo_root, target_file, patch)
            finally:
                await asyncio.to_thread(cm.__exit__, None, None, None)

    async def _do_apply_git(self, payload: Dict[str, Any], repo_root: Path,
                            target_file: str, patch: str) -> Dict[str, Any]:
        """Branch + apply + commit + checks, run under ``_APPLY_LOCK``.

        Stashes any uncommitted changes first (so we never start from a dirty
        tree that the patch could clobber) and restores them in ``finally`` on
        every exit path. Branch names carry a uuid+pid suffix so the 1s-resolution
        timestamps can never collide across concurrent or rapid applies.
        """
        # Save current branch
        cur = await asyncio.to_thread(
            self._run_git, ["rev-parse", "--abbrev-ref", "HEAD"], repo_root
        )
        cur_branch = (cur.get("stdout","") or "").strip() or "main"

        # Guard the tree: if it's dirty, stash so the apply starts clean and the
        # user's uncommitted work is preserved + restored afterwards.
        stashed = False
        status = await asyncio.to_thread(
            self._run_git, ["status", "--porcelain"], repo_root
        )
        if status.get("ok") and (status.get("stdout", "") or "").strip():
            stash_msg = f"skyn3t-auto-apply {uuid.uuid4().hex[:8]}"
            stash_r = await asyncio.to_thread(
                self._run_git,
                ["stash", "push", "--include-untracked", "-m", stash_msg],
                repo_root,
            )
            if not stash_r.get("ok"):
                return {"ok": False, "applied": False, "branch": None,
                        "error": ("working tree is dirty and could not be stashed: "
                                  f"{stash_r.get('stderr', '')[:300]}"),
                        "rejected_diff_path": None}
            stashed = True

        try:
            return await self._apply_on_branch(
                payload, repo_root, target_file, patch, cur_branch
            )
        finally:
            if stashed:
                # The stashed changes were the user's uncommitted work on
                # ``cur_branch``; restore them there (not onto the auto branch we
                # may have ended up on after a successful apply).
                head = await asyncio.to_thread(
                    self._run_git, ["rev-parse", "--abbrev-ref", "HEAD"], repo_root
                )
                head_branch = (head.get("stdout", "") or "").strip()
                # NB: never ``return`` from this ``finally`` — a return here would
                # silently discard the value produced by ``_apply_on_branch`` (the
                # success dict or structured error). Use a flag to skip the pop
                # while still letting the try-block's return value propagate.
                can_pop = True
                if head_branch != cur_branch:
                    co = await asyncio.to_thread(
                        self._run_git, ["checkout", cur_branch], repo_root
                    )
                    if not co.get("ok"):
                        logger.error(
                            "could not return to %s to restore stashed changes; "
                            "leaving stash in place: %s",
                            cur_branch, co.get("stderr", "")[:300],
                        )
                        can_pop = False
                if can_pop:
                    pop_r = await asyncio.to_thread(
                        self._run_git, ["stash", "pop"], repo_root
                    )
                    if not pop_r.get("ok"):
                        logger.error(
                            "could not restore stashed changes after auto-apply: %s",
                            pop_r.get("stderr", "")[:300],
                        )

    async def _apply_on_branch(self, payload: Dict[str, Any], repo_root: Path,
                               target_file: str, patch: str,
                               cur_branch: str) -> Dict[str, Any]:
        # uuid+pid suffix so 1s-resolution timestamps never collide across
        # concurrent or rapid applies.
        branch = f"skyn3t/auto/{int(time.time())}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        # Create + checkout branch
        r = await asyncio.to_thread(self._run_git, ["checkout", "-b", branch], repo_root)
        if not r["ok"]:
            return {"ok": False, "applied": False, "branch": None,
                    "error": f"checkout -b failed: {r.get('stderr','')}",
                    "rejected_diff_path": None}
        # Apply patch — try strict first, then progressively more lenient flags
        # to tolerate LLM diffs with bare "@@" headers, fuzzy whitespace, etc.
        apply_attempts = [
            ["git", "apply", "-"],
            ["git", "apply", "--recount", "-"],
            ["git", "apply", "--recount", "--whitespace=fix", "--unidiff-zero", "-"],
            ["git", "apply", "--recount", "--whitespace=fix", "--unidiff-zero", "-C0", "-"],
        ]
        last_err = ""
        applied = False
        for cmd in apply_attempts:
            proc = await asyncio.to_thread(subprocess.run, cmd,
                                            input=patch, text=True, capture_output=True,
                                            cwd=str(repo_root))
            if proc.returncode == 0:
                applied = True
                break
            last_err = proc.stderr.strip()
        if not applied:
            # Last resort: parse the diff ourselves and apply by string match.
            try:
                if self._fallback_apply(target_file, patch, repo_root=repo_root):
                    applied = True
                    last_err = ""
            except Exception as e:
                last_err = f"{last_err} | fallback failed: {e}"

        if not applied:
            await asyncio.to_thread(self._run_git, ["checkout", cur_branch], repo_root)
            await asyncio.to_thread(self._run_git, ["branch", "-D", branch], repo_root)
            rejected_path: Optional[str] = None
            try:
                rej_dir = self._proposal_dir(repo_root) / f"rejected-{int(time.time())}"
                rej_dir.mkdir(parents=True, exist_ok=True)
                (rej_dir / "patch.diff").write_text(patch)
                (rej_dir / "error.txt").write_text(last_err[:2000])
                rejected_path = str(rej_dir)
            except Exception:
                logger.exception("could not write rejected diff dir")
            return {"ok": False, "applied": False, "branch": None,
                     "error": f"git apply failed (all attempts): {last_err[:300]}",
                     "rejected_diff_path": rejected_path}
        # Commit
        add_result = await asyncio.to_thread(self._run_git, ["add", target_file], repo_root)
        if not add_result.get("ok"):
            return {
                "ok": False,
                "applied": False,
                "branch": branch,
                "previous_branch": cur_branch,
                "error": f"git add failed: {add_result.get('stderr', '')[:300]}",
                "rejected_diff_path": None,
            }
        cm = await asyncio.to_thread(self._run_git,
            ["commit", "-m", f"chore(auto): {payload.get('rationale','code improvement')[:60]}"],
            repo_root)
        if not cm.get("ok"):
            return {
                "ok": False,
                "applied": False,
                "branch": branch,
                "previous_branch": cur_branch,
                "error": f"git commit failed: {cm.get('stderr', '')[:300]}",
                "rejected_diff_path": None,
            }
        commit_sha = ""
        sha_r = await asyncio.to_thread(self._run_git, ["rev-parse", "HEAD"], repo_root)
        commit_sha = (sha_r.get("stdout") or "").strip()[:12]
        # Run tests
        check_r = await asyncio.to_thread(self._run_repo_checks, repo_root)
        if not check_r.get("ok"):
            # roll back
            checkout_result = await asyncio.to_thread(
                self._run_git, ["checkout", cur_branch], repo_root
            )
            branch_result: Dict[str, Any] = {"ok": False, "stderr": ""}
            active_branch: Optional[str] = branch
            error = f"repo checks failed; rollback failed: {checkout_result.get('stderr', '')[:300]}"
            if checkout_result.get("ok"):
                branch_result = await asyncio.to_thread(
                    self._run_git, ["branch", "-D", branch], repo_root
                )
                if branch_result.get("ok"):
                    active_branch = None
                    error = "repo checks failed; rolled back"
                else:
                    error = (
                        "repo checks failed; restored previous branch but could not delete "
                        f"{branch}: {branch_result.get('stderr', '')[:300]}"
                    )
            return {"ok": False, "applied": False, "branch": active_branch,
                    "previous_branch": cur_branch,
                    "error": error,
                    "rejected_diff_path": None,
                    "test_output": check_r.get("stdout", "")[-1200:] + "\n" + check_r.get("stderr", "")[-400:],
                    "check_command": check_r.get("command")}
        # Stay on the branch — user can merge manually
        return {"ok": True, "applied": True, "branch": branch,
                "commit": commit_sha,
                "previous_branch": cur_branch,
                "tests_passed": True if check_r.get("ran") else None,
                "checks_skipped": not check_r.get("ran"),
                "check_command": check_r.get("command"),
                "check_note": check_r.get("note"),
                "error": None,
                "rejected_diff_path": None}

    @staticmethod
    def _fallback_apply(target_file: str, patch: str, *, repo_root: Path) -> bool:
        """String-based fallback when git apply rejects the diff.

        Parses the unified diff manually, finds each `-` line in the file and
        replaces with the corresponding `+` line(s). Conservative: only applies
        if every `-` block matches exactly once. Skips additions when the diff
        only adds (insert position guessed by the preceding context line).
        """
        path = repo_root / target_file
        if not path.exists() or not path.is_file():
            return False
        # Read bytes so universal-newline translation in text mode doesn't
        # strip "\r\n" before we get a chance to detect it. Diff hunks use
        # "\n" line endings; if the file is "\r\n" we normalize for matching
        # and restore on write.
        original_raw = path.read_bytes().decode("utf-8")
        had_crlf = "\r\n" in original_raw
        original = original_raw.replace("\r\n", "\n") if had_crlf else original_raw
        new_text = original

        # Tokenize patch into hunks separated by @@ markers.
        lines = patch.splitlines(keepends=False)
        hunks: List[List[str]] = []
        cur: List[str] = []
        for ln in lines:
            if ln.startswith("@@"):
                if cur:
                    hunks.append(cur)
                cur = []
            elif ln.startswith("--- ") or ln.startswith("+++ ") or ln.startswith("diff "):
                continue
            else:
                cur.append(ln)
        if cur:
            hunks.append(cur)

        for hunk in hunks:
            removes: List[str] = []
            adds: List[str] = []
            ctx_before: List[str] = []
            ctx_after: List[str] = []
            saw_change = False
            for ln in hunk:
                if not ln:
                    continue
                if ln[0] == "-":
                    removes.append(ln[1:])
                    saw_change = True
                elif ln[0] == "+":
                    adds.append(ln[1:])
                    saw_change = True
                elif ln[0] == " ":
                    if not saw_change:
                        ctx_before.append(ln[1:])
                    else:
                        ctx_after.append(ln[1:])

            # Pure deletion + addition replacement
            if removes and adds:
                old_block = "\n".join(removes)
                new_block = "\n".join(adds)
                if new_text.count(old_block) == 1:
                    new_text = new_text.replace(old_block, new_block, 1)
                else:
                    return False
            elif removes and not adds:
                old_block = "\n".join(removes)
                if new_text.count(old_block) == 1:
                    new_text = new_text.replace(old_block, "", 1)
                else:
                    return False
            elif adds and not removes:
                if ctx_before:
                    anchor = ctx_before[-1]
                    new_block = "\n".join(adds)
                    if new_text.count(anchor) == 1:
                        new_text = new_text.replace(anchor, anchor + "\n" + new_block, 1)
                    else:
                        return False
                else:
                    return False  # don't know where to add

        if new_text == original:
            return False
        if had_crlf:
            new_text = new_text.replace("\n", "\r\n")
        path.write_text(new_text, encoding="utf-8")
        return True

    @staticmethod
    def _run_git(args: List[str], cwd: Path = REPO_ROOT) -> Dict[str, Any]:
        try:
            r = subprocess.run(["git", *args], capture_output=True, text=True,
                                cwd=str(cwd), timeout=60)
            return {"ok": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr}
        except Exception as e:
            return {"ok": False, "stderr": str(e)}
