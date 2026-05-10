"""MCP-style tool-loop driver: LLM ↔ tool-manifest ↔ executions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional

from skyn3t.adapters.mcp_tools import REPO_ROOT, tool_manifest

logger = logging.getLogger("skyn3t.adapters.mcp_client")

_TOOL_RE = re.compile(r"```tool\s*\n(.*?)```", re.S)


class MCPDriver:
    def __init__(self, *, llm_client, tools: Dict[str, Any], max_rounds: int = 6):
        self.llm = llm_client
        self.tools = tools
        self.max_rounds = max_rounds
        self.history: List[Dict[str, Any]] = []
        # Track real, file-mutating activity so we can refuse to claim success
        # when the LLM cheerfully replies DONE without ever touching a file.
        self._mutating_tools = {"write_file", "apply_replacement", "git_commit"}
        self._mutations_made = 0  # count of successful mutating calls
        self._target_hash_before: Optional[str] = None
        self._target_hash_after: Optional[str] = None
        self._target_file: Optional[str] = None

    @staticmethod
    def _sha_of(path_str: str) -> Optional[str]:
        try:
            p = (REPO_ROOT / path_str).resolve()
            if not p.exists() or not p.is_file():
                return None
            return hashlib.sha256(p.read_bytes()).hexdigest()
        except Exception:
            return None

    def set_target_for_change_check(self, target_file: str) -> None:
        """Tell the driver which file to checksum before/after, to verify real changes."""
        self._target_file = target_file
        self._target_hash_before = self._sha_of(target_file)

    async def run(self, *, goal: str, system: str = "") -> Dict[str, Any]:
        manifest = tool_manifest()
        full_system = (
            "You are an autonomous agent that completes a goal by calling tools. "
            "Always reason step-by-step BEFORE calling each tool. "
            "Make small, verifiable edits. Run tests after non-trivial changes. "
            "Reply DONE on its own line when the goal is achieved.\n\n" + manifest
            + ("\n\n# Additional context\n" + system if system else "")
        )
        transcript = f"# Goal\n{goal}\n\n# Begin"

        for round_i in range(self.max_rounds):
            try:
                resp = await self.llm.complete(
                    transcript,
                    system=full_system,
                    max_tokens=4000,
                    temperature=0.2,
                )
            except Exception as e:
                logger.exception("LLM call failed in MCP loop")
                return {"ok": False, "error": f"llm: {e}", "history": self.history}

            self.history.append({"round": round_i, "llm": resp[:5000]})
            done_seen = False
            if isinstance(resp, str):
                tail_lines = [ln.strip() for ln in resp.splitlines()[-3:]]
                done_seen = any(ln == "DONE" for ln in tail_lines)
            if done_seen:
                return self._finalize_done(round_i)

            tool_calls = list(_TOOL_RE.finditer(resp or ""))
            if not tool_calls:
                # No tools, no DONE — bail. Treat as failure unless mutations
                # already happened (and target file actually changed if known).
                return self._finalize_no_tools(round_i)

            results = []
            for m in tool_calls:
                try:
                    spec = json.loads(m.group(1).strip())
                    name = spec.get("name")
                    args = spec.get("args") or {}
                    fn = self.tools.get(name)
                    if fn is None:
                        results.append({"name": name, "error": "unknown tool"})
                        continue
                    res = (
                        fn(**args)
                        if not asyncio.iscoroutinefunction(fn)
                        else await fn(**args)
                    )
                    results.append({"name": name, "result": res})
                    # Track real mutations
                    if name in self._mutating_tools and isinstance(res, dict) and res.get("ok"):
                        self._mutations_made += 1
                except Exception as e:
                    results.append({"name": "?", "error": f"{type(e).__name__}: {e}"})

            self.history.append({"round": round_i, "tool_results": results})
            transcript += (
                f"\n\n## Round {round_i + 1} tool results\n```json\n"
                f"{json.dumps(results, indent=2, default=str)[:6000]}\n```"
            )

        # Loop ran out of rounds without DONE. Same honesty check applies.
        if self._mutations_made == 0:
            return {
                "ok": False,
                "error": "max rounds without DONE and no mutating tool calls",
                "history": self.history,
                "mutations": 0,
            }
        if self._target_file:
            self._target_hash_after = self._sha_of(self._target_file)
            if (
                self._target_hash_before is not None
                and self._target_hash_before == self._target_hash_after
            ):
                return {
                    "ok": False,
                    "error": "max rounds without DONE and target file hash unchanged",
                    "history": self.history,
                    "mutations": self._mutations_made,
                    "target": self._target_file,
                }
        return {
            "ok": False,
            "error": "max rounds without DONE",
            "history": self.history,
            "mutations": self._mutations_made,
        }

    def _finalize_done(self, round_i: int) -> Dict[str, Any]:
        """Validate the LLM's DONE claim against real evidence (mutating
        tool calls + optional target-file hash change). Returns the result
        dict to surface to the caller.
        """
        # Capture target hash AFTER for change verification
        if self._target_file:
            self._target_hash_after = self._sha_of(self._target_file)
        changed: Optional[bool] = None
        if self._target_file and self._target_hash_before is not None:
            changed = self._target_hash_before != self._target_hash_after
        if self._mutations_made == 0:
            return {
                "ok": False,
                "error": "LLM said DONE but made no mutating tool calls",
                "history": self.history,
                "mutations": 0,
            }
        if self._target_file and changed is False:
            return {
                "ok": False,
                "error": "LLM said DONE but target file hash unchanged",
                "history": self.history,
                "mutations": self._mutations_made,
                "target": self._target_file,
            }
        return {
            "ok": True,
            "rounds": round_i + 1,
            "history": self.history,
            "mutations": self._mutations_made,
            "target_changed": changed,
        }

    def _finalize_no_tools(self, round_i: int) -> Dict[str, Any]:
        """The LLM produced neither tools nor a DONE marker. Without evidence
        of real work this is a silent failure — refuse to claim success unless
        prior rounds actually mutated the target.
        """
        if self._target_file:
            self._target_hash_after = self._sha_of(self._target_file)
        changed: Optional[bool] = None
        if self._target_file and self._target_hash_before is not None:
            changed = self._target_hash_before != self._target_hash_after
        if self._mutations_made == 0:
            return {
                "ok": False,
                "error": "no tool calls and no DONE; assuming nothing was done",
                "history": self.history,
                "mutations": 0,
            }
        if self._target_file and changed is False:
            return {
                "ok": False,
                "error": "no tool calls and target file hash unchanged",
                "history": self.history,
                "mutations": self._mutations_made,
                "target": self._target_file,
            }
        return {
            "ok": True,
            "rounds": round_i + 1,
            "history": self.history,
            "note": "no tool calls in final round; prior mutations succeeded",
            "mutations": self._mutations_made,
            "target_changed": changed,
        }
