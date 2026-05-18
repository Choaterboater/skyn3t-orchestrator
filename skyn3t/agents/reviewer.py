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

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

if TYPE_CHECKING:
    from skyn3t.core.messaging import AgentMessage

_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)$", re.MULTILINE)
_TODO_RE = re.compile(r"\b(TODO|FIXME|TBD|XXX)\b")
_CTA_HINTS = ("cta", "call to action", "get started", "sign up", "start free", "buy", "try")
_SCORE_RE = re.compile(r"(?:score|rating)[^\d]{0,15}(\d{1,3})", re.IGNORECASE)

logger = logging.getLogger(__name__)
_LLM_REVIEW_TIMEOUT_SECONDS = 180.0
_LLM_CRITIQUE_TIMEOUT_SECONDS = 90.0


def _llm_bucket_ceiling(score: int) -> int:
    if score < 50:
        return 49
    if score < 75:
        return 74
    return 100


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
        artifact_dir = self.resolve_artifact_dir(data.get("artifact_dir"))
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
            # Don't let a perfect heuristic checklist promote a middling
            # LLM review into a higher verdict bucket. The blend may still
            # improve the score within the same bucket, but a 69-quality
            # artifact should not become a `go`.
            blended = min(blended, _llm_bucket_ceiling(llm_score))
        else:
            blended = heuristic_score
        blended = max(0, min(100, blended))

        # Hard guard: if the brief explicitly asks for runnable code
        # ("build a … app/dashboard/site/api/script/cli with React/FastAPI/etc.")
        # but the artifact dir contains zero source files, the run shipped
        # docs-only and should NOT pass review. v52 surfaced this — planner
        # dropped CodeAgent, reviewer scored docs 100/100, run claimed `done`.
        if self._brief_implies_code(brief) and not self._has_source_files(artifact_dir):
            risks.append(
                "Missing core: brief asks for runnable code but no source "
                "files were produced (docs-only output)."
            )
            blended = min(blended, 30)
            verdict = "no-go"
        # Re-derive verdict from blended score, keeping ReviewWatcher-compatible
        # lowercase strings.
        elif blended >= 75 and not any("Missing core" in r for r in risks):
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

    async def critique(
        self,
        *,
        brief: str,
        artifact_dir: Path,
        stage_name: str,
        produced_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Critique a single stage's output for inter-agent conversation.

        Returns a dict with ``has_issues`` (bool), ``issues`` (list of
        dicts with file/line/problem/fix), and ``critique_text`` (str).
        This is narrower than a full review — it focuses on the 1-3 most
        important issues that would make the next stage's work worse.
        """
        files = self._artifact_files(artifact_dir)
        if produced_files:
            wanted = set(produced_files)
            files = [p for p in files if self._artifact_name(artifact_dir, p) in wanted]

        contents: Dict[str, str] = {}
        for p in files:
            try:
                contents[self._artifact_name(artifact_dir, p)] = p.read_text(encoding="utf-8")
            except Exception:
                contents[self._artifact_name(artifact_dir, p)] = ""

        # Run heuristics on just the stage's files.
        completeness = self._completeness_checklist(contents)
        consistency = self._consistency_notes(contents)
        risks = self._risks(contents)
        heuristic_verdict, _ = self._verdict(completeness, consistency, risks)

        # LLM critique pass — focused on actionable blockers.
        llm_critique = await self._llm_critique(
            brief=brief, contents=contents, stage_name=stage_name
        )

        issues: List[Dict[str, Any]] = []
        # Parse structured issues from LLM critique if available.
        if llm_critique:
            for line in llm_critique.splitlines():
                m = re.match(r"^\d+\.\s*([^:]+):\s*(.+?)(?:\s*→\s*(.+))?$", line.strip())
                if m:
                    issues.append({
                        "file": m.group(1).strip(),
                        "problem": m.group(2).strip(),
                        "fix": (m.group(3) or "").strip(),
                    })

        has_issues = bool(issues) or heuristic_verdict == "no-go"
        return {
            "has_issues": has_issues,
            "issues": issues,
            "critique_text": llm_critique or "",
            "heuristic_verdict": heuristic_verdict,
        }

    async def _llm_critique(
        self,
        *,
        brief: str,
        contents: Dict[str, str],
        stage_name: str,
    ) -> Optional[str]:
        """Run a focused LLM critique for a single stage."""
        if not contents:
            return None

        PER_FILE_CAP = 12000
        TOTAL_BUDGET = 80000
        snippets: List[Tuple[str, str]] = []
        for name, body in contents.items():
            s = (body or "").strip()
            if len(s) > PER_FILE_CAP:
                s = s[:PER_FILE_CAP] + "\n... [truncated]"
            snippets.append((name, s))
        total = sum(len(s) for _, s in snippets)
        if total > TOTAL_BUDGET and snippets:
            ratio = TOTAL_BUDGET / total
            scaled: List[Tuple[str, str]] = []
            for name, s in snippets:
                limit = max(200, int(len(s) * ratio))
                if len(s) > limit:
                    s = s[:limit] + "\n... [truncated to fit budget]"
                scaled.append((name, s))
            snippets = scaled
        chunks = [f"### {name}\n{s}" for name, s in snippets]
        files_block = "\n\n".join(chunks)
        brief_lower = (brief or "").lower()
        visual_focus = (
            stage_name == "code"
            and any(
                cue in brief_lower
                for cue in (
                    "dashboard",
                    "ui",
                    "ux",
                    "frontend",
                    "website",
                    "landing page",
                    "theme",
                    "design",
                    "visual",
                )
            )
        )
        visual_instructions = ""
        if visual_focus:
            visual_instructions = (
                "Prioritize visual-product issues too: weak hierarchy/spacing, missing responsive "
                "behavior, absent hover/focus/active states, and dashboards that feel empty because "
                "there is no realistic sample/live-like data shown in the initial view.\n\n"
            )

        role = (
            f"You are a senior reviewer critiquing the `{stage_name}` stage "
            f"of a build pipeline. Your job is to find the 1-3 MOST IMPORTANT "
            f"issues — things that would make the next stage's work worse if "
            f"not fixed. Be specific, name files/sections, and propose "
            f"concrete fixes. If the output is solid, reply with exactly: "
            f"NO_ISSUES.\n\n"
            f"{visual_instructions}"
            f"Brief:\n{brief[:2000]}\n\n"
            f"Artifacts:\n{files_block}\n\n"
            f"List up to 3 critical issues, each as a single line:\n"
            f"1. <file/section>: <problem> → <fix>\n"
            f"2. ...\n\n"
            f"Or reply NO_ISSUES."
        )
        out = await self._llm_generate(
            role_prompt=role,
            brief=brief or "(no brief provided)",
            fallback="",
            timeout_seconds=_LLM_CRITIQUE_TIMEOUT_SECONDS,
            purpose=f"{stage_name} critique",
        )
        out = (out or "").strip()
        if not out or "NO_ISSUES" in out[:30].upper():
            return None
        return out

    def on_message(self, msg: "AgentMessage") -> Optional["AgentMessage"]:
        """Handle incoming critique requests via MessageBus."""
        if msg.kind == "request" and msg.payload.get("intent") == "critique":
            # Synchronous handler — the actual async critique work is done
            # by the caller (runner) calling critique() directly for now.
            # This hook exists so the bus layer can route requests.
            return None
        return None

    # ------------------------------------------------------------------
    # LLM helper
    # ------------------------------------------------------------------
    @staticmethod
    def _artifact_name(artifact_dir: Path, path: Path) -> str:
        return path.relative_to(artifact_dir).as_posix()

    # Directory names anywhere in the path that mark third-party / build
    # output. Walking into these poisons both the heuristic (every TODO
    # in @babel/* etc. costs 8 points) and the LLM context (real source
    # files get scaled down to ~200 chars to make room for vendor code).
    _SKIP_DIR_PARTS = frozenset({
        "node_modules", "dist", "build", ".git", ".next", ".turbo",
        ".cache", ".parcel-cache", ".vite", ".svelte-kit", ".nuxt",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        "venv", ".venv", "env", "target", "out",
    })
    # Filenames that aren't worth feeding to the reviewer.
    _SKIP_FILE_NAMES = frozenset({
        "review.md",
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "poetry.lock", "Pipfile.lock", "Cargo.lock", "composer.lock",
    })

    @classmethod
    def _should_skip_artifact(cls, path: Path, artifact_dir: Path) -> bool:
        try:
            rel = path.relative_to(artifact_dir)
        except ValueError:
            rel = path
        if cls._SKIP_DIR_PARTS.intersection(rel.parts):
            return True
        if path.name in cls._SKIP_FILE_NAMES:
            return True
        return False

    def _artifact_files(self, artifact_dir: Path) -> List[Path]:
        return sorted(
            (
                path
                for path in artifact_dir.rglob("*")
                if path.is_file() and not self._should_skip_artifact(path, artifact_dir)
            ),
            key=lambda path: self._artifact_name(artifact_dir, path),
        )

    # Source-file extensions counted as "real code" for the docs-only guard.
    _CODE_EXTS = frozenset({
        ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".py", ".rb", ".go", ".rs", ".java", ".kt", ".swift",
        ".html", ".vue", ".svelte",
    })

    @classmethod
    def _has_source_files(cls, artifact_dir: Path) -> bool:
        """True iff the artifact dir (recursively) contains at least one
        file with a code extension. ``scaffold/`` is the conventional
        subdir, but this scans everywhere to be tolerant of layout drift.
        """
        if not artifact_dir.exists():
            return False
        for p in artifact_dir.rglob("*"):
            if not p.is_file():
                continue
            if cls._should_skip_artifact(p, artifact_dir):
                continue
            if p.suffix in cls._CODE_EXTS:
                return True
        return False

    @staticmethod
    def _brief_implies_code(brief: str) -> bool:
        """Mirror of planner._should_force_code_agent's "this brief asks
        for runnable software" logic. Kept as a local copy so the reviewer
        doesn't import from studio.planner (one-way dep).
        """
        text = (brief or "").strip().lower()
        if not text:
            return False
        # Pure-docs intent ("write a readme", "draft a spec") opts out.
        if re.search(
            r"^\s*(?:write|draft|produce|prepare|compose)\s+(?:an?\s+|the\s+)?"
            r"(?:readme|spec|specification|brief|plan|proposal|roadmap|"
            r"analysis|research|blog\s+post|email|summary|report|writeup)\b",
            text,
        ):
            return False
        # Strong "build a software thing" signal.
        if re.search(
            r"\b(?:build|create|make|ship|launch|scaffold|generate|prototype|"
            r"develop|implement)(?:\s+\S+){0,6}\s+"
            r"(?:app|site|website|api|backend|frontend|service|tool|script|"
            r"cli|bot|dashboard|extension|game)\b",
            text,
        ):
            return True
        # Stack-name signal: brief mentions a code stack/framework.
        if re.search(
            r"\b(?:react|vite|next(?:\.js)?|fastapi|flask|express|node(?:\.js)?|"
            r"typescript|python|swift|rust|go(?:lang)?|django|svelte|vue)\b",
            text,
        ):
            return True
        return False

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
        timeout_seconds: float = _LLM_REVIEW_TIMEOUT_SECONDS,
        purpose: str = "review",
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
            try:
                skills_block = self.load_skills_for_prompt(
                    tags=["reviewer", "critique", "quality"],
                    limit=2,
                )
                if skills_block:
                    prompt = prompt + skills_block
            except Exception:
                pass
            out = str(
                await asyncio.wait_for(
                    client.complete(prompt, max_tokens=max_tokens, temperature=0.2),
                    timeout=timeout_seconds,
                )
            )
            if out and "[deterministic-stub]" not in out and len(out.strip()) > 80:
                return out.strip()
        except asyncio.TimeoutError:
            logger.warning(
                "reviewer llm_generate timed out after %.1fs for %s; using fallback",
                timeout_seconds,
                purpose,
            )
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

        # Build a "Files" block. Caps used to be 1500/30000 — that
        # truncated App.jsx (37KB) to its first 1500 chars and the LLM
        # confabulated the rest, producing a review of files it never
        # actually saw. Subscription-backed CLIs (kimi/claude/copilot)
        # have plenty of context budget for full artifacts now.
        TOTAL_BUDGET = 100000  # ~25K tokens of artifact text
        PER_FILE_CAP = 12000   # full App.jsx fits in one piece
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
            max_tokens=8000,
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
