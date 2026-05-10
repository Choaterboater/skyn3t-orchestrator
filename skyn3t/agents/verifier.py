"""VerifierAgent — quality-gate that catches silent failures.

Pattern:
    verifier = VerifierAgent(event_bus=...)
    res = await verifier.execute(TaskRequest(input_data={
        'brief': '...',
        'artifact_path': '/path/to/file.md',
        'expectations': ['must mention Skynet aesthetic', 'must include palette']
    }))
    print(res.output)
    # {'verdict': 'no'|'yes'|'partial', 'score': 0..100, 'reasons': [...], 'cited': [...]}

Used by Studio runner as a quality gate after every artifact-producing stage.
If verdict is 'no', the stage is flagged and (optionally) re-run with the
verifier's critique injected as additional context.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.verifier")

MAX_ARTIFACT_CHARS = 6_000


class VerifierAgent(BaseAgent):
    def __init__(self, name: str = "verifier", *, event_bus: Optional[EventBus] = None,
                 config: Optional[Dict[str, Any]] = None):
        super().__init__(name=name, agent_type="verifier", provider="local",
                         event_bus=event_bus or EventBus(), config=config)
        self.add_capability(AgentCapability(
            name="verification",
            description="grades whether an artifact addresses a brief",
            parameters={"brief": "str", "artifact_path": "str", "expectations": "list[str]"}))
        self.add_capability(AgentCapability(
            name="quality_gate", description="boolean ok/not-ok gate", parameters={}))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        if hasattr(self, "think"):
            try:
                await self.think("verifier checking artifact")
            except Exception:
                logger.debug("think() failed during verify", exc_info=True)
        d = task.input_data or {}
        brief = (d.get("brief") or "").strip()
        artifact_path = d.get("artifact_path", "")
        expectations: List[str] = d.get("expectations") or []
        if not brief or not artifact_path:
            return TaskResult(task_id=task.task_id, success=False,
                              error="brief and artifact_path required")
        try:
            text = Path(artifact_path).read_text(encoding="utf-8")
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False,
                              error=f"can't read artifact: {e}")

        verdict, score, reasons, cited = await self._verify(
            brief=brief, artifact=text[:MAX_ARTIFACT_CHARS],
            expectations=expectations)

        if hasattr(self, "share_learning"):
            try:
                await self.share_learning(
                    f"verified {Path(artifact_path).name}: {verdict} ({score}/100)",
                    scope="quality")
            except Exception:
                logger.debug("share_learning(verify) failed", exc_info=True)

        return TaskResult(task_id=task.task_id, success=True,
                          output={
                              "verdict": verdict,           # "yes" | "no" | "partial"
                              "score": score,
                              "reasons": reasons,
                              "cited": cited,
                              "artifact_path": artifact_path,
                              "summary": f"{verdict} · {score}/100 · {len(reasons)} reasons",
                          })

    async def _verify(self, *, brief: str, artifact: str,
                        expectations: List[str]) -> tuple[str, int, List[str], List[str]]:
        """Use LLM to grade. Falls back to heuristic if LLM stub."""
        client = self.get_llm() if hasattr(self, "get_llm") else None
        if client is None:
            try:
                from skyn3t.adapters import LLMClient
                client = LLMClient(default_model=self.config.get("model"),
                                   backend=self.config.get("backend"),
                                   event_bus=self.event_bus, caller_name=self.name)
            except Exception:
                return self._heuristic_verify(brief=brief, artifact=artifact,
                                                expectations=expectations)

        exp_block = "\n".join(f"- {e}" for e in expectations) if expectations else "(none specified — judge by brief alone)"
        prompt = (
            f"Brief from user:\n{brief}\n\n"
            f"Specific expectations:\n{exp_block}\n\n"
            f"Artifact produced:\n```\n{artifact}\n```\n\n"
            f"Reply ONLY with valid JSON of the form:\n"
            f'{{"verdict": "yes|no|partial", "score": 0-100, '
            f'"reasons": ["short reason 1", "short reason 2", ...], '
            f'"cited": ["quoted snippet from the artifact that supports each reason"]}}\n'
            f"Be strict. Don't grade on effort. The brief is the spec."
        )
        system = (
            "You are a verification agent. Your job is to gate quality. "
            "Output ONLY valid JSON, no preamble. "
            "Score harshly: 90+ = the artifact would satisfy a discerning user, "
            "70-89 = mostly addresses brief but missing important elements, "
            "50-69 = partial — touches brief but misses the spirit, "
            "0-49 = doesn't address the brief or contradicts it."
        )
        try:
            out = await client.complete(prompt, system=system,
                                          max_tokens=600, temperature=0.0)
            if "[deterministic-stub]" in out:
                return self._heuristic_verify(brief=brief, artifact=artifact,
                                                expectations=expectations)
            data = self._parse_json(out)
            verdict = str(data.get("verdict", "partial")).lower()
            if verdict not in ("yes", "no", "partial"):
                verdict = "partial"
            score = int(data.get("score", 50))
            score = max(0, min(100, score))
            reasons = [str(r)[:200] for r in (data.get("reasons") or [])][:6]
            cited = [str(c)[:200] for c in (data.get("cited") or [])][:6]
            return verdict, score, reasons, cited
        except Exception:
            logger.exception("LLM verify failed; using heuristic")
            return self._heuristic_verify(brief=brief, artifact=artifact,
                                            expectations=expectations)

    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        """Find the first {...} JSON object in text."""
        if not text:
            return {}
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _heuristic_verify(*, brief: str, artifact: str,
                            expectations: List[str]) -> tuple[str, int, List[str], List[str]]:
        """Cheap fallback when LLM unavailable: word-overlap + length sanity."""
        if not artifact.strip():
            return "no", 0, ["artifact is empty"], []
        if len(artifact) < 200:
            return "no", 25, ["artifact is suspiciously short"], []
        # Token overlap (very rough)
        b_tokens = set(re.findall(r"\w{4,}", brief.lower()))
        a_tokens = set(re.findall(r"\w{4,}", artifact.lower()))
        if not b_tokens:
            return "partial", 60, ["heuristic: no brief tokens to compare"], []
        overlap = len(b_tokens & a_tokens) / max(1, len(b_tokens))
        score = min(100, int(overlap * 100) + 20)
        verdict = "yes" if score >= 75 else "partial" if score >= 50 else "no"
        reasons = [f"heuristic: brief-token overlap {int(overlap*100)}%"]
        if expectations:
            missing = [e for e in expectations
                        if not any(t for t in re.findall(r"\w{4,}", e.lower()) if t in a_tokens)]
            if missing:
                reasons.append(f"expectations not clearly addressed: {missing[:3]}")
                score = max(0, score - 20)
                verdict = "no" if score < 50 else verdict
        return verdict, score, reasons, []
