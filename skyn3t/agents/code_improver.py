from __future__ import annotations
import asyncio, difflib, json, logging, os, re, subprocess, time
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.code_improver")

REPO_ROOT = Path(__file__).resolve().parents[2]   # /.../jarvis


class CodeImproverAgent(BaseAgent):
    """Reads recent failures + lessons; proposes a small targeted code change.

    Output: a Proposal(kind='code_patch') containing a unified diff. The apply
    handler is registered with the proposal store at startup (or on first execute):
    on approval, it creates a branch `skyn3t/auto/<id>`, applies the diff with
    `git apply`, runs `pytest`, and:
      - if tests pass:  leaves the branch in place and returns success.
      - if tests fail:  resets the branch (deletes it) and returns failure.
    """

    def __init__(self, name: str = "code_improver", *, event_bus: Optional[EventBus] = None,
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

    def _register_handler(self) -> None:
        if self._handler_registered: return
        try:
            from skyn3t.cortex import get_store
            get_store().register_handler("code_patch", self._apply_patch)
            self._handler_registered = True
        except Exception:
            logger.exception("could not register code_patch handler")

    async def execute(self, task: TaskRequest) -> TaskResult:
        if hasattr(self, "think"):
            try: await self.think(f"code_improver scanning")
            except Exception: pass
        self._register_handler()  # idempotent

        input_data = task.input_data or {}
        target = input_data.get("target_file")  # optional explicit override
        rationale = input_data.get("rationale", "")
        diff = input_data.get("diff")  # optional pre-built diff

        if diff and target:
            patch_text = diff
        else:
            target, patch_text, rationale = await self._draft_patch(target=target, rationale=rationale)

        if not patch_text or not target:
            return TaskResult(task_id=task.task_id, success=True,
                              output={"proposed": False, "reason": "no actionable diff"})

        # File the proposal
        try:
            from skyn3t.cortex import get_store
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"proposal store unavailable: {e}")

        proposal = get_store().create(
            kind="code_patch",
            title=f"Patch {target}",
            summary=rationale or "auto-generated improvement proposal",
            detail=f"Target: `{target}`\n\nReason: {rationale}\n\n```diff\n{patch_text}\n```",
            payload={"target_file": target, "patch": patch_text, "rationale": rationale},
            source="code_improver",
        )
        return TaskResult(task_id=task.task_id, success=True,
                          output={"proposed": True, "proposal_id": proposal.id,
                                  "target": target})

    async def _draft_patch(self, *, target: Optional[str], rationale: str) -> tuple[Optional[str], Optional[str], str]:
        """Draft a patch. If the caller supplied an explicit ``target`` + ``rationale``,
        try the LLM first. Falls back to a deterministic, heuristic transform that
        looks for ``datetime.utcnow()`` / TODO / FIXME markers in ``skyn3t/`` and
        produces a tiny clean-up patch. Returns ``(target, diff, rationale)``.
        """
        # 1. LLM-driven path: only when the user gave a specific file + rationale.
        if target and rationale:
            llm_diff = await self._llm_draft(target, rationale)
            if llm_diff:
                return target, llm_diff, rationale

        # 2. Deterministic fallback (unchanged behavior).
        candidates: List[Path] = []
        if target:
            p = REPO_ROOT / target
            if p.exists(): candidates = [p]
        else:
            for p in (REPO_ROOT / "skyn3t").rglob("*.py"):
                try:
                    text = p.read_text(encoding="utf-8")
                except Exception: continue
                if "datetime.utcnow()" in text or "TODO" in text or "FIXME" in text:
                    candidates.append(p)
            candidates = candidates[:1]
        if not candidates: return None, None, rationale or "nothing to improve"
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
        rel = target_path.relative_to(REPO_ROOT).as_posix()
        diff = "".join(difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        return rel, diff, rationale

    async def _llm_draft(self, target_file: str, rationale: str) -> Optional[str]:
        """Ask the LLM for a unified diff that addresses ``rationale`` on ``target_file``.

        Returns a diff string suitable for ``git apply``, or ``None`` if the LLM
        returns a deterministic stub / non-diff output / the file is missing.
        """
        try:
            path = (REPO_ROOT / target_file).resolve()
            # confine to repo
            try:
                path.relative_to(REPO_ROOT.resolve())
            except ValueError:
                return None
            if not path.exists() or not path.is_file():
                return None
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                return None
            if len(text) > 200_000:
                text = text[:200_000] + "\n…[truncated for LLM context window]…"

            client = None
            try:
                if hasattr(self, "get_llm"):
                    client = self.get_llm()
            except Exception:
                client = None
            if client is None:
                try:
                    from skyn3t.adapters import LLMClient
                    client = LLMClient(
                        default_model=self.config.get("model"),
                        backend=self.config.get("backend"),
                    )
                except Exception:
                    return None

            rel = path.relative_to(REPO_ROOT.resolve()).as_posix()
            system = (
                "You are CodeImproverAgent inside the SkyN3t orchestrator. You produce unified diffs. "
                "You MUST output a single fenced ```diff block, nothing else. The diff must apply with "
                "`git apply` from the repo root. Use exactly two header lines: `--- a/<path>` and "
                "`+++ b/<path>` followed by valid hunks. Make the SMALLEST change that addresses the "
                "issue. Preserve all existing behavior unless the rationale says otherwise. Do not "
                "invent files or add unrelated edits."
            )
            prompt = (
                f"Target file: `{rel}`\n"
                f"Rationale (what to change and why):\n{rationale}\n\n"
                f"Current file contents:\n```\n{text}\n```\n\n"
                f"Reply ONLY with a fenced ```diff block containing the unified diff."
            )

            try:
                out = await client.complete(
                    prompt, system=system, max_tokens=4000, temperature=0.2,
                )
            except Exception:
                logger.exception("_llm_draft: client.complete raised")
                return None

            return self._extract_diff(out, rel)
        except Exception:
            logger.exception("_llm_draft failed")
            return None

    def _extract_diff(self, raw: str, rel_path: str) -> Optional[str]:
        """Pull a unified diff out of an LLM response. Returns ``None`` if no
        usable diff is present (e.g. deterministic stub, missing hunks, etc).
        """
        if not raw or "[deterministic-stub]" in raw:
            return None
        m = re.search(r"```(?:diff|patch)?\s*\n(.+?)```", raw, re.S)
        body = m.group(1) if m else raw
        body = body.strip()
        if not body:
            return None
        # require at least one hunk
        if "@@" not in body:
            return None
        # require headers reference our path; if missing, inject minimal headers.
        if f"a/{rel_path}" not in body and f"b/{rel_path}" not in body:
            if not body.startswith("--- "):
                body = f"--- a/{rel_path}\n+++ b/{rel_path}\n" + body
        if not body.endswith("\n"):
            body += "\n"
        return body

    # ---------------- apply handler ----------------
    async def _apply_patch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        target_file: str = payload.get("target_file", "")
        patch: str = payload.get("patch", "")
        if not target_file or not patch:
            return {"ok": False, "error": "missing target_file or patch"}

        # Are we in a git repo?
        in_git = await asyncio.to_thread(self._run_git, ["rev-parse", "--is-inside-work-tree"])
        if not in_git["ok"]:
            # No git: write the proposed file straight to data/proposals/code/<id>/ as a preview, do NOT touch repo.
            preview_dir = Path("data/proposals/code") / f"{int(time.time())}"
            preview_dir.mkdir(parents=True, exist_ok=True)
            (preview_dir / "patch.diff").write_text(patch)
            return {"ok": True, "applied": False, "preview_dir": str(preview_dir),
                    "note": "not a git repo; saved patch as preview only"}

        branch = f"skyn3t/auto/{int(time.time())}"
        # Save current branch
        cur = await asyncio.to_thread(self._run_git, ["rev-parse", "--abbrev-ref", "HEAD"])
        cur_branch = (cur.get("stdout","") or "").strip() or "main"
        # Create + checkout branch
        r = await asyncio.to_thread(self._run_git, ["checkout", "-b", branch])
        if not r["ok"]:
            return {"ok": False, "error": f"checkout -b failed: {r.get('stderr','')}"}
        # Apply patch
        proc = await asyncio.to_thread(subprocess.run, ["git", "apply", "-"],
                                        input=patch, text=True, capture_output=True, cwd=str(REPO_ROOT))
        if proc.returncode != 0:
            await asyncio.to_thread(self._run_git, ["checkout", cur_branch])
            await asyncio.to_thread(self._run_git, ["branch", "-D", branch])
            return {"ok": False, "error": f"git apply failed: {proc.stderr.strip()[:300]}"}
        # Commit
        await asyncio.to_thread(self._run_git, ["add", target_file])
        cm = await asyncio.to_thread(self._run_git,
            ["commit", "-m", f"chore(auto): {payload.get('rationale','code improvement')[:60]}"])
        # Run tests
        test_r = await asyncio.to_thread(subprocess.run, ["python3", "-m", "pytest", "-q", "--tb=line"],
                                          capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=180)
        if test_r.returncode != 0:
            # roll back
            await asyncio.to_thread(self._run_git, ["checkout", cur_branch])
            await asyncio.to_thread(self._run_git, ["branch", "-D", branch])
            return {"ok": False, "error": "tests failed; rolled back",
                    "test_output": test_r.stdout[-1200:] + "\n" + test_r.stderr[-400:]}
        # Stay on the branch — user can merge manually
        return {"ok": True, "applied": True, "branch": branch,
                "previous_branch": cur_branch,
                "tests_passed": True}

    @staticmethod
    def _run_git(args: List[str]) -> Dict[str, Any]:
        try:
            r = subprocess.run(["git", *args], capture_output=True, text=True,
                                cwd=str(REPO_ROOT), timeout=60)
            return {"ok": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr}
        except Exception as e:
            return {"ok": False, "stderr": str(e)}
