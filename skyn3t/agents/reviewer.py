"""Reviewer Agent - QA pass over an artifact directory.

LLM-free heuristic review. Walks every file in ``artifact_dir``, scores
completeness, looks for empty headings / TODO markers / missing CTA, checks
that brand colors referenced in palette.json appear somewhere in
landing_copy.md, and emits ``review.md`` with a go/no-go verdict.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)$", re.MULTILINE)
_TODO_RE = re.compile(r"\b(TODO|FIXME|TBD|XXX)\b")
_CTA_HINTS = ("cta", "call to action", "get started", "sign up", "start free", "buy", "try")


class ReviewerAgent(BaseAgent):
    """Heuristic QA reviewer. Reads artifacts, writes review.md."""

    def __init__(
        self,
        name: str = "reviewer",
        event_bus: EventBus = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="reviewer",
            provider="local",
            event_bus=event_bus,
            config=config,
        )
        self.add_capability(AgentCapability(
            name="review",
            description="Heuristic review of an artifact directory.",
            parameters={"artifact_dir": "str"},
        ))
        self.add_capability(AgentCapability(
            name="qa",
            description="Completeness + consistency checks across artifacts.",
            parameters={"artifact_dir": "str"},
        ))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")

        data = task.input_data or {}
        artifact_dir = Path(data.get("artifact_dir") or ".")
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        files = sorted(p for p in artifact_dir.iterdir() if p.is_file() and p.name != "review.md")
        await self.think(f"reviewing {len(files)} artifact(s) in {artifact_dir}")

        contents: Dict[str, str] = {}
        for p in files:
            try:
                contents[p.name] = p.read_text(encoding="utf-8")
            except Exception:
                contents[p.name] = ""

        completeness = self._completeness_checklist(contents)
        consistency = self._consistency_notes(contents)
        risks = self._risks(contents)
        verdict, score = self._verdict(completeness, consistency, risks)

        review_md = self._render_review_md(
            artifact_dir=artifact_dir,
            files=files,
            completeness=completeness,
            consistency=consistency,
            risks=risks,
            verdict=verdict,
            score=score,
        )
        review_path = artifact_dir / "review.md"
        review_path.write_text(review_md, encoding="utf-8")
        await self.think(f"wrote {review_path.name} (verdict: {verdict})")

        if next_agent:
            await self.send_message(
                to=next_agent,
                kind="info",
                content=f"{self.name} done; artifacts in {artifact_dir}",
                payload={"verdict": verdict, "score": score, "review": str(review_path)},
            )

        await self.share_learning(
            f"Reviewer flagged {len(risks)} risk(s); verdict={verdict}.",
            scope="global",
            verdict=verdict,
            risk_count=len(risks),
        )

        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "files": [str(review_path)],
                "verdict": verdict,
                "score": score,
                "summary": f"Review complete: {verdict} ({score}/100).",
            },
        )

    def _completeness_checklist(self, contents: Dict[str, str]) -> List[Dict[str, Any]]:
        expected = [
            "architecture.md",
            "tech_stack.json",
            "brand.md",
            "palette.json",
            "components.md",
            "positioning.md",
            "channel_plan.md",
            "launch_checklist.md",
        ]
        items: List[Dict[str, Any]] = []
        for name in expected:
            present = name in contents and len(contents[name].strip()) > 0
            items.append({"name": name, "present": present})
        # Note any markdown artifacts that have empty headings.
        for name, body in contents.items():
            if not name.endswith(".md"):
                continue
            for m in _HEADING_RE.finditer(body):
                if not m.group(2).strip():
                    items.append({"name": f"{name} (empty heading)", "present": False})
                    break
        return items

    def _consistency_notes(self, contents: Dict[str, str]) -> List[str]:
        notes: List[str] = []

        # Palette colors should appear in landing_copy or components.
        palette_raw = contents.get("palette.json", "")
        if palette_raw:
            try:
                palette = json.loads(palette_raw)
                colors = [v for v in palette.values() if isinstance(v, str) and v.startswith("#")]
                landing = contents.get("landing_copy.md", "")
                components = contents.get("components.md", "")
                for c in colors:
                    in_landing = c in landing
                    in_components = c in components
                    if landing and not in_landing:
                        notes.append(
                            f"Palette color `{c}` is not referenced in landing_copy.md."
                        )
                    if components and not in_components:
                        notes.append(
                            f"Palette color `{c}` is not referenced in components.md."
                        )
            except Exception as e:
                notes.append(f"palette.json failed to parse: {e}")

        # tech_stack.json should be valid JSON with required keys.
        ts_raw = contents.get("tech_stack.json", "")
        if ts_raw:
            try:
                ts = json.loads(ts_raw)
                for k in ("frontend", "backend", "db", "infra", "ci"):
                    if k not in ts:
                        notes.append(f"tech_stack.json missing key `{k}`.")
            except Exception as e:
                notes.append(f"tech_stack.json failed to parse: {e}")

        # Landing copy should mention a CTA.
        landing = contents.get("landing_copy.md", "").lower()
        if landing and not any(h in landing for h in _CTA_HINTS):
            notes.append("landing_copy.md does not contain an obvious CTA.")

        if not notes:
            notes.append("No consistency issues detected.")
        return notes

    def _risks(self, contents: Dict[str, str]) -> List[str]:
        risks: List[str] = []
        for name, body in contents.items():
            for match in _TODO_RE.finditer(body):
                risks.append(f"{name}: contains `{match.group(0)}` marker.")
                break
            if name.endswith(".md") and len(body.strip()) < 80:
                risks.append(f"{name}: very short ({len(body.strip())} chars) - likely a stub.")
        # Combined risk: review.md missing required artifacts.
        required_md = {"architecture.md", "brand.md", "positioning.md"}
        missing = sorted(required_md - set(contents))
        if missing:
            risks.append("Missing core artifact(s): " + ", ".join(missing))
        return risks

    def _verdict(
        self,
        completeness: List[Dict[str, Any]],
        consistency: List[str],
        risks: List[str],
    ) -> tuple[str, int]:
        total = max(1, len(completeness))
        present = sum(1 for c in completeness if c["present"])
        completeness_pct = present / total
        # Penalize risks and bad consistency notes.
        risk_penalty = min(40, len(risks) * 8)
        bad_consistency = [n for n in consistency if not n.startswith("No consistency")]
        consistency_penalty = min(20, len(bad_consistency) * 4)
        score = int(round(completeness_pct * 100)) - risk_penalty - consistency_penalty
        score = max(0, min(100, score))
        if score >= 75 and not any("Missing core" in r for r in risks):
            verdict = "go"
        elif score >= 50:
            verdict = "go-with-fixes"
        else:
            verdict = "no-go"
        return verdict, score

    def _render_review_md(
        self,
        artifact_dir: Path,
        files: List[Path],
        completeness: List[Dict[str, Any]],
        consistency: List[str],
        risks: List[str],
        verdict: str,
        score: int,
    ) -> str:
        out: List[str] = []
        out.append(f"# Review - {artifact_dir.name}")
        out.append("")
        out.append(f"**Verdict:** `{verdict}`  **Score:** {score}/100")
        out.append("")
        out.append("## Files reviewed")
        out.append("")
        if files:
            for f in files:
                out.append(f"- `{f.name}` ({f.stat().st_size} bytes)")
        else:
            out.append("- _(none)_")
        out.append("")
        out.append("## Completeness checklist")
        out.append("")
        for item in completeness:
            box = "[x]" if item["present"] else "[ ]"
            out.append(f"- {box} {item['name']}")
        out.append("")
        out.append("## Consistency notes")
        out.append("")
        for n in consistency:
            out.append(f"- {n}")
        out.append("")
        out.append("## Risks")
        out.append("")
        if risks:
            for r in risks:
                out.append(f"- {r}")
        else:
            out.append("- None detected.")
        out.append("")
        out.append("## Recommendation")
        out.append("")
        if verdict == "go":
            out.append("Ship it. Spot-check before merging to main.")
        elif verdict == "go-with-fixes":
            out.append("Address risks above, then ship. Don't block on stylistic items.")
        else:
            out.append("Do not ship. Fix missing core artifacts and re-run review.")
        out.append("")
        return "\n".join(out)
