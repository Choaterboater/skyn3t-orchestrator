"""Reviewer Agent - QA pass over an artifact directory.

Hybrid review. Walks every file in ``artifact_dir`` and combines:

1. Heuristic checks (file existence, empty headings, TODO markers, palette/copy
   consistency, JSON validity).
2. An LLM-driven senior-reviewer pass that reads the artifact contents and
   identifies completeness gaps, internal inconsistencies, weak claims, and
   missing CTAs - returning a 0-100 score plus a markdown review body.

The final score blends the LLM score with the heuristic score; the verdict
remains compatible with ReviewWatcher (lowercase ``go`` / ``go-with-fixes`` /
``no-go``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)$", re.MULTILINE)
_TODO_RE = re.compile(r"\b(TODO|FIXME|TBD|XXX)\b")
_CTA_HINTS = ("cta", "call to action", "get started", "sign up", "start free", "buy", "try")
_SCORE_RE = re.compile(r"(?:score|rating)[^\d]{0,15}(\d{1,3})", re.IGNORECASE)


class ReviewerAgent(BaseAgent):
    """Hybrid (heuristic + LLM) QA reviewer. Reads artifacts, writes review.md."""

    def __init__(
        self,
        name: str = "reviewer",
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="reviewer",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(AgentCapability(
            name="review",
            description="Hybrid heuristic + LLM review of an artifact directory.",
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

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")

        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip()
        artifact_dir = Path(data.get("artifact_dir") or ".")
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        files = self._artifact_files(artifact_dir)
        await self.think(f"reviewing {len(files)} artifact(s) in {artifact_dir}")

        contents: Dict[str, str] = {}
        for p in files:
            try:
                contents[self._artifact_name(artifact_dir, p)] = p.read_text(encoding="utf-8")
            except Exception:
                contents[self._artifact_name(artifact_dir, p)] = ""

        # Heuristics first - cheap and always available.
        expected_files = data.get("expected_artifacts")  # optional override from planner
        completeness = self._completeness_checklist(contents, expected_override=expected_files)
        consistency = self._consistency_notes(contents)
        risks = self._risks(contents, expected_files=expected_files)
        heuristic_verdict, heuristic_score = self._verdict(completeness, consistency, risks)

        # LLM pass - reads artifacts and produces a senior-reviewer narrative.
        llm_review_md, llm_score = await self._llm_review(brief=brief, contents=contents)
        llm_review_md = self._sanitize_llm_review_md(llm_review_md)

        # Blend scores. If LLM produced a usable score, weight it 60/40 with
        # the heuristic; otherwise fall back to the heuristic alone.
        if llm_score is not None:
            blended = int(round(llm_score * 0.6 + heuristic_score * 0.4))
        else:
            blended = heuristic_score
        blended = max(0, min(100, blended))

        # Re-derive verdict from blended score, keeping ReviewWatcher-compatible
        # lowercase strings.
        if blended >= 75 and not any("Missing core" in r for r in risks):
            verdict = "go"
        elif blended >= 50:
            verdict = "go-with-fixes"
        else:
            verdict = "no-go"

        review_md = self._render_review_md(
            artifact_dir=artifact_dir,
            files=files,
            completeness=completeness,
            consistency=consistency,
            risks=risks,
            verdict=verdict,
            score=blended,
            llm_review_md=llm_review_md,
            heuristic_score=heuristic_score,
            llm_score=llm_score,
        )
        review_path = artifact_dir / "review.md"
        review_path.write_text(review_md, encoding="utf-8")
        await self.think(f"wrote {review_path.name} (verdict: {verdict})")

        if next_agent:
            await self.send_message(
                to=next_agent,
                kind="info",
                content=f"{self.name} done; artifacts in {artifact_dir}",
                payload={"verdict": verdict, "score": blended, "review": str(review_path)},
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
                "score": blended,
                "summary": f"Review complete: {verdict} ({blended}/100).",
            },
        )

    # ------------------------------------------------------------------
    # LLM helper
    # ------------------------------------------------------------------
    @staticmethod
    def _artifact_name(artifact_dir: Path, path: Path) -> str:
        return path.relative_to(artifact_dir).as_posix()

    def _artifact_files(self, artifact_dir: Path) -> List[Path]:
        return sorted(
            (
                path
                for path in artifact_dir.rglob("*")
                if path.is_file() and path.name != "review.md"
            ),
            key=lambda path: self._artifact_name(artifact_dir, path),
        )

    @staticmethod
    def _sanitize_llm_review_md(review_md: Optional[str]) -> Optional[str]:
        if not review_md:
            return review_md

        cleaned: List[str] = []
        for line in review_md.splitlines():
            stripped = line.strip()
            plain = re.sub(r"[*_`]", "", stripped)
            plain = re.sub(r"^#+\s*", "", plain).strip()
            if re.match(r"(?i)^(?:\d+\.\s*)?(score|verdict)\s*:", plain):
                continue
            cleaned.append(line)

        while cleaned and not cleaned[0].strip():
            cleaned.pop(0)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        return "\n".join(cleaned).strip()

    async def _llm_generate(
        self,
        *,
        role_prompt: str,
        brief: str,
        fallback: str,
        max_tokens: int = 2500,
    ) -> str:
        try:
            client = self.get_llm() if hasattr(self, "get_llm") else None
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(
                    default_model=self.config.get("model"),
                    backend=self.config.get("backend"),
                    event_bus=self.event_bus,
                    caller_name=self.name,
                )
            cot_preamble = (
                "Reason step-by-step:\n"
                "1. Did the brief actually get addressed?\n"
                "2. Are the artifacts internally consistent (e.g. brand voice matches palette mood)?\n"
                "3. What's missing that a real user would notice immediately?\n"
                "4. What's the most generous fair score (0-100)?\n"
                "THEN write the review.\n\n"
            )
            prompt = (
                f"{cot_preamble}{role_prompt}\n\nBrief from user:\n{brief}\n\n"
                "Produce ONLY the markdown (or JSON if asked) - no code fences, no preamble."
            )
            out = await client.complete(prompt, max_tokens=max_tokens, temperature=0.2)
            if out and "[deterministic-stub]" not in out and len(out.strip()) > 80:
                return out.strip()
        except Exception:
            pass
        return fallback

    async def _llm_review(
        self,
        *,
        brief: str,
        contents: Dict[str, str],
    ) -> Tuple[Optional[str], Optional[int]]:
        """Run an LLM review pass. Returns (markdown body, score) or (None, None)."""
        if not contents:
            return None, None

        # Build a compact "Files" block. We cap each artifact (1500 chars) AND
        # the total combined size (TOTAL_BUDGET) so a project with many files
        # can't blow the model's context window. When over budget, every
        # artifact is fairly downsized rather than dropping the tail.
        TOTAL_BUDGET = 30000  # chars across all artifacts in the prompt
        PER_FILE_CAP = 1500
        items = list(contents.items())
        # Initial per-file truncation.
        snippets: List[Tuple[str, str]] = []
        for name, body in items:
            s = (body or "").strip()
            if len(s) > PER_FILE_CAP:
                s = s[:PER_FILE_CAP] + "\n... [truncated]"
            snippets.append((name, s))
        # If total still exceeds the budget, scale every file down equally.
        total = sum(len(s) for _, s in snippets)
        if total > TOTAL_BUDGET and snippets:
            ratio = TOTAL_BUDGET / total
            scaled: List[Tuple[str, str]] = []
            for name, s in snippets:
                limit = max(200, int(len(s) * ratio))  # never below 200 chars
                if len(s) > limit:
                    s = s[:limit] + "\n... [truncated to fit budget]"
                scaled.append((name, s))
            snippets = scaled
        chunks = [f"### {name}\n{s}" for name, s in snippets]
        files_block = "\n\n".join(chunks)

        role = (
            "You are a senior product reviewer doing a sharp pre-launch QA "
            "pass. Read every artifact below. Identify: completeness gaps, "
            "internal inconsistencies (e.g. a brand color claimed but not "
            "used, conflicting positioning across files), weak or "
            "unsubstantiated claims, missing CTAs, and any signal that the "
            "swarm misread the brief.\n\n"
            "Output a markdown review with these sections:\n"
            "1. Summary (2-3 sentences).\n"
            "2. Strengths (bullet list, max 5).\n"
            "3. Gaps & inconsistencies (bullet list).\n"
            "4. Weak claims / risks (bullet list).\n"
            "5. Score: a single line of the form `Score: NN/100`.\n"
            "Do not include a separate verdict line; the final verdict is blended outside your review.\n\n"
            f"Artifacts:\n{files_block}"
        )
        # Use a non-stub fallback so _llm_generate's gate triggers correctly
        # only when there's a real response.
        out = await self._llm_generate(
            role_prompt=role,
            brief=brief or "(no brief provided)",
            fallback="",  # Empty fallback - we handle missing LLM separately.
            max_tokens=2200,
        )
        if not out:
            return None, None

        # Extract score from the response. Be lenient about formatting.
        score: Optional[int] = None
        m = _SCORE_RE.search(out)
        if m:
            try:
                v = int(m.group(1))
                if 0 <= v <= 100:
                    score = v
            except ValueError:
                pass
        return out, score

    # ------------------------------------------------------------------
    # Heuristic checks (unchanged behaviour, kept compatible)
    # ------------------------------------------------------------------
    def _completeness_checklist(self, contents: Dict[str, str],
                                  expected_override: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        # If a caller supplies expected_override, use that exactly. Otherwise
        # infer expectations from what's already present + a soft default.
        # This makes the checklist template-aware: a frontend_redesign project
        # is no longer penalized for missing positioning.md, etc.
        if expected_override is not None:
            # Reviewer is the one writing review.md and excludes it from `contents`,
            # so it would always count itself as missing. Filter it out.
            expected = [n for n in expected_override if n != "review.md"]
        else:
            # Soft default: every produced file IS expected (already present).
            # Only add core artifacts to the expectations if at least one
            # related file is also present (proxy for "this template runs that stage").
            expected = sorted([n for n, b in contents.items() if b.strip()])
            related_groups = [
                ({"architecture.md", "tech_stack.json"}, "architecture"),
                ({"brand.md", "palette.json", "components.md"}, "design"),
                ({"positioning.md", "channel_plan.md", "launch_checklist.md"}, "marketing"),
                ({"market_scan.md", "business_model.md", "pitch_outline.md"}, "business"),
            ]
            for group, _label in related_groups:
                produced = group & set(contents)
                if produced:
                    # If ANY file from this group is present, expect ALL of them.
                    for name in group:
                        if name not in expected:
                            expected.append(name)
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

    def _risks(self, contents: Dict[str, str],
                expected_files: Optional[List[str]] = None) -> List[str]:
        risks: List[str] = []
        for name, body in contents.items():
            for match in _TODO_RE.finditer(body):
                risks.append(f"{name}: contains `{match.group(0)}` marker.")
                break
            if name.endswith(".md") and len(body.strip()) < 80:
                risks.append(f"{name}: very short ({len(body.strip())} chars) - likely a stub.")
        if expected_files:
            # Planner-driven: the override IS the truth. Don't run the older
            # hardcoded "missing core" template-group heuristics on top.
            missing_planned = [f for f in expected_files if f not in contents]
            if missing_planned:
                risks.append("Planner expected but missing: " + ", ".join(missing_planned))
        else:
            # Template-aware "missing core" — only flag a file as missing if a sibling
            # from the same artifact group IS present (which means the template ran
            # that stage but it didn't produce the expected file). Don't penalize
            # frontend_redesign for not having marketing artifacts.
            groups = [
                ("architecture",  {"architecture.md", "tech_stack.json"}),
                ("design",        {"brand.md", "palette.json", "components.md"}),
                ("marketing",     {"positioning.md", "channel_plan.md", "launch_checklist.md"}),
                ("business",      {"market_scan.md", "business_model.md", "pitch_outline.md"}),
            ]
            present_files = set(contents)
            for label, group in groups:
                produced = group & present_files
                if produced and produced != group:
                    missing = sorted(group - present_files)
                    risks.append(f"{label} stage produced partial output — missing: {', '.join(missing)}")
        return risks

    def _verdict(
        self,
        completeness: List[Dict[str, Any]],
        consistency: List[str],
        risks: List[str],
    ) -> Tuple[str, int]:
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
        llm_review_md: Optional[str] = None,
        heuristic_score: Optional[int] = None,
        llm_score: Optional[int] = None,
    ) -> str:
        out: List[str] = []
        out.append(f"# Review - {artifact_dir.name}")
        out.append("")
        out.append(f"**Verdict:** `{verdict}`  **Score:** {score}/100")
        if heuristic_score is not None or llm_score is not None:
            parts = []
            if heuristic_score is not None:
                parts.append(f"heuristic={heuristic_score}")
            if llm_score is not None:
                parts.append(f"llm={llm_score}")
            if parts:
                out.append(f"_Score breakdown: {', '.join(parts)}_")
        out.append("")
        if llm_review_md:
            out.append("## LLM review")
            out.append("")
            out.append(llm_review_md)
            out.append("")
        out.append("## Files reviewed")
        out.append("")
        if files:
            for f in files:
                out.append(
                    f"- `{self._artifact_name(artifact_dir, f)}` ({f.stat().st_size} bytes)"
                )
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
