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
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Tuple

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

if TYPE_CHECKING:
    from skyn3t.core.messaging import AgentMessage

_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)$", re.MULTILINE)
_TODO_RE = re.compile(r"\b(TODO|FIXME|TBD|XXX)\b")
_CTA_HINTS = ("cta", "call to action", "get started", "sign up", "start free", "buy", "try")
# Total score. Requires the digit be ON THE SAME LINE as "Score:" or
# "Rating:" so we don't slurp across the new sub-score lines (e.g. a
# match against "## 5. Score\nCompleteness: 18" would otherwise return 18).
_SCORE_RE = re.compile(
    r"(?:score|rating)\b[ \t:\-–—]*?(\d{1,3})\b(?!\s*/\s*25)",
    re.IGNORECASE,
)
# Fallback total-score patterns for models that drop the "Score:" label.
# Matches "75/100" or "75 out of 100" but never a "/25" sub-score line.
_SCORE_OUT_OF_100_RE = re.compile(
    r"\b(\d{1,3})\s*(?:/|out\s+of)\s*100\b",
    re.IGNORECASE,
)
# Per-axis regexes for the 4-axis structured rubric. Each axis scores
# /25 and the four sum into the /100 total. We accept "Completeness:
# 18/25", "Completeness: 18", and "Completeness 18 / 25" — graders
# don't always follow the exact format, especially smaller models.
_SUB_SCORE_AXES = ("completeness", "correctness", "consistency", "packaging")
_SUB_SCORE_RES = {
    axis: re.compile(
        # Accept space, colon, hyphen, em-dash, or en-dash as a separator
        # between the axis label and its score.
        rf"\b{axis}\b[\s:\-–—]*?(\d{{1,3}})\s*(?:/\s*25)?",
        re.IGNORECASE,
    )
    for axis in _SUB_SCORE_AXES
}

logger = logging.getLogger(__name__)
_LLM_REVIEW_TIMEOUT_SECONDS = 180.0
_LLM_CRITIQUE_TIMEOUT_SECONDS = 90.0


def _find_compose_file(project_dir: Path) -> Optional[Path]:
    """Return the first supported Compose manifest in ``project_dir``."""
    for candidate in (
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ):
        path = project_dir / candidate
        if path.is_file():
            return path
    return None


def _has_packaging_env_vars(artifact_dir: Path, scaffold_dir: Path) -> bool:
    """Mirror PackagingAgent's env-var scan when deciding if web config UI is needed."""
    try:
        from skyn3t.agents.env_scanner import scan as scan_env
    except Exception:
        # Fall back to the stricter behavior if the scanner can't load.
        return True

    if scan_env(artifact_dir).vars:
        return True
    try:
        scaffold_nested_in_artifact = scaffold_dir.relative_to(artifact_dir) != Path(".")
    except ValueError:
        scaffold_nested_in_artifact = False
    if (
        scaffold_dir != artifact_dir
        and scaffold_dir.is_dir()
        and not scaffold_nested_in_artifact
        and scan_env(scaffold_dir).vars
    ):
        return True
    return False


def _llm_bucket_ceiling(score: int) -> int:
    """Deprecated — bucket ceilings (49 / 74 / 100) used to clamp the
    verdict-gate score so a strong heuristic couldn't paper over a weak
    LLM review. That floor masked real progress between runs (every
    build with llm<50 displayed the same ``49`` no matter how it
    changed) and the user removed it. Kept as a no-op so any
    out-of-tree callers don't break."""
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
        # Pass the scaffold dir (or artifact dir if no scaffold) so CLI
        # backends' tool calls see the actual files instead of an empty
        # /tmp sandbox — otherwise the LLM scores against a phantom
        # "scaffold is empty" reading.
        scaffold_dir = artifact_dir / "scaffold"
        llm_cwd = scaffold_dir if scaffold_dir.exists() else artifact_dir
        llm_review_md, llm_score, llm_sub_scores = await self._llm_review(
            brief=brief, contents=contents, cwd=str(llm_cwd),
        )
        llm_review_md = self._sanitize_llm_review_md(llm_review_md)

        # Packaging axis (0-10) — does this scaffold ship as a runnable
        # product? Scored against the project's detected stack family
        # so a CLI tool doesn't get docked for missing a Settings UI.
        # If the run intentionally skipped PackagingAgent (extra=
        # {"packaging_enabled": False}), don't grade against artifacts
        # the pipeline was told not to generate.
        packaging_enabled = data.get("packaging_enabled") is not False
        if packaging_enabled:
            packaging_score, packaging_gaps, packaging_family = self._packaging_score(artifact_dir)
        else:
            packaging_score, packaging_gaps, packaging_family = None, [], None

        networking_report = None
        try:
            from skyn3t.intelligence.networking_quality import evaluate_networking_quality

            networking_report = evaluate_networking_quality(
                brief=brief,
                contents=contents,
                artifact_dir=artifact_dir,
            )
        except Exception:
            logger.debug("networking quality rubric failed", exc_info=True)

        # Blend scores. When packaging is enabled, weight LLM/heuristic/
        # packaging at 54/36/10 (or 90/10 heuristic+packaging if no LLM).
        # When disabled, drop the packaging axis entirely and rescale
        # to 60/40 (or 100% heuristic if no LLM) so the missing 10%
        # doesn't silently penalize the score.
        if packaging_enabled:
            packaging_pct = (packaging_score or 0) * 10  # rescale 0-10 to 0-100
            if llm_score is not None:
                blended = int(round(
                    llm_score * 0.54
                    + heuristic_score * 0.36
                    + packaging_pct * 0.10
                ))
            else:
                blended = int(round(heuristic_score * 0.90 + packaging_pct * 0.10))
        else:
            if llm_score is not None:
                blended = int(round(llm_score * 0.60 + heuristic_score * 0.40))
            else:
                blended = int(round(heuristic_score * 1.00))
        blended = max(0, min(100, blended))
        # Verdict gate used to clamp `blended` into the bucket implied
        # by the LLM's own score (llm<50 → cap at 49). That floor
        # erased real run-over-run progress — every below-50 LLM
        # review displayed the same `49` even when the underlying
        # number moved from 28 → 42. Floor removed by user request;
        # verdict_score now equals the real blended score, and the
        # verdict text below buckets that single value directly. The
        # "Missing core" hard guard right after still caps to 30 so a
        # docs-only build can't sneak past on heuristic alone.
        verdict_score = blended

        # Surface packaging gaps as soft risks so the reviewer markdown
        # explains why the score landed where it did. Skip when packaging
        # was intentionally disabled — the gaps reflect files the pipeline
        # chose not to generate, not actual quality issues.
        if packaging_enabled and packaging_gaps:
            for gap in packaging_gaps:
                risks.append(f"Packaging: {gap}")
        if networking_report is not None and networking_report.applicable:
            for gap in networking_report.gaps:
                risks.append(gap)
            blended = min(blended, int(networking_report.score))
            verdict_score = min(verdict_score, int(networking_report.score))

        # H26: cap the reviewer score when objective verification failed.
        # A high LLM/heuristic score must not overrule "the scaffold didn't
        # build/boot/integrate".
        objective = (data or {}).get("objective_verification") or {}
        failed_objective = [
            name
            for name, record in objective.items()
            if isinstance(record, dict)
            and str(record.get("verdict") or "").lower() != "yes"
        ]
        if failed_objective:
            risks.append(
                "Objective verification failed: "
                + ", ".join(failed_objective)
                + " — reviewer score capped."
            )
            blended = min(blended, 49)
            verdict_score = min(verdict_score, 49)

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
            verdict_score = min(verdict_score, 30)
            verdict = "no-go"
        # Verdict text buckets the real (un-floored) verdict_score.
        # ReviewWatcher consumes the lowercase strings unchanged.
        elif verdict_score >= 80 and not any("Missing core" in r for r in risks):
            verdict = "go"
        elif verdict_score >= (70 if self._brief_implies_visual_ui(brief, scaffold_dir) else 60):
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
            verdict_score=verdict_score,
            llm_review_md=llm_review_md,
            heuristic_score=heuristic_score,
            llm_score=llm_score,
            llm_sub_scores=llm_sub_scores,
            packaging_score=packaging_score,
            packaging_gaps=packaging_gaps,
            packaging_family=packaging_family,
            networking_report=networking_report.to_dict()
            if networking_report is not None and networking_report.applicable
            else None,
        )
        review_path = artifact_dir / "review.md"
        review_path.write_text(review_md, encoding="utf-8")
        await self.think(f"wrote {review_path.name} (verdict: {verdict})")

        if next_agent:
            await self.send_message(
                to=next_agent,
                kind="info",
                content=f"{self.name} done; artifacts in {artifact_dir}",
                payload={"verdict": verdict, "score": verdict_score, "review": str(review_path)},
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
                # output["score"] feeds the downstream
                # REVIEWER_SCORE_THRESHOLD=80 composite gate in
                # _finalize_project_outcome. After the bucket-floor
                # removal, verdict_score == blended; we still emit
                # score_unclamped for any out-of-tree consumer that
                # was reading the old name.
                "score": verdict_score,
                "score_unclamped": blended,
                "summary": f"Review complete: {verdict} ({verdict_score}/100).",
                "sub_scores": llm_sub_scores,
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
        try:
            skills_block = self.load_skills_for_prompt(
                tags=["reviewer", "critique", "quality", stage_name],
                limit=3,
            )
            if skills_block:
                role = role + skills_block
        except Exception:
            pass
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

    @classmethod
    def _walk_artifact_files(cls, artifact_dir: Path) -> Iterator[Path]:
        if not artifact_dir.exists():
            return
        for dirpath, dirnames, filenames in os.walk(artifact_dir):
            dirnames[:] = sorted(d for d in dirnames if d not in cls._SKIP_DIR_PARTS)
            base = Path(dirpath)
            for filename in sorted(filenames):
                path = base / filename
                if path.name in cls._SKIP_FILE_NAMES:
                    continue
                yield path

    def _artifact_files(self, artifact_dir: Path) -> List[Path]:
        return sorted(
            self._walk_artifact_files(artifact_dir),
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
        for p in cls._walk_artifact_files(artifact_dir):
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
    def _brief_implies_visual_ui(brief: str, scaffold_dir: Path) -> bool:
        text = (brief or "").strip().lower()
        if not text or not scaffold_dir.exists():
            return False
        return any(
            cue in text
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
                "polished",
                "habit",
                "tracker",
                "app",
            )
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
        timeout_seconds: float = _LLM_REVIEW_TIMEOUT_SECONDS,
        purpose: str = "review",
        cwd: Optional[str] = None,
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
                "4. What's an honest score (0-100) that reflects what you actually saw?\n"
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
                    client.complete(
                        prompt, max_tokens=max_tokens, temperature=0.2, cwd=cwd,
                    ),
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

    @staticmethod
    def _parse_total_score(text: str) -> Optional[int]:
        """Extract a 0-100 total score from a free-form LLM review.

        Tries the keyworded ``Score:``/``Rating:`` line first, then any
        ``NN/100`` / ``NN out of 100`` phrasing. Cheaper models often drop
        the exact ``Score:`` label; without this fallback a perfectly good
        LLM review is discarded and the blend collapses to a deterministic
        heuristic-only score (~53), which silently tanks the stack trend.
        """
        if not text:
            return None
        m = _SCORE_RE.search(text)
        if m:
            try:
                v = int(m.group(1))
                if 0 <= v <= 100:
                    return v
            except ValueError:
                pass
        for m2 in _SCORE_OUT_OF_100_RE.finditer(text):
            try:
                v = int(m2.group(1))
            except ValueError:
                continue
            if 0 <= v <= 100:
                return v
        return None

    async def _llm_review(
        self,
        *,
        brief: str,
        contents: Dict[str, str],
        cwd: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[int], Optional[Dict[str, int]]]:
        """Run an LLM review pass.

        Returns ``(markdown body, total_score, sub_scores)``:
          * ``total_score``: 0..100 final score the LLM gave (or computed
            from sub-scores if it omitted the total).
          * ``sub_scores``: 4-axis dict
            {completeness, correctness, consistency, packaging}, each /25.
            ``None`` if the LLM didn't follow the new format — callers
            fall back to using only ``total_score``.

        ``cwd`` is forwarded to the LLM CLI backend so its file-inspection
        tool calls see the real scaffold instead of an empty sandbox.
        """
        if not contents:
            return None, None, None

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
            "5. Score: four sub-scores plus the final total. Use EXACTLY this format,\n"
            "   one per line:\n"
            "       Completeness: NN/25   # planned artifacts present, non-empty, no stubs\n"
            "       Correctness:  NN/25   # code/content actually does what it claims\n"
            "       Consistency:  NN/25   # palette/voice/architecture align across files\n"
            "       Packaging:    NN/25   # README, env, build/boot artifacts present\n"
            "       Score:        NN/100  # sum of the four\n"
            "   Be honest, not generous. A stubbed entrypoint is not 'completeness=25'.\n"
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
            cwd=cwd,
        )
        if not out:
            return None, None, None

        # Extract sub-scores (Completeness/Correctness/Consistency/Packaging,
        # each /25). The LLM is asked to produce all four. If a model
        # ignores the format or only partially complies, sub_scores is
        # None and we fall back to the single-score regex below.
        sub_scores: Dict[str, int] = {}
        for axis, rx in _SUB_SCORE_RES.items():
            m_axis = rx.search(out)
            if not m_axis:
                continue
            try:
                v_axis = int(m_axis.group(1))
            except ValueError:
                continue
            if 0 <= v_axis <= 25:
                sub_scores[axis] = v_axis

        # Total score. Prefer the explicit `Score: NN/100` line, then any
        # `NN/100` phrasing, then summing the four sub-scores.
        score: Optional[int] = self._parse_total_score(out)
        if score is None and len(sub_scores) == 4:
            total = sum(sub_scores.values())
            if 0 <= total <= 100:
                score = total
        # Partial-axis recovery: cheaper models sometimes emit only 2-3 axes
        # and no parseable total. Scale the mean axis (/25) to /100 instead of
        # discarding the whole LLM review — without this the blend collapses to
        # a deterministic heuristic-only ~53 and tanks the stack's score trend.
        if score is None and 2 <= len(sub_scores) < 4:
            mean_axis = sum(sub_scores.values()) / len(sub_scores)
            score = max(0, min(100, int(round(mean_axis * 4))))

        # Only return sub_scores when all four are present — partial
        # coverage isn't useful downstream and would mislead the rubric
        # display in review.md.
        full_sub_scores = sub_scores if len(sub_scores) == 4 else None
        return out, score, full_sub_scores

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

    # ------------------------------------------------------------------
    # Packaging axis — 0-10 score for "is this scaffold a runnable product?"
    # ------------------------------------------------------------------

    def _packaging_score(self, artifact_dir: Path) -> Tuple[int, List[str], Optional[str]]:
        """Score the project's packaging completeness against its family.

        Returns ``(score 0-10, list_of_gaps, family)``. A perfect score
        means: README is non-trivial, the right per-family artifacts
        exist, .gitignore is present. Tier-aware: a CLI tool doesn't
        get docked for missing a Settings UI; a web app does.

        Late-imported so the reviewer module stays decoupled from
        packaging (which lives in a sibling agent module).
        """
        try:
            from skyn3t.agents.stack_detector import detect as detect_stack
        except Exception:
            return 0, ["stack_detector unavailable — packaging not scored"], None

        if not artifact_dir.is_dir():
            return 0, ["Artifact directory does not exist"], "unknown"

        detection = detect_stack(artifact_dir)
        family = detection.family

        score = 0
        gaps: List[str] = []
        scaffold_dir = artifact_dir / "scaffold"

        # README: present + non-trivial (>200 chars). 3 points across
        # all tiers.
        readme = artifact_dir / "README.md"
        if readme.is_file():
            try:
                if len(readme.read_text(encoding="utf-8")) > 200:
                    score += 3
                else:
                    gaps.append("README is a stub")
            except Exception:
                gaps.append("README unreadable")
        else:
            gaps.append("No README.md at project root")

        # .gitignore: present. 2 points. Lots of scaffolds ship without
        # one and operators commit node_modules / .env by accident.
        gitignore = artifact_dir / ".gitignore"
        if gitignore.is_file():
            score += 2
        else:
            gaps.append("No .gitignore at project root")

        # Family-specific artifacts: 5 points total.
        if family == "web":
            # PackagingAgent intentionally skips Settings.jsx/useConfig for
            # zero-config apps; don't dock those runs for omitting a UI that
            # would be pure scaffolding cruft.
            if not _has_packaging_env_vars(artifact_dir, scaffold_dir):
                score += 5
            else:
                settings = scaffold_dir / "src" / "Settings.jsx"
                use_config = scaffold_dir / "src" / "hooks" / "useConfig.js"
                if settings.is_file():
                    score += 3
                else:
                    gaps.append("No in-app Settings.jsx (users will be stuck on a .env wall)")
                if use_config.is_file():
                    score += 2
                else:
                    gaps.append("No useConfig hook (Settings.jsx has nothing to persist into)")
        elif family == "server":
            dockerfile = artifact_dir / "Dockerfile"
            compose = _find_compose_file(artifact_dir)
            env_example = artifact_dir / ".env.example"
            if dockerfile.is_file():
                score += 2
            else:
                gaps.append("No Dockerfile (users have to write their own)")
            if compose is not None:
                score += 2
            else:
                gaps.append("No docker-compose.yml (no one-command quick start)")
            if env_example.is_file():
                score += 1
            else:
                gaps.append("No .env.example (operator has to discover env vars from source)")
        elif family == "fullstack":
            # 2 web + 2 server + 1 combo wiring
            docker_ok = (artifact_dir / "Dockerfile").is_file()
            compose_path = _find_compose_file(artifact_dir)
            compose_ok = compose_path is not None
            # Zero-config apps skip Settings UI (same logic as web family).
            if not _has_packaging_env_vars(artifact_dir, scaffold_dir):
                score += 2  # web layer auto-scores for zero-config
            else:
                web_ok = (scaffold_dir / "src" / "Settings.jsx").is_file()
                hook_ok = (scaffold_dir / "src" / "hooks" / "useConfig.js").is_file()
                if web_ok and hook_ok:
                    score += 2
                else:
                    gaps.append("Frontend missing Settings UI / useConfig (web layer)")
            if docker_ok and compose_ok:
                score += 2
            else:
                gaps.append("Backend missing Dockerfile / docker-compose (server layer)")
            # Combo wiring: frontend service in compose + API_BASE_URL
            # default in useConfig.
            wired = False
            if compose_path is not None:
                try:
                    compose_text = compose_path.read_text(encoding="utf-8")
                    if re.search(r"^\s*frontend\s*:", compose_text, re.MULTILINE):
                        wired = True
                except Exception:
                    pass
            if wired:
                score += 1
            else:
                gaps.append("Frontend not wired into docker-compose (no one-command start)")
        else:
            # Unknown family — can't grade family-specific artifacts, so
            # award the 5 points by default. The 0/2/3 for README +
            # gitignore still apply above, and any docs gaps still
            # surface there.
            score += 5

        return max(0, min(10, score)), gaps, family

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
        if score >= 80 and not any("Missing core" in r for r in risks):
            verdict = "go"
        elif score >= 60:
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
        verdict_score: Optional[int] = None,
        llm_review_md: Optional[str] = None,
        heuristic_score: Optional[int] = None,
        llm_score: Optional[int] = None,
        llm_sub_scores: Optional[Dict[str, int]] = None,
        packaging_score: Optional[int] = None,
        packaging_gaps: Optional[List[str]] = None,
        packaging_family: Optional[str] = None,
        networking_report: Optional[Dict[str, Any]] = None,
    ) -> str:
        out: List[str] = []
        out.append(f"# Review - {artifact_dir.name}")
        out.append("")
        # Show the unclamped blended score so users can see progress
        # between runs. When the bucket ceiling pulled the verdict down,
        # also show the gated value so the verdict isn't surprising.
        if verdict_score is not None and verdict_score != score:
            out.append(
                f"**Verdict:** `{verdict}`  **Score:** {score}/100  "
                f"_(verdict gate: {verdict_score}/100)_"
            )
        else:
            out.append(f"**Verdict:** `{verdict}`  **Score:** {score}/100")
        if heuristic_score is not None or llm_score is not None or packaging_score is not None:
            parts = []
            if heuristic_score is not None:
                parts.append(f"heuristic={heuristic_score}")
            if llm_score is not None:
                parts.append(f"llm={llm_score}")
            if packaging_score is not None:
                parts.append(f"packaging={packaging_score}/10")
            if parts:
                out.append(f"_Score breakdown: {', '.join(parts)}_")
        if llm_sub_scores:
            # Surface the 4-axis breakdown so a reader can see which
            # dimension dragged the score. Order is fixed for scanability.
            axis_parts = []
            for _axis in ("completeness", "correctness", "consistency", "packaging"):
                v = llm_sub_scores.get(_axis)
                if v is not None:
                    axis_parts.append(f"{_axis}={v}/25")
            if axis_parts:
                out.append(f"_LLM sub-scores: {', '.join(axis_parts)}_")
        if packaging_score is not None and (packaging_gaps or packaging_family):
            out.append("")
            family_label = packaging_family or "unknown"
            out.append(f"### Packaging ({family_label} tier): {packaging_score}/10")
            if packaging_gaps:
                for gap in packaging_gaps:
                    out.append(f"- ⚠️ {gap}")
            else:
                out.append("- ✓ All packaging artifacts present.")
        if networking_report:
            out.append("")
            out.append(
                "### Networking domain quality: "
                f"{networking_report.get('score')}/"
                f"{networking_report.get('max_score', 100)}"
            )
            gaps = networking_report.get("gaps") or []
            if gaps:
                for gap in gaps:
                    out.append(f"- ⚠️ {gap}")
            else:
                out.append("- ✓ Networking operator workflows covered.")
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
