"""Brainstorm Agent - expands a brief into framings, alternatives, JTBD, and key questions.

Pure stdlib, deterministic transform. Runs as the first stage of every
Studio pipeline so downstream specialists (research, architect, writer,
...) operate on a primary direction with explicit alternatives and open
questions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.brainstorm")

LENS_TEMPLATES = [
    ("user-jobs",       "Reframe as a user job: 'When I {context}, I want to {goal}, so I can {benefit}.'"),
    ("constraint-flip", "Invert one assumption: what if the typical constraint were lifted?"),
    ("simplest-mvp",    "Strip to the smallest thing that delivers value end-to-end."),
    ("competitor-gap",  "Identify a gap that incumbents systematically miss."),
    ("contrarian",      "Take the unfashionable position and steel-man it."),
    ("composable",      "Treat the deliverable as a kit of small pieces that can be reassembled."),
    ("automate-it",     "What part can be automated end-to-end with zero human in the loop?"),
    ("workflow-first",  "Map the user's existing workflow and slot in alongside it."),
]

PROBLEM_VERBS = {
    "build": "creating",
    "make": "creating",
    "design": "designing",
    "launch": "launching",
    "improve": "improving",
    "fix": "fixing",
    "ship": "shipping",
    "write": "writing",
    "scale": "scaling",
}


class BrainstormAgent(BaseAgent):
    """Expands a free-form brief into framings, alternatives, JTBD, key questions.

    Output:
      artifact_dir/brainstorm.md   - primary + alternatives + JTBD + open questions
    TaskResult.output:
      {"primary_direction": str, "alternatives": [str,...],
       "key_questions": [str,...], "files": [...], "summary": str}
    """

    def __init__(
        self,
        name: str = "brainstorm",
        *,
        event_bus: Optional[EventBus] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="brainstorm",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(AgentCapability(
            name="brainstorm",
            description="expand briefs into framings + alternatives",
            parameters={"brief": "str"},
        ))
        self.add_capability(AgentCapability(
            name="problem_framing",
            description="JTBD framing + key questions",
            parameters={},
        ))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        try:
            await self.think(f"brainstorming: {task.title or task.task_id}")
        except Exception:
            pass
        try:
            input_data = task.input_data or {}
            brief = (
                input_data.get("brief")
                or input_data.get("idea")
                or input_data.get("description")
                or ""
            ).strip()
            artifact_dir = input_data.get("artifact_dir") or "."
            next_agent = input_data.get("next_agent")
            require_clarification = bool(input_data.get("require_clarification"))

            if not brief:
                brief = "(no brief provided - produce an exploration of likely directions)"

            # Clarification check: ambiguous briefs should ask questions instead of guessing.
            # Skip if user already provided clarifications (passed via input_data.clarifications).
            if not input_data.get("clarifications"):
                questions = await self._maybe_ask_clarifications(
                    brief,
                    force=require_clarification,
                )
                if require_clarification and not questions:
                    questions = self._kickoff_questions(brief)
                if questions:
                    # Write a marker file so the runner can pick it up
                    ad = Path(artifact_dir)
                    ad.mkdir(parents=True, exist_ok=True)
                    clarify_path = ad / "_clarifications.json"
                    clarify_path.write_text(json.dumps({"questions": questions, "asked_by": self.name},
                                                        indent=2), encoding="utf-8")
                    return TaskResult(
                        task_id=task.task_id, success=True,
                        output={
                            "needs_clarification": True,
                            "questions": questions,
                            "files": [str(clarify_path)],
                            "summary": f"Need {len(questions)} clarifications before proceeding.",
                        },
                    )

            angles = self._expand(brief)

            panel_results: List[Dict[str, str]] = []
            panel = self.config.get("panel") if self.config else None
            if panel is None:
                panel = [
                    {"backend": "claude_cli", "model": "opus",      "label": "claude-opus"},
                    {"backend": "kimi_cli",   "model": "kimi-code/kimi-for-coding", "label": "kimi-k2.6"},
                ]
            for member in panel:
                line = await self._consult(brief, member)
                if line:
                    panel_results.append({"label": member.get("label", member.get("backend", "?")),
                                          "framing": line})
                    angles.append({"lens": member.get("label", "panel"), "framing": line})

            lens = (input_data.get("lens") or "fidelity").lower()
            if lens == "fidelity":
                primary = brief.strip()
                alternatives: List[str] = []
                if panel_results:
                    alternatives.extend(p["framing"] for p in panel_results if p.get("framing"))
                alternatives.extend(a["framing"] for a in angles[:5] if a.get("framing") not in alternatives)
                alternatives = alternatives[:6]
            else:
                primary, alternatives = self._pick_primary(angles, panel_results, brief=brief)
            jtbd = self._jtbd(brief)
            questions = self._key_questions(brief)
            assumptions = self._assumptions(brief)
            success = self._success_criteria(brief)

            md = self._render_md(brief, primary, alternatives, jtbd, questions, assumptions, success,
                                 panel_results=panel_results)
            ad = Path(artifact_dir)
            ad.mkdir(parents=True, exist_ok=True)
            out_path = ad / "brainstorm.md"
            out_path.write_text(md, encoding="utf-8")

            try:
                await self.think(f"wrote {out_path.name}")
            except Exception:
                pass

            if next_agent:
                try:
                    handoff_direction = brief.strip() if lens == "fidelity" else primary
                    await self.send_message(
                        to=next_agent,
                        kind="info",
                        content=(
                            f"Brainstorm complete. User's direction (verbatim): {handoff_direction}. "
                            f"Open questions: {'; '.join(questions[:3])}"
                        ),
                    )
                except Exception:
                    pass

            try:
                await self.share_learning(
                    f"brainstorm: {len(angles)} angles, primary='{primary[:60]}'",
                    scope="studio",
                )
            except Exception:
                pass

            return TaskResult(
                task_id=task.task_id,
                success=True,
                output={
                    "files": [str(out_path)],
                    "your_direction": brief.strip(),
                    "lens": lens,
                    "primary_direction": primary,
                    "alternatives": alternatives,
                    "key_questions": questions,
                    "jtbd": jtbd,
                    "assumptions": assumptions,
                    "success_criteria": success,
                    "summary": f"Brainstormed {len(angles)} angles; primary direction set.",
                },
            )
        except Exception as e:
            logger.exception("brainstorm failed")
            return TaskResult(task_id=task.task_id, success=False, error=str(e))

    # ------- helpers ----------
    async def _maybe_ask_clarifications(self, brief: str, *, force: bool = False) -> List[str]:
        """Ask LLM if brief is ambiguous; if so return up to 3 short questions."""
        if not brief or len(brief.strip()) < 10:
            return []
        try:
            client = self.get_llm() if hasattr(self, "get_llm") else None
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(default_model=self.config.get("model"),
                                   backend=self.config.get("backend"),
                                   event_bus=self.event_bus, caller_name=self.name)
            system = (
                "You are a senior product manager triaging a project brief. Decide if the brief "
                "is specific enough to act on. Reply with valid JSON only:\n"
                '{"clear": true/false, "questions": ["q1", "q2"]}\n'
                "Set clear=true if you can confidently produce useful artifacts (architecture, brand, "
                "code) without further input. Set clear=false ONLY when there are critical ambiguities. "
                "Limit to AT MOST 3 short, specific questions. Don't ask about preferences a designer "
                "could just pick (color, font). DO ask about platform/audience/scope when unclear. "
                "Return empty questions array if clear."
            )
            if force:
                system += (
                    " The user explicitly wants an early confirmation pass, so ask 2-3 short kickoff "
                    "questions even if the brief looks mostly clear."
                )
            prompt = f"Brief:\n{brief}\n\nReply JSON."
            out = await asyncio.wait_for(
                client.complete(prompt, system=system, max_tokens=400, temperature=0.0),
                timeout=30,
            )
            if not out or "[deterministic-stub]" in out:
                return []
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", out)
            if not m:
                return []
            data = json.loads(m.group(0))
            if data.get("clear"):
                return []
            qs = data.get("questions") or []
            return [str(q)[:200] for q in qs if q][:3]
        except Exception:
            return []

    def _kickoff_questions(self, brief: str) -> List[str]:
        return [
            "Who is the primary user or customer for this first version?",
            "What must the first deliverable do end-to-end before you would call it useful?",
            "What important constraint, non-goal, or risk should the swarm avoid?",
        ]

    def _seed(self, brief: str) -> int:
        return int(hashlib.sha256(brief.encode("utf-8")).hexdigest()[:8], 16)

    def _expand(self, brief: str) -> List[Dict[str, str]]:
        seed = self._seed(brief)
        out: List[Dict[str, str]] = []
        # Rotate so order varies per brief but is deterministic
        rot = seed % len(LENS_TEMPLATES)
        rotated = LENS_TEMPLATES[rot:] + LENS_TEMPLATES[:rot]
        for lens, template in rotated[:6]:
            out.append({"lens": lens, "framing": self._compose(template, brief)})
        return out

    def _compose(self, template: str, brief: str) -> str:
        verb = "creating"
        words = brief.split()
        first = words[0].lower() if words else ""
        verb = PROBLEM_VERBS.get(first, verb)
        return (
            template.replace("{context}", "the user's current workflow")
            .replace("{goal}", verb + " " + self._object_phrase(brief))
            .replace("{benefit}", "they can move faster with less friction")
        )

    def _object_phrase(self, brief: str) -> str:
        # take the noun-ish chunk after the first verb
        words = brief.strip().split()
        if not words:
            return "what they need"
        if words[0].lower() in PROBLEM_VERBS:
            return " ".join(words[1:6]) or "the thing"
        return " ".join(words[:5])

    def _pick_primary(self, angles: List[Dict[str, str]],
                       panel_results: Optional[List[Dict[str, str]]] = None,
                       brief: str = "") -> tuple[str, List[str]]:
        """Bias-free selection.

        No model is allowed to judge framings. If LLM panelists contributed
        framings, all panelists' contributions are treated as peers; the
        'primary' is chosen by deterministic rotation derived from the brief
        hash (so it's reproducible and balanced across sessions, not biased
        toward any one provider). Template lenses fill in if the panel is
        empty or short.
        """
        peers: List[str] = []
        if panel_results:
            peers.extend(p["framing"] for p in panel_results if p.get("framing"))
        for a in angles:
            f = a.get("framing")
            if f and f not in peers:
                peers.append(f)
        if not peers:
            return "(no directions produced)", []
        # rotation index from brief hash → reproducible, no model arbitration
        idx = self._seed(brief) % len(peers) if brief else 0
        primary = peers[idx]
        rest = [p for i, p in enumerate(peers) if i != idx][:5]
        return primary, rest

    async def _consult(self, brief: str, member: Dict[str, Any]) -> Optional[str]:
        try:
            from skyn3t.adapters import LLMClient
            client = LLMClient(default_model=member.get("model"), backend=member.get("backend"))
            prompt = (
                "Reframe the following brief as one fresh, concrete problem framing. "
                "Think briefly: what's the SHARPEST framing that the user might NOT have considered? "
                "One sentence. No preamble, no quotes, no list formatting.\n\n"
                f"Brief: {brief}"
            )
            out = await asyncio.wait_for(
                client.complete(prompt, max_tokens=120, temperature=0.9), timeout=45)
            line = (out or "").strip().splitlines()[0] if out else ""
            line = line.strip(" \"'`-•*").strip()
            if not line or line.startswith("[deterministic-stub]"):
                return None
            try:
                await self.think(f"panel/{member.get('label','llm')}: {line[:80]}")
            except Exception:
                pass
            return line[:300]
        except Exception:
            return None

    def _jtbd(self, brief: str) -> str:
        obj = self._object_phrase(brief)
        return (
            f"When the user encounters the situation behind '{brief[:80]}', "
            f"they want to accomplish {obj}, so they can reduce time, friction, or risk."
        )

    def _key_questions(self, brief: str) -> List[str]:
        return [
            "Who is the primary user, and what does their day look like today?",
            "What does success look like in 30 days? In 6 months?",
            "Which existing tool or workflow is being replaced or augmented?",
            "What's the riskiest assumption - the one that, if wrong, kills the idea?",
            "What's the smallest version that could deliver value end-to-end?",
            "Where will the data live? Who owns it? Is privacy a hard constraint?",
        ]

    def _assumptions(self, brief: str) -> List[str]:
        return [
            "There exists a definable user with a recurring need.",
            "The need is painful enough that switching cost is acceptable.",
            "We can deliver an MVP within typical timebox without infra blockers.",
            "Distribution is solvable (audience exists or can be reached).",
        ]

    def _success_criteria(self, brief: str) -> List[str]:
        return [
            "First version is usable end-to-end without manual workarounds.",
            "Three sample users can complete the core task without help.",
            "Cost of operation is bounded and predictable.",
            "Outcome is measurable (a number, not a vibe).",
        ]

    def _render_md(self, brief, primary, alternatives, jtbd, questions, assumptions, success,
                    panel_results: Optional[List[Dict[str, str]]] = None) -> str:
        now = datetime.now(timezone.utc).isoformat()
        lines = [
            "# Brainstorm",
            f"_generated: {now}_",
            "",
            "## Brief",
            f"> {brief}",
            "",
            "## Your direction (verbatim)",
            f"**{primary}**",
            "",
            "_This is what you wrote. Downstream agents must work from this, not from agent rephrasings._",
            "",
            "## Optional alternative angles (advisory only — do not replace your direction)",
        ]
        for a in alternatives:
            lines.append(f"- {a}")
        if panel_results:
            lines += ["", "## Panel contributions"]
            for p in panel_results:
                lines.append(f"- **{p['label']}** — {p['framing']}")
        lines += [
            "",
            "## Jobs-to-be-done framing",
            jtbd,
            "",
            "## Key questions",
        ]
        for q in questions:
            lines.append(f"- {q}")
        lines += ["", "## Assumptions to test"]
        for a in assumptions:
            lines.append(f"- {a}")
        lines += ["", "## Success criteria"]
        for s in success:
            lines.append(f"- {s}")
        lines += ["", "_- ready for handoff to research -_", ""]
        return "\n".join(lines)
