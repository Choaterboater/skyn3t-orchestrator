"""High-level Project Studio orchestrator.

:class:`StudioRunner` takes a free-form brief plus a template key and
runs the corresponding pipeline of specialist agents, persisting a
project manifest and any artifacts they produce under the configured
projects root.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from skyn3t.config.settings import get_settings
from skyn3t.core.agent import AgentCapability, TaskRequest, TaskResult
from skyn3t.core.event_context import push_event_context
from skyn3t.studio.mission_setup import (
    augment_brief_with_mission_setup,
    mission_setup_stage_hints,
    normalize_mission_setup,
)
from skyn3t.studio.registry import get_agent
from skyn3t.studio.repo_target import (
    augment_brief_with_repo_target,
    normalize_repo_target,
    repo_target_stage_hints,
    resolve_repo_target,
)
from skyn3t.studio.templates import get_template

logger = logging.getLogger("skyn3t.studio")

_BUILD_FIX_LLM_TIMEOUT_SECONDS = 120.0


class StackShapeMismatchError(RuntimeError):
    """Raised when post-code stack validation finds inconsistent files.

    The runner uses this to bail intentionally rather than crash; the
    outer exception handler distinguishes it from a real crash so the
    user sees an accurate ``next_action`` instead of "runner crashed."
    """


class UnresolvedScaffoldStubError(RuntimeError):
    """Raised when generated TODO stubs remain after the consistency fix pass."""


class MissingPlannedFilesError(RuntimeError):
    """Raised when planned scaffold files are still missing after fix passes."""


class StudioRunner:
    """Run project templates end-to-end against a pool of specialist agents."""

    # Class-level semaphore so all StudioRunner instances share the same limit.
    # When users batch-submit many projects, the underlying CLI subprocesses
    # (Codex, planners, etc.) serialize on shared resources and stall. Capping
    # concurrency here keeps the dashboard responsive and projects making
    # forward progress instead of all wedging at once.
    _concurrency_sem: Optional[asyncio.Semaphore] = None
    MAX_CONCURRENT_PROJECTS = 3

    @classmethod
    def _get_sem(cls) -> asyncio.Semaphore:
        if cls._concurrency_sem is None:
            cls._concurrency_sem = asyncio.Semaphore(cls.MAX_CONCURRENT_PROJECTS)
        return cls._concurrency_sem

    def __init__(
        self,
        *,
        event_bus: Any,
        rag: Any = None,
        projects_root: Optional[Path] = None,
    ) -> None:
        self.event_bus = event_bus
        self.rag = rag
        self._retry_tasks: Set[asyncio.Task[Any]] = set()
        configured_root = projects_root if projects_root is not None else get_settings().projects_dir
        self.projects_root = Path(configured_root).expanduser()
        self.projects_root.mkdir(parents=True, exist_ok=True)
        # Reap projects that were "running" or "queued" when the server died.
        # Their async task is gone; they'll never progress on their own.
        self._reap_orphans()

    @staticmethod
    def _resolved_repo_target(value: Any) -> dict:
        return resolve_repo_target(value)

    def reserve_project(
        self,
        template_key: str,
        brief: str,
        *,
        slug: Optional[str] = None,
        mission_setup: Optional[dict] = None,
        repo_target: Optional[dict] = None,
        ) -> dict:
        """Create the initial queued manifest synchronously and return it."""
        template = get_template(template_key)
        setup = normalize_mission_setup(mission_setup)
        repo = self._resolved_repo_target(repo_target)
        slug_source = brief or template_key
        if repo["local_path"]:
            slug_source = f"{slug_source} {Path(repo['local_path']).name}"
        slug = slug or self._slugify(slug_source)
        artifact_dir = self.projects_root / slug
        artifact_dir.mkdir(parents=True, exist_ok=True)

        existing = self.get_project(slug)
        if existing is not None:
            return existing

        now = time.time()
        manifest: Dict[str, Any] = {
            "slug": slug,
            "template": template_key,
            "title": template.title,
            "brief": brief,
            "mission_setup": setup,
            "repo_target": repo,
            "stages": [],
            "artifacts": [],
            "quality_summary": None,
            "created_at": now,
            "status": "queued",
            "next_action": "Queued — waiting for a worker slot.",
            "workflow_summary": self._workflow_from_template(template, template_key),
        }
        self._append_history(
            manifest,
            "PROJECT_QUEUED",
            status="queued",
            message="Queued — waiting for a worker slot.",
        )
        self._save_manifest(artifact_dir, manifest)
        self._publish(
            "PROJECT_QUEUED",
            {"slug": slug, "template": template_key, "title": template.title},
        )
        return manifest

    def _reap_orphans(self) -> None:
        try:
            for p in self.projects_root.iterdir():
                if not p.is_dir():
                    continue
                mf = p / "project.json"
                if not mf.exists():
                    continue
                try:
                    d = json.loads(mf.read_text())
                except Exception:
                    continue
                if d.get("status") in ("running", "queued"):
                    d = self._normalize_manifest(d)
                    d["status"] = "interrupted"
                    d["error"] = None
                    d["completed_at"] = time.time()
                    d["current_stage"] = None
                    d["current_agent"] = None
                    self._clear_quality_summary(d)
                    d["next_action"] = "Project was interrupted because the server restarted."
                    self._append_history(
                        d,
                        "PROJECT_REAPED",
                        status="interrupted",
                        message="Server restarted before the queued/running project could finish.",
                    )
                    try:
                        self._save_manifest(p, d)
                    except Exception:
                        # Manifest write is the ONLY durable record of
                        # the "interrupted" state — losing it means the
                        # project shows up as "still running" forever
                        # in the dashboard after a restart.
                        logger.warning(
                            "reap_orphans: failed to persist interrupted "
                            "state for %s — project will look running on "
                            "next start", p, exc_info=True,
                        )
        except Exception:
            logger.exception("reap_orphans failed")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def start(
        self,
        template_key: str,
        brief: str,
        *,
        slug: Optional[str] = None,
        extra: Optional[dict] = None,
        mission_setup: Optional[dict] = None,
        repo_target: Optional[dict] = None,
    ) -> dict:
        """Execute the named template against ``brief`` and return the manifest."""
        slug = slug or self._slugify(brief or template_key)
        artifact_dir = self.projects_root / slug
        artifact_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.get_project(slug)
        try:
            setup = normalize_mission_setup(mission_setup)
            repo = self._resolved_repo_target(repo_target)
            # Enrich BEFORE planning so plan_pipeline / designer-skip /
            # extensibility detection / etc. see the category's default
            # features in the brief. The original "enrich after plan"
            # ordering meant the planner picked DesignerAgent for v29
            # before the "Homarr/dark theme" defaults were appended,
            # then the designer-skip post-process couldn't undo it
            # because... wait, yes it could — but the LLM-planner path
            # had already locked it in. Moved here so EVERY downstream
            # consumer sees the enriched text.
            raw_brief = brief
            enriched_brief = brief
            expanded_brief = brief
            category_defaults = None
            try:
                from skyn3t.agents.product_categories import (
                    enrich_brief,
                    expand_sparse_brief,
                )
                expanded_brief = expand_sparse_brief(brief)
                enriched_brief, category_defaults = enrich_brief(expanded_brief)
                if category_defaults and category_defaults.slug != "unknown":
                    logger.info(
                        "project %s classified as %s — added %d implicit "
                        "features + %d aesthetic defaults",
                        slug, category_defaults.slug,
                        len(category_defaults.implicit_features),
                        len(category_defaults.aesthetic_baseline),
                    )
            except Exception:
                logger.debug("category enrichment failed", exc_info=True)
            execution_profile = self._infer_execution_profile(raw_brief, extra)
            effective_brief = augment_brief_with_repo_target(
                augment_brief_with_mission_setup(enriched_brief, setup),
                repo,
            )
            template = get_template(template_key)
            # Dynamic mode: empty stages → ask the planner.
            if not template.stages and template_key == "auto":
                from skyn3t.studio.planner import plan_pipeline
                # Build an LLM client for the planner. Use the brainstorm agent's config
                # as a sensible default; falls back to deterministic if unavailable.
                llm_client = None
                try:
                    from skyn3t.adapters import LLMClient
                    llm_client = LLMClient(default_model=None, backend=None,
                                           event_bus=self.event_bus, caller_name="planner")
                except Exception:
                    pass
                planned = await plan_pipeline(brief=effective_brief, llm_client=llm_client)
                # Belt-and-braces guard: if the brief asks for runnable
                # software but no code-producing agent made it into the
                # plan, force CodeAgent in. v52 surfaced a case where the
                # planner's safety net silently failed to add CodeAgent to
                # a "build a Vite + React dashboard" brief, and the run
                # shipped docs-only with reviewer 100/100. This guard runs
                # after the planner so it cannot be silently bypassed.
                from skyn3t.studio.planner import PlannedStage, _should_force_code_agent
                _agents_in_plan = {p.agent for p in planned}
                if (
                    _should_force_code_agent(effective_brief)
                    and "CodeAgent" not in _agents_in_plan
                    and "CodeImproverAgent" not in _agents_in_plan
                ):
                    logger.warning(
                        "planner produced no code agent for a code-requiring "
                        "brief; injecting CodeAgent. plan was: %s",
                        [p.agent for p in planned],
                    )
                    insert_at = next(
                        (i for i, p in enumerate(planned) if p.agent == "ReviewerAgent"),
                        len(planned),
                    )
                    planned.insert(insert_at, PlannedStage(
                        name="code",
                        agent="CodeAgent",
                        capability="code_generation",
                        expected_artifact="(source files)",
                        rationale="injected by runner safety net (planner dropped code agent)",
                    ))
                # Convert PlannedStage list → a one-off Template instance for this run
                from skyn3t.studio.templates import StageSpec, Template
                converted_stages = [
                    StageSpec(name=p.name, agent=p.agent, capability=p.capability,
                               handoff_to=p.handoff_to,
                               input_extra={**p.input_extra,
                                            "expected_artifact": p.expected_artifact,
                                            "planned_rationale": p.rationale})
                    for p in planned
                ]
                template = Template(key="auto", title="Auto-planned",
                                   description=f"Dynamically planned for: {brief[:80]}",
                                   stages=converted_stages)
                expected_files: list = []
                for p in planned:
                    if not p.expected_artifact:
                        continue
                    for piece in p.expected_artifact.split(","):
                        name = piece.strip().strip("()")
                        if name and "." in name and " " not in name and "/" not in name:
                            if name not in expected_files:
                                expected_files.append(name)
                expected_files = [n for n in expected_files if n != "review.md"]
                extra = dict(extra or {})
                extra.setdefault("expected_artifacts", expected_files)
            if manifest is None:
                manifest = self.reserve_project(
                    template_key,
                    brief,
                    slug=slug,
                    mission_setup=setup,
                    repo_target=repo,
                )
            manifest = self._normalize_manifest(manifest)
            setup = normalize_mission_setup(
                mission_setup if mission_setup is not None else manifest.get("mission_setup")
            )
            repo_source = repo_target if repo_target is not None else manifest.get("repo_target")
            repo = self._resolved_repo_target(repo_source)
            # Re-resolve effective_brief now that we have setup/repo
            # from the manifest. Enrichment is already on enriched_brief
            # from the pre-plan step above; just thread it through here.
            effective_brief = augment_brief_with_repo_target(
                augment_brief_with_mission_setup(enriched_brief, setup),
                repo,
            )
            # Record product category on the manifest so the dashboard
            # can show "classified as homelab_dashboard" + the default
            # features are auditable.
            if category_defaults and category_defaults.slug != "unknown":
                manifest["product_category"] = category_defaults.slug
                manifest["product_category_label"] = category_defaults.label
            manifest["template"] = template_key
            manifest["title"] = template.title
            manifest["brief"] = brief
            manifest["brief_raw"] = raw_brief
            manifest["brief_expanded"] = bool((expanded_brief or "").strip() != (raw_brief or "").strip())
            manifest["execution_profile"] = execution_profile
            manifest["mission_setup"] = setup
            manifest["repo_target"] = repo
            manifest["workflow_summary"] = self._workflow_from_template(template, template_key)
            self._clear_quality_summary(manifest)

            sem = self._get_sem()
            async with sem:  # waits here if MAX_CONCURRENT_PROJECTS already running
                manifest["status"] = "running"
                manifest["started_at"] = manifest.get("started_at") or time.time()
                manifest["running_at"] = time.time()
                manifest["current_stage"] = None
                manifest["current_agent"] = None
                manifest["next_action"] = "SkyN3t is briefing the swarm."
                self._append_history(
                    manifest,
                    "PROJECT_STARTED",
                    status="running",
                    message="SkyN3t is briefing the swarm.",
                )
                self._save_manifest(artifact_dir, manifest)

                self._publish(
                    "PROJECT_STARTED",
                    {"slug": slug, "template": template_key, "title": template.title},
                )

                return await self._run_pipeline(
                    template=template,
                    template_key=template_key,
                    brief=effective_brief,
                    slug=slug,
                    artifact_dir=artifact_dir,
                    manifest=manifest,
                    extra={
                        **mission_setup_stage_hints(setup),
                        **repo_target_stage_hints(repo),
                        "execution_profile": execution_profile,
                        **(extra or {}),
                    },
                )
        except Exception as exc:
            if isinstance(
                exc,
                (StackShapeMismatchError, UnresolvedScaffoldStubError, MissingPlannedFilesError),
            ):
                logger.info("studio start exited intentionally for slug=%s: %s", slug, exc)
                existing = self.get_project(slug)
                if existing is not None:
                    return existing
            logger.exception("studio start failed for slug=%s", slug)
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}"
            self.mark_project_failed(
                slug,
                error,
                next_action="Project stopped before the swarm could finish starting.",
            )
            raise

    async def resume(self, slug: str, answers: List[str]) -> dict:
        """Resume a project that's awaiting_clarification by re-running the
        pipeline with the user's answers folded into the brief."""
        artifact_dir = self.projects_root / slug
        mf_path = artifact_dir / "project.json"
        if not mf_path.exists():
            raise FileNotFoundError(slug)
        manifest_data = json.loads(mf_path.read_text())
        if not isinstance(manifest_data, dict):
            raise ValueError(f"invalid manifest for {slug}")
        manifest: dict[str, Any] = self._normalize_manifest(manifest_data)
        if manifest.get("status") != "awaiting_clarification":
            # Already running or done — nothing to do
            return manifest
        try:
            # Fold answers into brief
            questions = (manifest.get("clarification") or {}).get("questions", [])
            qa_block = "\n\n## User clarifications\n"
            for q, a in zip(questions, answers):
                qa_block += f"- **Q:** {q}\n  **A:** {a}\n"
            new_brief = (manifest.get("brief") or "") + qa_block
            # Reset to running, clear stages so the pipeline reruns from scratch with
            # answers in the brief. (The clarifications field is preserved as history.)
            manifest["clarification_history"] = manifest.get("clarification_history", []) + [
                {"questions": questions, "answers": answers, "answered_at": time.time()}
            ]
            manifest["clarification"] = None
            manifest["status"] = "running"
            manifest["stages"] = []
            manifest["brief"] = new_brief
            manifest["current_stage"] = None
            manifest["current_agent"] = None
            self._clear_quality_summary(manifest)
            setup = normalize_mission_setup(manifest.get("mission_setup"))
            repo_source = manifest.get("repo_target")
            repo = self._resolved_repo_target(repo_source)
            manifest["repo_target"] = repo
            effective_brief = augment_brief_with_repo_target(
                augment_brief_with_mission_setup(new_brief, setup),
                repo,
            )
            execution_profile = str(
                manifest.get("execution_profile")
                or self._infer_execution_profile(
                    str(manifest.get("brief_raw") or manifest.get("brief") or new_brief),
                    None,
                )
            )
            manifest["execution_profile"] = execution_profile

            template = get_template(manifest["template"])
            if not template.stages and manifest["template"] == "auto":
                from skyn3t.adapters import LLMClient
                from skyn3t.studio.planner import plan_pipeline
                from skyn3t.studio.templates import StageSpec, Template
                llm_client = LLMClient(default_model=None, backend=None,
                                       event_bus=self.event_bus, caller_name="planner")
                planned = await plan_pipeline(brief=effective_brief, llm_client=llm_client)
                template = Template(key="auto", title="Auto-planned",
                                    description=f"Re-planned after clarification: {new_brief[:80]}",
                                    stages=[StageSpec(name=p.name, agent=p.agent,
                                                        capability=p.capability,
                                                        handoff_to=p.handoff_to,
                                                        input_extra={**p.input_extra,
                                                                     "expected_artifact": p.expected_artifact,
                                                                     "planned_rationale": p.rationale})
                                             for p in planned])
            manifest["workflow_summary"] = self._workflow_from_template(
                template, manifest["template"]
            )
            manifest["next_action"] = (
                f"Resuming with {len(answers)} clarification answer(s)."
            )
            self._append_history(
                manifest,
                "PROJECT_RESUMED",
                status="running",
                message=manifest["next_action"],
                answer_count=len(answers),
            )
            self._save_manifest(artifact_dir, manifest)
            self._publish("PROJECT_RESUMED", {"slug": slug, "answer_count": len(answers)})
            extra: Dict[str, Any] = {
                **mission_setup_stage_hints(setup),
                **repo_target_stage_hints(repo),
                "execution_profile": execution_profile,
            }
            extra.pop("require_clarification", None)
            extra["clarifications"] = True
            expected_outputs = (
                (manifest.get("workflow_summary") or {}).get("expected_outputs") or []
            )
            expected_files = [
                output
                for output in expected_outputs
                if isinstance(output, str) and "." in output and "/" not in output
            ]
            if expected_files:
                extra["expected_artifacts"] = expected_files
            return await self._run_pipeline(
                template=template, template_key=manifest["template"],
                brief=effective_brief, slug=slug,
                artifact_dir=artifact_dir, manifest=manifest, extra=extra,
            )
        except Exception as exc:
            logger.exception("studio resume failed for slug=%s", slug)
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}"
            self.mark_project_failed(
                slug,
                error,
                next_action="Project stopped while applying clarification answers.",
            )
            raise

    async def _run_pipeline(
        self,
        *,
        template: Any,
        template_key: str,
        brief: str,
        slug: str,
        artifact_dir: Path,
        manifest: Dict[str, Any],
        extra: Optional[dict],
    ) -> dict:
        """Execute the actual stage loop. Caller has already acquired the
        concurrency semaphore and set manifest["status"] = "running"."""
        # Defensive: never let a bad artifact_dir leak into agent input_data.
        # This guards against caller bugs, symlink attacks, and env misconfig.
        resolved_art = artifact_dir.resolve()
        resolved_root = self.projects_root.resolve()
        try:
            resolved_art.relative_to(resolved_root)
        except ValueError:
            raise RuntimeError(
                f"artifact_dir {resolved_art} is outside projects_root {resolved_root}. "
                f"Refusing to run pipeline to prevent writes outside the project sandbox."
            )
        # Extra guard: never run if artifact_dir IS the SkyN3t repo root.
        repo_marker = resolved_art / "skyn3t" / "core" / "agent.py"
        if repo_marker.exists():
            raise RuntimeError(
                f"artifact_dir {resolved_art} appears to be the SkyN3t repo root. "
                f"Refusing to run pipeline to prevent overwriting source files."
            )

        try:
            self._init_benchmark(manifest)
            # Auto-detect target_file from the brief (e.g. "target_file: skyn3t/web/dashboard.html").
            # Also infer from common phrasings so users don't have to type the keyword.
            auto_target = str((extra or {}).get("target_file") or "").strip() or None
            if not auto_target:
                m = re.search(r"target_file\s*[:=]\s*([^\s]+)", brief or "")
                if m:
                    auto_target = m.group(1).strip().rstrip(".,")
                else:
                    m2 = re.search(r"\b([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.\w+)\b", brief or "")
                    if m2:
                        auto_target = m2.group(1).strip().rstrip(".,")
            if auto_target:
                extra = dict(extra or {})
                extra.setdefault("target_file", auto_target)
                extra.setdefault("rationale", brief)

            # Inject contract verifier + consistency reviewer before the
            # main reviewer stage. Contract runs first (cheap deterministic
            # palette/tech_stack/placeholder checks); consistency reviewer
            # runs second (semantic LLM critique on the repaired scaffold);
            # main reviewer scores last. Each stage's fix loop repairs the
            # scaffold before the next one re-evaluates it.
            stages = list(template.stages)
            reviewer_idx = None
            for i, s in enumerate(stages):
                if s.agent == "ReviewerAgent" and s.name == "reviewer":
                    reviewer_idx = i
                    break
            if reviewer_idx is not None:
                from skyn3t.studio.templates import StageSpec
                consistency_stage = StageSpec(
                    name="consistency_reviewer",
                    agent="ConsistencyReviewerAgent",
                    capability="review",
                    handoff_to="reviewer",
                    input_extra={},
                )
                contract_stage = StageSpec(
                    name="contract_verifier",
                    agent="ContractVerifierAgent",
                    capability="review",
                    handoff_to="consistency_reviewer",
                    input_extra={},
                )
                stages.insert(reviewer_idx, consistency_stage)
                stages.insert(reviewer_idx, contract_stage)

            execution_profile = self._infer_execution_profile(
                str(manifest.get("brief_raw") or manifest.get("brief") or brief),
                extra,
            )
            manifest["execution_profile"] = execution_profile
            if isinstance(extra, dict):
                extra = {**extra, "execution_profile": execution_profile}

            for stage in stages:
                agent = get_agent(stage.agent, event_bus=self.event_bus, rag=self.rag)
                if hasattr(agent, "initialize"):
                    maybe = agent.initialize()
                    if hasattr(maybe, "__await__"):
                        await maybe

                input_data: Dict[str, Any] = {
                    "brief": brief,
                    "artifact_dir": str(artifact_dir),
                    "next_agent": stage.handoff_to,
                    **(extra or {}),
                    **stage.input_extra,
                }
                # Inject scoreboard-derived pre-warnings into the code
                # stage. If this (stack, planned-shape) has lost the
                # router mount in past runs, tell the CodeAgent to
                # double-check the mount lines before handoff.
                if stage.name == "code":
                    pre_warnings = self._scoreboard_prewarnings(brief)
                    if pre_warnings:
                        input_data["scoreboard_prewarnings"] = pre_warnings
                task = TaskRequest(
                    title=f"{template_key}:{stage.name}",
                    input_data=input_data,
                )
                # ``required_capability`` is not part of the base TaskRequest
                # dataclass, but downstream agents/orchestrators may consult it
                # for routing; attach it dynamically so the information is not
                # lost without breaking instantiation.
                if "required_capability" in TaskRequest.__dataclass_fields__:
                    setattr(
                        task,
                        "required_capability",
                        AgentCapability(
                            name=stage.capability,
                            description=f"Agent capability: {stage.capability}",
                        ),
                    )
                else:
                    task.input_data.setdefault(
                        "required_capability", stage.capability
                    )

                stage_output = self._default_stage_artifact(stage)
                stage_started_at = time.time()
                manifest["current_stage"] = stage.name
                manifest["current_agent"] = stage.agent
                manifest["next_action"] = (
                    f"{stage.agent} is working on {stage_output}."
                    if stage_output
                    else f"{stage.agent} is working on {stage.name}."
                )
                self._set_stage_record(
                    manifest,
                    stage,
                    status="running",
                    started_at=stage_started_at,
                    task_id=task.task_id,
                    summary=manifest["next_action"],
                    next_action=manifest["next_action"],
                )
                self._append_history(
                    manifest,
                    "PROJECT_STAGE_STARTED",
                    status="running",
                    stage=stage.name,
                    agent=stage.agent,
                    message=manifest["next_action"],
                )
                self._save_manifest(artifact_dir, manifest)
                self._publish(
                    "PROJECT_STAGE_STARTED",
                    {"slug": slug, "stage": stage.name, "agent": stage.agent},
                )
                try:
                    import asyncio as _asyncio
                    _stage_to: Optional[float] = (extra or {}).get("stage_timeout") if isinstance(extra, dict) else None
                    try:
                        _stage_to = float(_stage_to) if _stage_to else None
                    except Exception:
                        _stage_to = None
                    _stage_to = self._stage_timeout_for(stage.name, execution_profile, _stage_to)
                    with push_event_context(
                        project_slug=slug,
                        project_stage=stage.name,
                        project_template=template_key,
                        task_id=task.task_id,
                        correlation_id=task.task_id,
                    ):
                        result = await _asyncio.wait_for(
                            agent.execute(task), timeout=_stage_to
                        )
                except _asyncio.TimeoutError:
                    timeout_secs = int(_stage_to) if _stage_to is not None else 0
                    stage_error = f"stage timeout (>{timeout_secs}s)"
                    self._publish(
                        "PROJECT_STAGE_FAILED",
                        {
                            "slug": slug,
                            "stage": stage.name,
                            "agent": stage.agent,
                            "error": "stage timeout",
                        },
                    )
                    manifest["status"] = "failed"
                    manifest["next_action"] = (
                        f"{stage.agent} timed out during {stage.name}."
                    )
                    self._clear_quality_summary(manifest)
                    self._set_stage_record(
                        manifest,
                        stage,
                        status="failed",
                        started_at=stage_started_at,
                        completed_at=time.time(),
                        task_id=task.task_id,
                        summary=stage_error,
                        error=stage_error,
                        next_action=manifest["next_action"],
                    )
                    self._append_history(
                        manifest,
                        "PROJECT_STAGE_FAILED",
                        status="failed",
                        stage=stage.name,
                        agent=stage.agent,
                        message=f"Stage timeout (>{timeout_secs}s).",
                        error=stage_error,
                    )
                    self._save_manifest(artifact_dir, manifest)
                    # Record stage timeout as a 'no' verdict for the
                    # scoreboard so self-learning sees the failure. The
                    # scoreboard otherwise only learns from verifier
                    # outcomes — but stage timeouts that never reach the
                    # verifier ARE failures, and the meta-agent should
                    # see them. Whatever partial scaffold exists is the
                    # shape that failed.
                    if stage.name == "code":
                        try:
                            from skyn3t.intelligence.build_patterns import (
                                get_default_scoreboard,
                            )
                            scaffold_dir = artifact_dir / "scaffold"
                            stack = "unknown"
                            shape = []
                            if scaffold_dir.exists():
                                try:
                                    from skyn3t.agents.stack_templates import (
                                        detect_stack,
                                    )
                                    stack = detect_stack(brief) or "unknown"
                                except Exception:
                                    pass
                                shape = self._scaffold_shape(scaffold_dir)
                            sb = get_default_scoreboard()
                            sb.record(stack, shape, "no")
                            # Attribute the failure to the backend the
                            # code stage was running on so the adaptive
                            # router (resolve_model_for_file) can demote
                            # backends that consistently lose for this
                            # stack. resolve_model honors per-stage env
                            # overrides — same source of truth used at
                            # construction time.
                            try:
                                from skyn3t.core.model_router import resolve_model
                                stage_backend, _ = resolve_model("code")
                                if stage_backend and stack and shape:
                                    sb.record_backend(stack, shape, stage_backend, "no")
                            except Exception:
                                logger.debug(
                                    "scoreboard record_backend on timeout failed",
                                    exc_info=True,
                                )
                        except Exception:
                            logger.exception("scoreboard record on stage timeout failed")
                    break
                except Exception as e:  # noqa: BLE001 - surface failure into manifest
                    stage_error = str(e)
                    self._publish(
                        "PROJECT_STAGE_FAILED",
                        {
                            "slug": slug,
                            "stage": stage.name,
                            "agent": stage.agent,
                            "error": stage_error,
                        },
                    )
                    manifest["status"] = "failed"
                    manifest["next_action"] = (
                        f"{stage.agent} could not finish {stage.name}."
                    )
                    self._clear_quality_summary(manifest)
                    self._set_stage_record(
                        manifest,
                        stage,
                        status="failed",
                        started_at=stage_started_at,
                        completed_at=time.time(),
                        task_id=task.task_id,
                        summary=stage_error,
                        error=stage_error,
                        next_action=manifest["next_action"],
                    )
                    self._append_history(
                        manifest,
                        "PROJECT_STAGE_FAILED",
                        status="failed",
                        stage=stage.name,
                        agent=stage.agent,
                        message=stage_error,
                        error=stage_error,
                    )
                    self._save_manifest(artifact_dir, manifest)
                    # Same scoreboard hook as the timeout path — record
                    # a code-stage exception as 'no' verdict so the
                    # learner sees it.
                    if stage.name == "code":
                        try:
                            from skyn3t.intelligence.build_patterns import (
                                get_default_scoreboard,
                            )
                            scaffold_dir = artifact_dir / "scaffold"
                            stack = "unknown"
                            shape = []
                            if scaffold_dir.exists():
                                try:
                                    from skyn3t.agents.stack_templates import (
                                        detect_stack,
                                    )
                                    stack = detect_stack(brief) or "unknown"
                                except Exception:
                                    pass
                                shape = self._scaffold_shape(scaffold_dir)
                            sb = get_default_scoreboard()
                            sb.record(stack, shape, "no")
                            try:
                                from skyn3t.core.model_router import resolve_model
                                stage_backend, _ = resolve_model("code")
                                if stage_backend and stack and shape:
                                    sb.record_backend(stack, shape, stage_backend, "no")
                            except Exception:
                                logger.debug(
                                    "scoreboard record_backend on error failed",
                                    exc_info=True,
                                )
                        except Exception:
                            logger.exception("scoreboard record on stage error failed")
                    break

                ok = bool(getattr(result, "success", True))
                output = getattr(result, "output", None) or {}
                stage_files = self._normalize_stage_files(
                    output.get("files") if isinstance(output, dict) else None,
                    artifact_dir=artifact_dir,
                )
                stage_summary = self._summarize_stage_output(output)

                # Conversational pause: if any stage returns needs_clarification=True, halt and
                # persist a special manifest status so the dashboard can prompt the user.
                if isinstance(output, dict) and output.get("needs_clarification"):
                    manifest["status"] = "awaiting_clarification"
                    manifest["clarification"] = {
                        "asked_by": stage.agent,
                        "questions": output.get("questions", []),
                        "asked_at": time.time(),
                    }
                    question_count = len(output.get("questions", []))
                    manifest["next_action"] = (
                        f"Answer {question_count} clarification question(s) to continue."
                    )
                    self._set_stage_record(
                        manifest,
                        stage,
                        status="waiting",
                        started_at=stage_started_at,
                        completed_at=time.time(),
                        task_id=task.task_id,
                        summary=stage_summary or manifest["next_action"],
                        files=stage_files,
                        next_action=manifest["next_action"],
                        question_count=question_count,
                    )
                    self._append_history(
                        manifest,
                        "PROJECT_AWAITING_CLARIFICATION",
                        status="awaiting_clarification",
                        stage=stage.name,
                        agent=stage.agent,
                        message=manifest["next_action"],
                        question_count=question_count,
                    )
                    self._finalize_benchmark(manifest)
                    self._save_manifest(artifact_dir, manifest)
                    self._publish("PROJECT_AWAITING_CLARIFICATION",
                                   {"slug": slug, "stage": stage.name,
                                    "questions": output.get("questions", [])})
                    # IMPORTANT: do NOT continue to next stage. Return now; user will resume.
                    return manifest

                for f in stage_files:
                    if f not in manifest["artifacts"]:
                        manifest["artifacts"].append(f)
                if not ok:
                    # A stage returned success=False; treat the whole project
                    # as failed so the dashboard badge reflects reality.
                    manifest["status"] = "failed"
                    stage_error = (
                        str(getattr(result, "error", "") or "")
                        or self._summarize_stage_output(output)
                        or "stage returned success=false"
                    )
                    manifest["next_action"] = (
                        f"{stage.agent} could not finish {stage.name}."
                    )
                    self._clear_quality_summary(manifest)
                    self._set_stage_record(
                        manifest,
                        stage,
                        status="failed",
                        started_at=stage_started_at,
                        completed_at=time.time(),
                        task_id=task.task_id,
                        summary=stage_error,
                        files=stage_files,
                        error=stage_error,
                        next_action=manifest["next_action"],
                    )
                    self._append_history(
                        manifest,
                        "PROJECT_STAGE_FAILED",
                        status="failed",
                        stage=stage.name,
                        agent=stage.agent,
                        message=stage_error,
                        error=stage_error,
                    )
                    self._save_manifest(artifact_dir, manifest)
                    self._publish(
                        "PROJECT_STAGE_FAILED",
                        {
                            "slug": slug,
                            "stage": stage.name,
                            "agent": stage.agent,
                            "error": stage_error,
                        },
                    )
                    break

                # Contract verifier fix loop: deterministic
                # palette/tech_stack/placeholder findings → targeted
                # regen of the offending files. Runs before the
                # consistency reviewer so the LLM critique sees the
                # repaired scaffold.
                if stage.name == "contract_verifier":
                    contract_output = output if isinstance(output, dict) else {}
                    # Phase-2 resolution: if a previous pass applied a
                    # contract fix and stashed the signature on the
                    # manifest, the outcome of THIS pass tells us
                    # whether the fix worked. Verdict != "needs_fix"
                    # (or blocker count 0) → mark the index row True.
                    # Re-flagged blockers → mark False, then keep going
                    # to the fix-loop below.
                    pending = manifest.pop("_pending_fix", None) if isinstance(manifest, dict) else None
                    if pending and pending.get("stage") == stage.name:
                        prior_sig = pending.get("error_signature") or ""
                        worked = (
                            contract_output.get("verdict") != "needs_fix"
                            or int(contract_output.get("blocker_count", 0)) == 0
                        )
                        if prior_sig:
                            try:
                                from skyn3t.memory.store import MemoryStore
                                store = MemoryStore()
                                eid = await store.mark_latest_unresolved_fix_worked(
                                    prior_sig, worked,
                                )
                                if eid:
                                    logger.info(
                                        "contract_verifier: resolved fix %s for %s → worked=%s",
                                        pending.get("fix_applied"), prior_sig, worked,
                                    )
                            except Exception:
                                logger.debug(
                                    "mark_latest_unresolved_fix_worked failed",
                                    exc_info=True,
                                )
                    if contract_output.get("verdict") == "needs_fix":
                        blockers = contract_output.get("blocker_count", 0)
                        if blockers > 0:
                            self._append_history(
                                manifest,
                                "CONTRACT_VERIFIER_BLOCKERS",
                                status="running",
                                message=f"{blockers} blocker(s) found by contract verifier.",
                            )
                            try:
                                import json as _json

                                from skyn3t.adapters import LLMClient
                                from skyn3t.agents.targeted_fix import (
                                    FileIssue,
                                    apply_targeted_fix,
                                )
                                report_json = contract_output.get("report_json", "{}")
                                report = _json.loads(report_json)
                                findings = report.get("findings", [])
                                # Publish structured event so ExperienceIngestor
                                # can write a concrete failure lesson into
                                # the vector store. The ingestor filters on
                                # payload["kind"] (see _on_system_alert).
                                # This is what makes the next canary smarter.
                                try:
                                    # Derive a stable signature so the
                                    # experience index can rank fixes
                                    # by this exact failure class later.
                                    from skyn3t.intelligence.error_signatures import (
                                        signature_for_findings,
                                    )
                                    contract_signature = signature_for_findings(
                                        findings, source="contract",
                                    )
                                    self._publish(
                                        "CONTRACT_VERIFIER_BLOCKERS",
                                        {
                                            "slug": slug,
                                            "project_slug": slug,
                                            "stage": stage.name,
                                            "findings": [
                                                f for f in findings
                                                if isinstance(f, dict)
                                                and f.get("severity") == "blocker"
                                            ][:8],
                                            "stack": manifest.get("stack") or "",
                                            "verdict": "needs_fix",
                                            "error_signature": contract_signature,
                                        },
                                    )
                                except Exception:
                                    logger.debug("contract blocker publish failed", exc_info=True)
                                scaffold_dir = artifact_dir / "scaffold"

                                # Group findings by target file. The
                                # mapping per category mirrors the plan.
                                merged: Dict[str, Dict[str, Any]] = {}
                                for f in findings:
                                    if f.get("severity") != "blocker":
                                        continue
                                    category = str(f.get("category") or "")
                                    raw_file = str(f.get("file") or "")
                                    fix_hint = f.get("fix_hint") or {}
                                    base_msg = str(f.get("message") or "")

                                    if category == "palette_schism_css":
                                        target = raw_file
                                        palette = fix_hint.get("canonical_palette") or []
                                        offending = fix_hint.get("offending_hexes") or []
                                        err = (
                                            base_msg
                                            + "\nRewrite this file using ONLY the canonical palette: "
                                            + ", ".join(palette)
                                            + ".\nDo NOT introduce any other hex literals. "
                                            "Preserve all selectors, layout rules, and class names. "
                                            "Detected unauthorized hex(es): "
                                            + ", ".join(offending)
                                            + "."
                                        )
                                    elif category == "tech_stack_mismatch":
                                        target = raw_file
                                        expected = fix_hint.get("expected_packages") or []
                                        role = fix_hint.get("role") or ""
                                        declared = fix_hint.get("declared") or ""
                                        err = (
                                            base_msg
                                            + f"\nAdd one of {expected} to {target} so the manifest "
                                            "matches the declared tech stack. "
                                            "Preserve all existing scripts and the name/version/type fields. "
                                            f"(role={role}, declared={declared})"
                                        )
                                    elif category == "architecture_drift":
                                        target = raw_file
                                        expected = fix_hint.get("expected_packages") or []
                                        keyword = fix_hint.get("keyword") or ""
                                        err = (
                                            base_msg
                                            + f"\narchitecture.md mentions {keyword!r}. "
                                            f"Add one of {expected} to {target} (use a sane semver) "
                                            "so the scaffold matches the architecture doc. "
                                            "Preserve all existing scripts and the name/version/type fields."
                                        )
                                    elif category == "language_mismatch":
                                        # Two subtypes: polluted package.json (remove
                                        # Python libs) vs tech_stack-says-Python-
                                        # scaffold-is-Node (revise tech_stack).
                                        polluted = fix_hint.get("polluted_packages") or []
                                        if polluted:
                                            target = fix_hint.get("package_json") or raw_file
                                            err = (
                                                base_msg
                                                + f"\nRemove these Python libs that are NOT valid "
                                                f"npm packages: {', '.join(polluted)}. "
                                                "Keep all other dependencies, scripts, and the "
                                                "name/version/type fields exactly as-is. Return "
                                                "the corrected package.json."
                                            )
                                        else:
                                            # tech_stack lies — regenerate to match actual Node scaffold
                                            target = "tech_stack.json"
                                            err = (
                                                base_msg
                                                + "\nThe scaffold is actually a Node project "
                                                "(package.json present, no pyproject.toml). "
                                                "Rewrite tech_stack.json with Node-compatible "
                                                "values: backend should be 'express' or 'hono-node', "
                                                "frontend should be 'react-vite' or similar, db "
                                                "should be 'better-sqlite3' or 'postgres' (the npm "
                                                "package). Do not return Python framework names."
                                            )
                                    elif category == "placeholder_leak":
                                        target = raw_file
                                        literal = fix_hint.get("literal") or ""
                                        err = (
                                            base_msg
                                            + f"\nRemove the literal {literal!r} from this file. "
                                            "Replace with concrete content consistent with the brief: "
                                            + (brief[:400] if brief else "")
                                            + ". Preserve overall structure."
                                        )
                                    elif category == "missing_feature_evidence":
                                        target = fix_hint.get("fix_target") or raw_file
                                        instruction = fix_hint.get("fix_instruction") or ""
                                        keyword = fix_hint.get("keyword") or ""
                                        err = (
                                            base_msg
                                            + f"\nBrief requires {keyword!r}. {instruction}"
                                            + "\nDo not remove or rename existing exports/selectors."
                                        )
                                    elif category == "cli_prose_leak":
                                        target = raw_file
                                        matched = fix_hint.get("matched") or ""
                                        err = (
                                            base_msg
                                            + f"\nThe file begins with LLM tool-call narration "
                                            f"(matched: {matched!r}). Rewrite the file from scratch "
                                            "so it contains ONLY the real file content. "
                                            "Brief context: "
                                            + (brief[:400] if brief else "")
                                            + ". No planning chatter, no '● Search', no 'I'm checking'."
                                        )
                                    else:
                                        # Categories not eligible for targeted-fix
                                        # (e.g. palette_schism_palette which lives
                                        # outside the scaffold).
                                        continue

                                    normalized = self._normalize_scaffold_issue_path(
                                        scaffold_dir, target
                                    )
                                    if not normalized:
                                        logger.warning(
                                            "Skipping unresolved contract-verifier file entry: %s",
                                            target,
                                        )
                                        continue
                                    bucket = merged.setdefault(
                                        normalized,
                                        {"messages": [], "action": "regenerate"},
                                    )
                                    bucket["messages"].append(err)

                                if merged:
                                    issues = [
                                        FileIssue(
                                            path=path,
                                            error_message="\n\n".join(bucket["messages"])[:2048],
                                            suggested_action=bucket["action"],
                                        )
                                        for path, bucket in merged.items()
                                    ]
                                    client = LLMClient(
                                        event_bus=self.event_bus, caller_name="contract_fix"
                                    )
                                    fix_result = await apply_targeted_fix(
                                        scaffold_dir=scaffold_dir,
                                        issues=issues,
                                        llm_client=client,
                                        brief=brief,
                                    )
                                    # Stash the active signature + fix
                                    # label on the manifest so the next
                                    # verifier pass can resolve the
                                    # experience_index row's fix_worked
                                    # state (mark_latest_unresolved_*).
                                    if contract_signature and fix_result.fix_label:
                                        manifest["_pending_fix"] = {
                                            "error_signature": contract_signature,
                                            "fix_applied": fix_result.fix_label,
                                            "stage": stage.name,
                                        }
                                    self._append_history(
                                        manifest,
                                        "CONTRACT_FIX_APPLIED",
                                        status="running",
                                        message=(
                                            f"Fixed {len(fix_result.files_changed)} file(s), "
                                            f"created {len(fix_result.files_created)} placeholder(s)."
                                        ),
                                    )
                            except Exception:
                                logger.exception("contract verifier fix loop failed")

                # Consistency reviewer fix loop: if blockers found,
                # apply targeted fixes before the main reviewer runs.
                if stage.name == "consistency_reviewer":
                    review_output = output if isinstance(output, dict) else {}
                    # Phase-2 resolution: mirror of the contract_verifier
                    # path. If the previous pass applied a consistency
                    # fix, this pass's verdict tells us whether it worked.
                    pending = (
                        manifest.pop("_pending_fix", None)
                        if isinstance(manifest, dict) else None
                    )
                    if pending and pending.get("stage") == stage.name:
                        prior_sig = pending.get("error_signature") or ""
                        worked = (
                            review_output.get("verdict") != "needs_fix"
                            or int(review_output.get("blocker_count", 0)) == 0
                        )
                        if prior_sig:
                            try:
                                from skyn3t.memory.store import MemoryStore
                                eid = await MemoryStore().mark_latest_unresolved_fix_worked(
                                    prior_sig, worked,
                                )
                                if eid:
                                    logger.info(
                                        "consistency_reviewer: resolved fix %s for %s → worked=%s",
                                        pending.get("fix_applied"), prior_sig, worked,
                                    )
                            except Exception:
                                logger.debug(
                                    "mark_latest_unresolved_fix_worked failed (consistency)",
                                    exc_info=True,
                                )
                    if review_output.get("verdict") == "needs_fix":
                        blockers = review_output.get("blocker_count", 0)
                        if blockers > 0:
                            self._append_history(
                                manifest,
                                "CONSISTENCY_REVIEW_BLOCKERS",
                                status="running",
                                message=f"{blockers} blocker(s) found by consistency reviewer.",
                            )
                            try:
                                from skyn3t.adapters import LLMClient
                                from skyn3t.agents.targeted_fix import (
                                    FileIssue,
                                    apply_targeted_fix,
                                )

                                report_json = review_output.get("report_json", "[]")
                                import json as _json
                                report = _json.loads(report_json)
                                findings = report.get("findings", [])
                                # Derive a consistency-scoped signature.
                                # Source = "consistency" so it doesn't
                                # collide with contract: signatures even
                                # when the same category name appears.
                                try:
                                    from skyn3t.intelligence.error_signatures import (
                                        signature_for_findings,
                                    )
                                    consistency_signature = signature_for_findings(
                                        findings, source="consistency",
                                    )
                                except Exception:
                                    consistency_signature = None
                                # Publish to SYSTEM_ALERT so the ingestor
                                # picks it up via _on_system_alert →
                                # ingest_project_event → experience_index
                                # row. The runner previously only added
                                # this as a local history entry, so the
                                # cortex never saw consistency blockers.
                                try:
                                    self._publish(
                                        "CONSISTENCY_REVIEW_BLOCKERS",
                                        {
                                            "slug": slug,
                                            "project_slug": slug,
                                            "stage": stage.name,
                                            "findings": [
                                                f for f in findings
                                                if isinstance(f, dict)
                                                and f.get("severity") == "blocker"
                                            ][:8],
                                            "stack": manifest.get("stack") or "",
                                            "verdict": "needs_fix",
                                            "error_signature": consistency_signature,
                                        },
                                    )
                                except Exception:
                                    logger.debug(
                                        "publish CONSISTENCY_REVIEW_BLOCKERS failed",
                                        exc_info=True,
                                    )
                                review_issues = []
                                for f in findings:
                                    if f.get("severity") != "blocker":
                                        continue
                                    target_fp = self._consistency_fix_target(
                                        scaffold_dir=scaffold_dir,
                                        issue_file=f.get("file", "(unknown)"),
                                        category=str(f.get("category") or ""),
                                    )
                                    normalized_fp = self._normalize_scaffold_issue_path(
                                        scaffold_dir,
                                        target_fp,
                                    )
                                    if not normalized_fp:
                                        logger.warning(
                                            "Skipping unresolved consistency-reviewer file entry: %s",
                                            f.get("file", "(unknown)"),
                                        )
                                        continue
                                    review_issues.append(
                                        FileIssue(
                                            path=normalized_fp,
                                            error_message=f.get("message", ""),
                                            suggested_action="regenerate",
                                        )
                                    )
                                issues = review_issues
                                if issues:
                                    scaffold_dir = artifact_dir / "scaffold"
                                    client = LLMClient(
                                        event_bus=self.event_bus, caller_name="consistency_fix"
                                    )
                                    fix_result = await apply_targeted_fix(
                                        scaffold_dir=scaffold_dir,
                                        issues=issues,
                                        llm_client=client,
                                        brief=brief,
                                    )
                                    if consistency_signature and fix_result.fix_label:
                                        manifest["_pending_fix"] = {
                                            "error_signature": consistency_signature,
                                            "fix_applied": fix_result.fix_label,
                                            "stage": stage.name,
                                        }
                                    self._append_history(
                                        manifest,
                                        "CONSISTENCY_FIX_APPLIED",
                                        status="running",
                                        message=(
                                            f"Fixed {len(fix_result.files_changed)} file(s), "
                                            f"created {len(fix_result.files_created)} placeholder(s)."
                                        ),
                                    )
                            except Exception:
                                logger.exception("consistency reviewer fix loop failed")

                # Inter-agent conversation: cross-model critique +
                # one bounded revision pass. Returns the original
                # result if the critic says NO_ISSUES, the revision
                # fails, or the stage is in the skip list.
                critique_timeout_seconds = self._critique_timeout_for(
                    stage_name=stage.name,
                    execution_profile=execution_profile,
                )
                try:
                    revised_result = await asyncio.wait_for(
                        self._critique_and_revise(
                            stage=stage,
                            agent=agent,
                            result=result,
                            artifact_dir=artifact_dir,
                            brief=brief,
                            task=task,
                            manifest=manifest,
                        ),
                        timeout=critique_timeout_seconds,
                    )
                    if revised_result is not None and revised_result is not result:
                        result = revised_result
                        output = getattr(result, "output", None) or {}
                        stage_files = self._normalize_stage_files(
                            output.get("files") if isinstance(output, dict) else None,
                            artifact_dir=artifact_dir,
                        )
                        stage_summary = self._summarize_stage_output(output)
                        self._publish(
                            "PROJECT_STAGE_REVISED",
                            {
                                "slug": slug,
                                "stage": stage.name,
                                "agent": stage.agent,
                            },
                        )
                except asyncio.TimeoutError:
                    logger.warning(
                        "critique window timed out for %s after %.1fs; continuing with original result",
                        stage.name,
                        critique_timeout_seconds,
                    )
                    self._append_history(
                        manifest,
                        "CRITIQUE_FAILED",
                        status="running",
                        stage=stage.name,
                        message=(
                            f"Critique window timed out after "
                            f"{critique_timeout_seconds:.0f}s."
                        ),
                    )
                except Exception:
                    logger.exception("critique pass crashed; continuing with original result")

                # ── Post-code static consistency check ─────────────────────
                # Run the pure-Python consistency engine after the code stage
                # to catch import graph errors, missing deps, and service
                # hallucinations before the expensive build verifier runs.
                if stage.name == "code" and manifest.get("status") != "failed":
                    await self._run_post_code_checks(
                        manifest=manifest,
                        artifact_dir=artifact_dir,
                        brief=brief,
                        stage_name=stage.name,
                        stage_output=output if isinstance(output, dict) else None,
                    )

                quality_candidate = self._extract_quality_candidate(
                    stage=stage,
                    output=output,
                    artifact_dir=artifact_dir,
                )
                if quality_candidate is not None:
                    manifest["quality_summary"] = self._merge_quality_summary(
                        manifest.get("quality_summary"),
                        quality_candidate,
                    )
                manifest["next_action"] = (
                    f"Handing off to {stage.handoff_to}."
                    if stage.handoff_to
                    else "Wrapping up the project."
                )
                self._set_stage_record(
                    manifest,
                    stage,
                    status="done",
                    started_at=stage_started_at,
                    completed_at=time.time(),
                    task_id=task.task_id,
                    summary=stage_summary or manifest["next_action"],
                    files=stage_files,
                    next_action=manifest["next_action"],
                )
                manifest["current_stage"] = None
                manifest["current_agent"] = None
                self._append_history(
                    manifest,
                    "PROJECT_STAGE_COMPLETED",
                    status="running",
                    stage=stage.name,
                    agent=stage.agent,
                    message=stage_summary or manifest["next_action"],
                )
                self._save_manifest(artifact_dir, manifest)
                self._publish(
                    "PROJECT_STAGE_COMPLETED",
                    {
                        "slug": slug,
                        "stage": stage.name,
                        "agent": stage.agent,
                        "summary": stage_summary,
                    },
                )

            # If no stage failed, derive the final project outcome from the
            # strongest available quality signal instead of always claiming a
            # clean "done" result.
            if manifest.get("status") != "failed":
                manifest["status"], manifest["next_action"], manifest["error"] = (
                    self._finalize_project_outcome(
                        manifest.get("quality_summary"),
                        manifest=manifest,
                    )
                )
            else:
                self._clear_quality_summary(manifest)
                manifest["next_action"] = (
                    manifest.get("next_action")
                    or "Project stopped because a stage failed."
                )
            manifest["current_stage"] = None
            manifest["current_agent"] = None
            manifest["completed_at"] = time.time()
            manifest["artifacts"] = self._scan_artifacts(artifact_dir)
            scaffold_dir = artifact_dir / "scaffold"
            quality_summary = self._normalize_quality_summary(manifest.get("quality_summary"))
            reviewer_failed = (
                quality_summary is not None
                and quality_summary.get("source") == "reviewer"
                and manifest.get("status") == "failed"
            )
            # "Docs-only" failure detection: if the user's brief implied code
            # work but every artifact produced was markdown/text, the project
            # ran the wrong shape. Mark it failed so the auto-retry hook
            # spawns a new attempt with a forced code stage. Skip this check
            # when the brief was clearly docs-oriented (write/draft/produce
            # docs-noun) — those projects are expected to produce only docs.
            if manifest["status"] in {"done", "needs_fixes"} and not scaffold_dir.exists():
                if self._is_docs_only_for_code_brief(brief, manifest["artifacts"]):
                    manifest["status"] = "failed"
                    manifest["error"] = (
                        "Brief implied software work but every artifact is a "
                        "doc/markdown file. Auto-retry will force a code stage."
                    )
                    manifest["next_action"] = (
                        "Retrying with a code-producing planner."
                    )
                else:
                    pass
            # If a scaffold dir was produced, run the verifiers even when the
            # reviewer marked the run no-go. Reviewer no-go used to short-
            # circuit the mechanical verifiers, which hid the concrete build /
            # boot / integration failure that the retry system actually needs.
            should_run_verifiers = (
                scaffold_dir.exists()
                and scaffold_dir.is_dir()
                and (
                    manifest["status"] in {"done", "needs_fixes"}
                    or reviewer_failed
                )
            )
            if should_run_verifiers:
                skip_fix_loops = reviewer_failed
                failed_verifier = "build"
                try:
                    build_result = await self._run_build_verifier(
                        str(scaffold_dir), brief,
                    )
                except Exception:
                    logger.exception("build_verifier invocation failed")
                    build_result = None
                if build_result is not None:
                    manifest["build_verification"] = build_result
                    verdict = build_result.get("verdict")
                    if not skip_fix_loops:
                        FIX_ATTEMPTS = 2
                        attempt = 0
                        while verdict == "no" and attempt < FIX_ATTEMPTS:
                            attempt += 1
                            try:
                                fixed = await self._apply_build_fix_round(
                                    scaffold_dir, brief, build_result, attempt,
                                )
                            except Exception:
                                logger.exception("fix round failed")
                                fixed = False
                            if not fixed:
                                break
                            try:
                                build_result = await self._run_build_verifier(
                                    str(scaffold_dir), brief,
                                ) or build_result
                            except Exception:
                                logger.exception("re-verify after fix failed")
                                break
                            manifest["build_verification"] = build_result
                            verdict = build_result.get("verdict")
                            manifest.setdefault("build_fix_attempts", []).append({
                                "attempt": attempt,
                                "verdict": verdict,
                                "command": build_result.get("command"),
                            })
                            if verdict == "yes":
                                self._append_history(
                                    manifest,
                                    "BUILD_FIX_SUCCEEDED",
                                    status="running",
                                    message=f"In-place fix round {attempt} cleared the build.",
                                )
                                try:
                                    self._persist_fix_as_skill(
                                        stack=(build_result or {}).get("stack") or "unknown",
                                        fix_round=attempt,
                                        prior_summary=manifest.get("build_verification", {}).get("summary"),
                                    )
                                except Exception:
                                    logger.exception("persist fix-as-skill failed")
                                break
                    if verdict in ("yes", "skipped"):
                        try:
                            boot_result = await self._run_boot_verifier(
                                str(scaffold_dir), brief,
                            )
                        except Exception:
                            logger.exception("boot_verifier invocation failed")
                            boot_result = None
                        if boot_result is not None:
                            manifest["boot_verification"] = boot_result
                            boot_verdict = boot_result.get("verdict")
                            if not skip_fix_loops:
                                BOOT_FIX_ATTEMPTS = 2
                                boot_attempt = 0
                                while boot_verdict == "no" and boot_attempt < BOOT_FIX_ATTEMPTS:
                                    boot_attempt += 1
                                    try:
                                        fixed = await self._apply_build_fix_round(
                                            scaffold_dir, brief, boot_result, boot_attempt,
                                        )
                                    except Exception:
                                        logger.exception("boot fix round failed")
                                        fixed = False
                                    if not fixed:
                                        break
                                    try:
                                        boot_result = await self._run_boot_verifier(
                                            str(scaffold_dir), brief,
                                        ) or boot_result
                                    except Exception:
                                        logger.exception("re-boot after fix failed")
                                        break
                                    manifest["boot_verification"] = boot_result
                                    boot_verdict = boot_result.get("verdict")
                                    manifest.setdefault("boot_fix_attempts", []).append({
                                        "attempt": boot_attempt,
                                        "verdict": boot_verdict,
                                        "command": boot_result.get("command"),
                                    })
                                    if boot_verdict == "yes":
                                        self._append_history(
                                            manifest,
                                            "BOOT_FIX_SUCCEEDED",
                                            status="running",
                                            message=(
                                                f"In-place fix round {boot_attempt} "
                                                f"got the server booting."
                                            ),
                                        )
                                        try:
                                            self._persist_fix_as_skill(
                                                stack=(boot_result or {}).get("kind") or "unknown",
                                                fix_round=boot_attempt,
                                                prior_summary=(
                                                    manifest.get("boot_verification", {}).get("summary")
                                                ),
                                            )
                                        except Exception:
                                            logger.exception("persist boot-fix-as-skill failed")
                                        break
                                if boot_verdict == "no":
                                    verdict = "no"
                                    build_result = boot_result
                                    failed_verifier = "boot"
                            if boot_verdict == "yes":
                                try:
                                    integration_result = await self._run_integration_verifier(
                                        str(scaffold_dir), brief,
                                    )
                                except Exception:
                                    logger.exception("integration_verifier invocation failed")
                                    integration_result = None
                                if integration_result is not None:
                                    manifest["integration_verification"] = integration_result
                                    integration_verdict = integration_result.get("verdict")
                                    if not skip_fix_loops:
                                        INTEGRATION_FIX_ATTEMPTS = 2
                                        integration_attempt = 0
                                        while (
                                            integration_verdict == "no"
                                            and integration_attempt < INTEGRATION_FIX_ATTEMPTS
                                        ):
                                            integration_attempt += 1
                                            try:
                                                fixed = await self._apply_integration_fix_round(
                                                    scaffold_dir=scaffold_dir,
                                                    brief=brief,
                                                    integration_result=integration_result,
                                                    attempt=integration_attempt,
                                                )
                                            except Exception:
                                                logger.exception("integration fix round failed")
                                                fixed = False
                                            if not fixed:
                                                break
                                            try:
                                                await self._kill_stray_server_processes(
                                                    str(scaffold_dir)
                                                )
                                                await asyncio.sleep(0.5)
                                            except Exception:
                                                logger.debug("stray process cleanup failed (non-fatal)")
                                            try:
                                                boot_result = await self._run_boot_verifier(
                                                    str(scaffold_dir), brief,
                                                ) or boot_result
                                            except Exception:
                                                logger.exception("re-boot after integration fix failed")
                                                break
                                            manifest["boot_verification"] = boot_result
                                            boot_verdict = (boot_result or {}).get("verdict")
                                            if boot_verdict != "yes":
                                                verdict = "no"
                                                build_result = boot_result
                                                failed_verifier = "boot"
                                                break
                                            try:
                                                integration_result = await self._run_integration_verifier(
                                                    str(scaffold_dir), brief,
                                                ) or integration_result
                                            except Exception:
                                                logger.exception("integration re-check failed")
                                                break
                                            manifest["integration_verification"] = integration_result
                                            integration_verdict = integration_result.get("verdict")
                                            manifest.setdefault("integration_fix_attempts", []).append({
                                                "attempt": integration_attempt,
                                                "verdict": integration_verdict,
                                            })
                                            if integration_verdict == "yes":
                                                self._append_history(
                                                    manifest,
                                                    "INTEGRATION_FIX_SUCCEEDED",
                                                    status="running",
                                                    message=(
                                                        f"In-place integration fix round "
                                                        f"{integration_attempt} cleared "
                                                        "the integration contract gate."
                                                    ),
                                                )
                                                break
                                        if integration_verdict == "no":
                                            verdict = "no"
                                            build_result = integration_result
                                            failed_verifier = "integration"

                    if not skip_fix_loops and verdict == "no":
                        manifest["status"] = "failed"
                        failure_label = {
                            "build": "build failure",
                            "boot": "boot failure",
                            "integration": "integration failure",
                        }.get(failed_verifier, "build failure")
                        default_error = {
                            "build": "Build verifier rejected the scaffold.",
                            "boot": "Server failed to boot during verification.",
                            "integration": "Integration contract verification failed.",
                        }.get(failed_verifier, "Build verifier rejected the scaffold.")
                        manifest["error"] = (
                            build_result.get("summary")
                            or default_error
                        )
                        manifest["next_action"] = (
                            f"Retrying with the {failure_label} as a hint."
                        )
                        manifest["_retry_hint"] = (
                            build_result.get("failure_hint") or ""
                        )
                    try:
                        from skyn3t.intelligence.build_patterns import (
                            get_default_scoreboard,
                        )
                        stack = str((build_result or {}).get("stack") or "unknown")
                        shape = self._scaffold_shape(scaffold_dir)
                        sb = get_default_scoreboard()
                        sb.record(stack, shape, str(verdict or "no"))
                        # Attribute the verifier verdict to the backend
                        # that ran the code stage — the same source of
                        # truth (resolve_model) the agent itself used.
                        # Lets the adaptive router learn from real
                        # verifier outcomes, not just stage exceptions.
                        try:
                            from skyn3t.core.model_router import resolve_model
                            stage_backend, _ = resolve_model("code")
                            if stage_backend and stack and shape:
                                sb.record_backend(
                                    stack, shape, stage_backend, str(verdict or "no"),
                                )
                        except Exception:
                            logger.debug(
                                "scoreboard record_backend on verifier verdict failed",
                                exc_info=True,
                            )
                        sb.flush()
                    except Exception:
                        logger.exception("build_pattern record failed")
            completion_message = (
                manifest["next_action"]
                if manifest.get("status") in {"done", "needs_fixes"}
                else manifest.get("error") or manifest["next_action"]
            )
            self._finalize_benchmark(manifest)
            self._append_history(
                manifest,
                "PROJECT_COMPLETED",
                status=manifest["status"],
                message=completion_message,
            )
            self._save_manifest(artifact_dir, manifest)
            # Build payload enrichments for the experience ingestor:
            # extract feature tags from the brief (glassmorphism, dark
            # mode, etc.) so the next canary's RAG query on the same
            # features retrieves this run's lessons.
            _feature_tags: List[str] = []
            try:
                from skyn3t.agents.brief_requirements import extract_requirements
                _reqs = extract_requirements(brief or "")
                # Flatten rule labels to terse tags (the first word of each rule).
                seen_tags: set = set()
                for rule_list in _reqs.rules_by_ext.values():
                    for rule in rule_list:
                        first_word = rule.split(":", 1)[0].strip().lower()
                        if first_word and first_word not in seen_tags:
                            seen_tags.add(first_word)
                            _feature_tags.append(first_word)
            except Exception:
                logger.debug("feature_tag extraction for completion failed", exc_info=True)

            self._publish(
                "PROJECT_COMPLETED",
                {
                    "slug": slug,
                    "template": template_key,
                    "title": template.title,
                    "stages_completed": sum(
                        1
                        for stage_record in manifest.get("stages", [])
                        if isinstance(stage_record, dict)
                        and self._normalize_stage_status(
                            stage_record.get("status"), stage_record.get("ok")
                        )
                        == "done"
                    ),
                    "status": manifest["status"],
                    "verdict": (manifest.get("quality_summary") or {}).get("verdict") or "",
                    "stack": manifest.get("stack") or "",
                    "feature_tags": _feature_tags,
                    "message": completion_message,
                },
            )
            # Retry-success skill capture: when a `-retry` project finishes
            # well (status done / needs_fixes), the original failure that
            # triggered the retry is now a solved problem. Persist the
            # lesson so the next similar brief gets it injected at code-gen
            # time instead of repeating the same break → retry cycle.
            if (
                slug.endswith("-retry")
                and manifest.get("status") in {"done", "needs_fixes"}
            ):
                try:
                    self._persist_retry_as_skill(retry_slug=slug, retry_manifest=manifest)
                except Exception:
                    logger.exception("persist retry-as-skill failed for slug=%s", slug)

            # Auto-retry hook: if this attempt failed and we haven't already
            # retried, launch a second attempt with the dynamic "auto" planner
            # and inject the failure context as a lesson. The retry runs as a
            # background task so the original call returns immediately.
            if manifest.get("status") == "failed":
                await self._maybe_auto_retry(
                    manifest,
                    str(manifest.get("brief_raw") or manifest.get("brief") or brief),
                    slug,
                )
            return manifest
        except Exception as exc:
            # The runner itself crashed (agent init blew up, get_agent raised, etc).
            # Without this guard the manifest stays at {stages: [], completed_at: None}
            # and the user sees a project that ran 0 stages with no error.
            is_stack_mismatch = isinstance(exc, StackShapeMismatchError)
            is_stub_bail = isinstance(exc, UnresolvedScaffoldStubError)
            is_missing_files_bail = isinstance(exc, MissingPlannedFilesError)
            is_intentional_bail = is_stack_mismatch or is_stub_bail or is_missing_files_bail
            if is_intentional_bail:
                logger.info(
                    "studio runner bailed for slug=%s (intentional): %s", slug, exc
                )
            else:
                logger.exception("studio runner failed for slug=%s", slug)
            manifest["status"] = "failed"
            if is_intentional_bail:
                manifest["error"] = str(exc)
            else:
                manifest["error"] = (
                    f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}"
                )
            manifest["completed_at"] = time.time()
            manifest["current_stage"] = None
            manifest["current_agent"] = None
            self._clear_quality_summary(manifest)
            if is_stack_mismatch:
                manifest["next_action"] = (
                    "Project stopped: stack shape validation flagged files inconsistent "
                    "with the chosen stack. Review manifest['stack_shape_mismatches']."
                )
            elif is_stub_bail:
                manifest["next_action"] = "Retrying with the unresolved stub failure as a hint."
                manifest["_retry_hint"] = str(exc)
            elif is_missing_files_bail:
                manifest["next_action"] = "Retrying with the missing file witness as a hint."
                manifest["_retry_hint"] = str(exc)
            else:
                manifest["next_action"] = "Project stopped because the runner crashed."
            self._finalize_benchmark(manifest)
            self._append_history(
                manifest,
                "PROJECT_FAILED",
                status="failed",
                message=f"{type(exc).__name__}: {exc}",
                error=str(exc),
            )
            try:
                self._save_manifest(artifact_dir, manifest)
            except Exception:
                # Failure-state manifest write — if this loses, the
                # dashboard never sees this run as failed and the
                # auto-retry path can't read the error context.
                logger.warning(
                    "failure-state manifest write FAILED for slug=%s — "
                    "dashboard will misreport this run",
                    slug, exc_info=True,
                )
            self._publish("PROJECT_FAILED", {"slug": slug, "error": str(exc)})
            raise

    def list_projects(self) -> List[dict]:
        """Return every project manifest under ``projects_root``."""
        out: List[dict] = []
        if not self.projects_root.exists():
            return out
        for p in sorted(self.projects_root.iterdir()):
            if not p.is_dir():
                continue
            mf = p / "project.json"
            if mf.exists():
                try:
                    manifest = json.loads(mf.read_text())
                    if isinstance(manifest, dict):
                        out.append(self._normalize_manifest(manifest))
                except Exception:
                    continue
        return out

    def get_project(self, slug: str) -> Optional[dict]:
        """Return the manifest for ``slug`` or ``None`` if it does not exist."""
        mf = self.projects_root / slug / "project.json"
        if not mf.exists():
            return None
        try:
            manifest = json.loads(mf.read_text())
            return self._normalize_manifest(manifest) if isinstance(manifest, dict) else None
        except Exception:
            return None

    def export_zip(self, slug: str) -> Path:
        """Bundle a project directory into a zip file and return its path."""
        import shutil

        src = self.projects_root / slug
        if not src.exists():
            raise FileNotFoundError(slug)
        zip_path = self.projects_root / f"{slug}.zip"
        if zip_path.exists():
            zip_path.unlink()
        base = str(zip_path)
        if base.endswith(".zip"):
            base = base[:-4]
        shutil.make_archive(base, "zip", src)
        return zip_path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _slugify(s: str) -> str:
        s = (s or "").lower().strip()
        # Collapse any run of non-alnum characters to a single dash.
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = s.strip("-")
        if not s:
            s = "project"
        s = s[:60].rstrip("-")
        suffix = f"{time.time_ns():x}"[-6:]
        return f"{s}-{suffix}"

    # Extensions we treat as "real software output" — running code, markup,
    # styles, config, schemas. If the project produced any of these, it's
    # not a docs-only result. Markdown, text, and PDF are NOT in this list.
    _CODE_LIKE_EXTENSIONS = {
        ".py", ".pyi",
        ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
        ".html", ".htm", ".css", ".scss", ".sass",
        ".vue", ".svelte",
        ".go", ".rs", ".java", ".kt", ".swift",
        ".c", ".cc", ".cpp", ".h", ".hpp",
        ".rb", ".php",
        ".sql",
        ".sh", ".bash", ".zsh",
        ".yml", ".yaml", ".toml", ".ini", ".cfg",
        ".json", ".jsonc",
        ".dockerfile",  # users sometimes call it that
        ".tf", ".tfvars",
    }

    @classmethod
    def _is_docs_only_for_code_brief(cls, brief: str, artifacts: List[str]) -> bool:
        """Return True when the brief implied software but only docs got produced.

        Reuses the planner's own classification so the failure signal and the
        retry's forced-code-stage logic stay aligned. If the brief is empty,
        explicitly docs-shaped, or names a target file, we don't flag it.
        """
        from skyn3t.studio.planner import _should_force_code_agent

        if not _should_force_code_agent(brief or ""):
            return False
        if not artifacts:
            # Nothing at all was produced — that's a different failure shape
            # (handled by the per-stage failed-stage path); don't double-flag.
            return False
        for rel in artifacts:
            # Filename like "Dockerfile" with no extension still counts.
            name = Path(rel).name.lower()
            if name in {"dockerfile", "makefile", "procfile"}:
                return False
            suffix = Path(rel).suffix.lower()
            if suffix in cls._CODE_LIKE_EXTENSIONS:
                return False
        return True

    async def _apply_build_fix_round(
        self,
        scaffold_dir: Path,
        brief: str,
        build_result: Dict[str, Any],
        attempt: int,
    ) -> bool:
        """Surgical fix: parse the build log to identify specific broken
        files, then regenerate ONLY those files using the targeted fix
        engine. Falls back to whole-scaffold rewrite if parsing fails.

        This is the cheap retry shape — closer to how iterative dev works.
        We don't burn a fresh pipeline; we just edit the scaffold in place
        and re-run the gate.
        """
        import re as _re

        from skyn3t.adapters import LLMClient
        from skyn3t.agents.targeted_fix import (
            _parse_build_errors,
            apply_targeted_fix,
        )

        stderr = (build_result.get("stderr") or "")
        stdout = (build_result.get("stdout") or "")
        stack = build_result.get("stack") or "unknown"
        log_tail = (stderr or stdout).strip()
        if not log_tail:
            return False

        # ── Phase 1: targeted fix (preferred) ────────────────────────────
        issues = _parse_build_errors(stderr, stdout)
        if issues:
            client = LLMClient(event_bus=self.event_bus, caller_name="build_fix")
            try:
                result = await apply_targeted_fix(
                    scaffold_dir=scaffold_dir,
                    issues=issues,
                    llm_client=client,
                    brief=brief,
                    stack=stack,
                )
            except Exception:
                logger.exception("targeted fix engine failed on round %d", attempt)
                result = None
            if result is not None and (result.files_changed or result.files_created):
                logger.info(
                    "Targeted fix round %d: changed=%s created=%s errors=%s",
                    attempt,
                    result.files_changed,
                    result.files_created,
                    result.errors,
                )
                return True
            # If targeted fix produced nothing, fall through to heuristic path

        # ── Phase 2: heuristic fixes for common Rollup/Vite errors ───────
        # These are fast regex-based fixes that don't need an LLM call.
        heuristic_fixed = self._apply_heuristic_build_fixes(
            scaffold_dir, stderr, stdout
        )
        if heuristic_fixed:
            logger.info("Heuristic build fix applied for round %d", attempt)
            return True

        # ── Phase 3: legacy whole-scaffold rewrite (fallback) ────────────
        files_on_disk: List[tuple[str, str]] = []
        for p in sorted(scaffold_dir.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts or any(
                part in self._SCAFFOLD_SHAPE_SKIP for part in p.parts
            ):
                continue
            try:
                rel = p.relative_to(scaffold_dir).as_posix()
                body = p.read_text(encoding="utf-8")
            except Exception:
                continue
            files_on_disk.append((rel, body[:6000]))
        if not files_on_disk:
            return False

        client = LLMClient(event_bus=self.event_bus, caller_name="build_fix")
        system = (
            "You are a senior engineer fixing a small project that just "
            "failed its build/compile check. You are given the brief, the "
            "stack, the current file tree (with contents), and the build "
            "log. Reply with a JSON object of the form: "
            "{\"files\": [{\"path\": \"rel/path\", \"content\": \"FULL "
            "NEW FILE CONTENT\"}, ...]}. ONLY include files you are "
            "changing. Each `content` must be the complete new file body "
            "— not a diff. JSON only, no preamble."
        )
        file_block = "\n\n".join(
            f"### {rel}\n```\n{body}\n```" for rel, body in files_on_disk[:20]
        )
        prompt = (
            f"Brief:\n{brief}\n\n"
            f"Stack: {stack}\n\n"
            f"Current scaffold (first {min(len(files_on_disk), 20)} files):\n"
            f"{file_block}\n\n"
            f"Build log tail:\n{log_tail[-2000:]}\n\n"
            f"Fix the broken files. Return JSON: "
            f"{{\"files\":[{{\"path\":\"…\",\"content\":\"…\"}}]}}."
        )

        try:
            out = await client.complete(
                prompt,
                system=system,
                max_tokens=8000,
                temperature=0.2,
                timeout=_BUILD_FIX_LLM_TIMEOUT_SECONDS,
                _allow_backend_failover=False,
            )
        except Exception:
            logger.exception("LLM call during fix round %d failed", attempt)
            return False
        if not out or "[deterministic-stub]" in out:
            return False
        m = _re.search(r"\{[\s\S]*\}", out)
        if not m:
            return False
        try:
            data = json.loads(m.group(0))
        except Exception:
            return False
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list) or not files:
            return False

        resolved_root = scaffold_dir.resolve()
        wrote_any = False
        for spec in files[:25]:
            if not isinstance(spec, dict):
                continue
            rel = (spec.get("path") or "").lstrip("/").strip()
            content = spec.get("content")
            if not rel or not isinstance(content, str) or not content.strip():
                continue
            # Reject paths that touch skip patterns (node_modules, .git, etc.)
            if any(part in self._SCAFFOLD_SHAPE_SKIP for part in Path(rel).parts):
                logger.warning("LLM fix proposed skip-path %s; ignoring", rel)
                continue
            target = (scaffold_dir / rel).resolve()
            try:
                target.relative_to(resolved_root)
            except ValueError:
                continue  # path escape — never write
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                wrote_any = True
            except Exception:
                logger.exception("could not write fix to %s", target)
                continue
        return wrote_any

    async def _apply_integration_fix_round(
        self,
        *,
        scaffold_dir: Path,
        brief: str,
        integration_result: Dict[str, Any],
        attempt: int,
    ) -> bool:
        """Apply a targeted fix round using integration-verifier findings."""
        from skyn3t.adapters import LLMClient
        from skyn3t.agents.targeted_fix import FileIssue, apply_targeted_fix

        targets = self._integration_fix_targets(scaffold_dir, integration_result)
        failure_hint = (
            integration_result.get("failure_hint")
            or integration_result.get("summary")
            or "Integration contract failed."
        )
        stack = integration_result.get("kind") or "unknown"

        if not targets:
            # Fall back to the generic build-fix path if we couldn't
            # map missing routes to concrete backend files.
            synthetic = {
                "stderr": "",
                "stdout": failure_hint,
                "stack": stack,
            }
            return await self._apply_build_fix_round(
                scaffold_dir=scaffold_dir,
                brief=brief,
                build_result=synthetic,
                attempt=attempt,
            )

        issues = [
            FileIssue(
                path=path,
                error_message=failure_hint,
                suggested_action="regenerate",
            )
            for path in targets
        ]
        client = LLMClient(event_bus=self.event_bus, caller_name="integration_fix")
        try:
            result = await apply_targeted_fix(
                scaffold_dir=scaffold_dir,
                issues=issues,
                llm_client=client,
                brief=brief,
                stack=stack,
            )
        except Exception:
            logger.exception("integration targeted fix failed on round %d", attempt)
            return False
        if result is not None and (result.files_changed or result.files_created):
            logger.info(
                "Integration fix round %d: changed=%s created=%s errors=%s",
                attempt,
                result.files_changed,
                result.files_created,
                result.errors,
            )
            return True
        return False

    def _apply_heuristic_build_fixes(
        self, scaffold_dir: Path, stderr: str, stdout: str
    ) -> bool:
        """Fast regex-based fixes for common Rollup/Vite build errors.

        Returns True if any fix was applied. These run before the expensive
        LLM-based fallback so trivial export/syntax issues are resolved
        instantly.
        """
        import re

        text = f"{stderr}\n{stdout}"
        fixed_any = False

        # Heuristic 1: Rollup "X is not exported by Y" → add export keyword
        for m in re.finditer(
            r'"([^"]+)"\s+is\s+not\s+exported\s+by\s+"([^"]+)"',
            text,
            re.IGNORECASE,
        ):
            symbol = m.group(1)
            rel_path = m.group(2)
            target = scaffold_dir / rel_path
            if not target.exists():
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except Exception:
                continue
            if symbol == "default":
                # Importer uses "import X from './file'" but exporter has no
                # default export. Look for an existing named export first,
                # then fall back to any top-level definition.
                # Patterns (in priority order):
                #   export function X → export default function X
                #   export const X → export default const X
                #   export class X → export default class X
                #   function X → export default function X
                #   const X → export default const X
                #   class X → export default class X
                # If no named symbol is known, try the file basename as a guess.
                file_stem = Path(rel_path).stem
                guess_names = {file_stem}
                # Also extract the importer's local name from the build error
                # if available: "import useConfig from ..."
                for imp_m in re.finditer(
                    rf'import\s+(\w+)\s+from\s+["\'][^"\']*{re.escape(Path(rel_path).name)}["\']',
                    text,
                ):
                    guess_names.add(imp_m.group(1))

                default_patterns = []
                for name in guess_names:
                    default_patterns.extend([
                        (rf'^(\s*)export\s+(function\s+{re.escape(name)}\b)', r'\1export default \2'),
                        (rf'^(\s*)export\s+(const\s+{re.escape(name)}\b)', r'\1export default \2'),
                        (rf'^(\s*)export\s+(class\s+{re.escape(name)}\b)', r'\1export default \2'),
                        (rf'^(\s*)export\s+(let\s+{re.escape(name)}\b)', r'\1export default \2'),
                        (rf'^(\s*)export\s+(var\s+{re.escape(name)}\b)', r'\1export default \2'),
                        (rf'^(\s*)(function\s+{re.escape(name)}\b)', r'\1export default \2'),
                        (rf'^(\s*)(const\s+{re.escape(name)}\b)', r'\1export default \2'),
                        (rf'^(\s*)(class\s+{re.escape(name)}\b)', r'\1export default \2'),
                        (rf'^(\s*)(let\s+{re.escape(name)}\b)', r'\1export default \2'),
                        (rf'^(\s*)(var\s+{re.escape(name)}\b)', r'\1export default \2'),
                    ])
                # Also try any existing named export as a last resort
                default_patterns.extend([
                    (r'^(\s*)export\s+(function\s+\w+\b)', r'\1export default \2'),
                    (r'^(\s*)export\s+(const\s+\w+\b)', r'\1export default \2'),
                    (r'^(\s*)export\s+(class\s+\w+\b)', r'\1export default \2'),
                    (r'^(\s*)export\s+(let\s+\w+\b)', r'\1export default \2'),
                    (r'^(\s*)export\s+(var\s+\w+\b)', r'\1export default \2'),
                    (r'^(\s*)(function\s+\w+\b)', r'\1export default \2'),
                    (r'^(\s*)(const\s+\w+\b)', r'\1export default \2'),
                    (r'^(\s*)(class\s+\w+\b)', r'\1export default \2'),
                    (r'^(\s*)(let\s+\w+\b)', r'\1export default \2'),
                    (r'^(\s*)(var\s+\w+\b)', r'\1export default \2'),
                ])
                for old_pat, new_pat in default_patterns:
                    new_content, count = re.subn(old_pat, new_pat, content, count=1, flags=re.MULTILINE)
                    if count > 0:
                        try:
                            target.write_text(new_content, encoding="utf-8")
                            logger.info("Heuristic: added default export to %s", rel_path)
                            fixed_any = True
                            break
                        except Exception:
                            logger.exception("Heuristic fix write failed for %s", target)
            else:
                # Named export missing — look for the symbol definition without
                # 'export' before it.
                patterns = [
                    (rf'^(\s*)(function\s+{re.escape(symbol)}\b)', r'\1export \2'),
                    (rf'^(\s*)(const\s+{re.escape(symbol)}\b)', r'\1export \2'),
                    (rf'^(\s*)(let\s+{re.escape(symbol)}\b)', r'\1export \2'),
                    (rf'^(\s*)(class\s+{re.escape(symbol)}\b)', r'\1export \2'),
                    (rf'^(\s*)(var\s+{re.escape(symbol)}\b)', r'\1export \2'),
                ]
                for old_pat, new_pat in patterns:
                    new_content, count = re.subn(old_pat, new_pat, content, count=1, flags=re.MULTILINE)
                    if count > 0:
                        try:
                            target.write_text(new_content, encoding="utf-8")
                            logger.info("Heuristic: added export to %s in %s", symbol, rel_path)
                            fixed_any = True
                            break
                        except Exception:
                            logger.exception("Heuristic fix write failed for %s", target)

        return fixed_any

    def _persist_fix_as_skill(
        self,
        *,
        stack: str,
        fix_round: int,
        prior_summary: Optional[str] = None,
    ) -> None:
        """Write a learned skill for a successful in-place fix.

        The skill is keyed by (stack, fix-round) so two stacks with
        independent fix patterns don't collide. Body captures the prior
        verifier summary as the "this is the failure mode" anchor and
        notes that the fix loop resolved it in round N. The next time
        CodeAgent scaffolds for the same stack, this skill ends up in
        the system prompt and biases the model toward producing the
        already-fixed shape on the first try.
        """
        try:
            from skyn3t.intelligence.skill_library import Skill, get_default_library
        except Exception:
            return
        if not stack or stack == "unknown":
            return
        name = f"{stack}-fix-loop-round-{fix_round}"
        body_lines = [
            f"# Build fix learned for `{stack}`.",
            "",
            f"The in-place fix loop resolved a build failure on round {fix_round}.",
            "",
        ]
        if prior_summary:
            body_lines.extend([
                "## Original failure",
                "",
                prior_summary.strip()[:1000],
                "",
            ])
        body_lines.extend([
            "## Action",
            "",
            "When generating files for this stack, watch for the failure shape "
            "above and pre-emptively apply the patch the fix round produced. "
            "Re-using this skill should reduce the number of fix rounds the "
            "next similar build needs.",
        ])
        skill = Skill(
            name=name,
            tags=[stack, "fix-loop", "build-success"],
            success_count=1,
            failure_count=0,
            source="runner:in_place_fix_loop",
            body="\n".join(body_lines),
        )
        try:
            get_default_library().upsert(skill)
        except Exception:
            logger.exception("skill upsert failed for fix-loop skill %s", name)

    def _persist_retry_as_skill(
        self,
        *,
        retry_slug: str,
        retry_manifest: Dict[str, Any],
    ) -> None:
        """Write a skill when an auto-retry resolved a parent project's failure.

        The retry path is a different escape hatch from the in-place fix
        loop: a parent project hit the verifier wall, an entire new project
        was scaffolded with the failure as a hint, and that new project
        succeeded. The next homelab brief (or whatever stack this was)
        should pre-emptively avoid the same break.

        Skill body is intentionally compact — the model only needs the
        failure shape and a "watch for this" directive. The full diff
        is too noisy to bias the system prompt usefully.
        """
        try:
            from skyn3t.intelligence.skill_library import Skill, get_default_library
        except Exception:
            return
        # Find the parent project: retry slug always ends "-retry" and the
        # parent saved `_retry_slug` on its manifest. Walk back via the
        # projects_root rather than tracking state in memory so the hook
        # survives backend restarts.
        if not retry_slug.endswith("-retry"):
            return
        parent_slug = retry_slug[:-len("-retry")]
        parent_dir = self.projects_root / parent_slug
        parent_manifest_path = parent_dir / "project.json"
        if not parent_manifest_path.exists():
            return
        try:
            import json as _json
            parent_manifest = _json.loads(parent_manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(parent_manifest, dict):
            return
        # Pull the failure signature from the parent's verifier output.
        bv = parent_manifest.get("build_verification") or {}
        failure_summary = (
            (parent_manifest.get("_retry_hint") or "").strip()
            or (bv.get("failure_hint") or "").strip()
            or (bv.get("summary") or "").strip()
            or (parent_manifest.get("error") or "").strip()
        )
        if not failure_summary:
            return  # nothing actionable to record
        # Stack: prefer what the retry produced (which is the
        # known-good shape) over the parent's stack guess.
        stack = (
            (retry_manifest.get("build_verification") or {}).get("stack")
            or (parent_manifest.get("build_verification") or {}).get("stack")
            or retry_manifest.get("template")
            or "unknown"
        )
        if not stack or stack == "unknown":
            return
        # Skill name: tied to the stack and a short hash of the failure
        # summary so repeat failures of the same shape upsert into the
        # same skill (incrementing success_count) instead of spamming
        # the library with near-duplicates.
        try:
            import hashlib as _hashlib
            sig = _hashlib.sha1(failure_summary.encode("utf-8", "replace")).hexdigest()[:8]
        except Exception:
            sig = "x"
        name = f"{stack}-retry-recovery-{sig}"
        body_lines = [
            f"# Retry recovery for `{stack}`.",
            "",
            "A prior scaffold of this stack failed verification, then a full "
            "auto-retry succeeded. The failure shape below is now known-bad — "
            "pre-empt it on the first attempt next time.",
            "",
            "## Failure that triggered the retry",
            "",
            failure_summary[:1200],
            "",
            "## Action",
            "",
            "When generating files for this stack, watch for the failure "
            "shape above. Common causes for this class of break: malformed "
            "JS (unterminated template literals, leftover markdown fences), "
            "JSON with unquoted keys, Python with mis-indented blocks, or "
            "missing exports. Pre-check syntactically before declaring a "
            "file complete.",
        ]
        skill = Skill(
            name=name,
            tags=[stack, "retry-recovery", "build-success"],
            success_count=1,
            failure_count=0,
            source="runner:auto_retry",
            body="\n".join(body_lines),
        )
        try:
            get_default_library().upsert(skill)
            logger.info(
                "persisted retry-recovery skill: %s (parent=%s, retry=%s)",
                name, parent_slug, retry_slug,
            )
        except Exception:
            logger.exception("skill upsert failed for retry-recovery skill %s", name)

    async def _run_build_verifier(self, scaffold_dir: str, brief: str) -> Optional[Dict[str, Any]]:
        """Invoke BuildVerifierAgent in-process and return its output dict.

        Constructed fresh per project rather than fetched from the orchestrator
        — that way the verifier doesn't require any registered-agent plumbing,
        and tests can run it without booting the full stack.
        """
        try:
            from skyn3t.agents.build_verifier import BuildVerifierAgent
        except Exception:
            return None
        from skyn3t.core.agent import TaskRequest
        try:
            agent = BuildVerifierAgent(event_bus=self.event_bus)
            await agent.initialize()
            result = await agent.execute(
                TaskRequest(
                    title="build-verify",
                    description=f"verify scaffold for: {brief[:120]}",
                    input_data={"scaffold_dir": scaffold_dir, "brief": brief},
                )
            )
        except Exception:
            logger.exception("build_verifier execute failed")
            return None
        if not result.success or not isinstance(result.output, dict):
            return None
        return result.output

    async def _run_boot_verifier(self, scaffold_dir: str, brief: str) -> Optional[Dict[str, Any]]:
        """Invoke BootVerifierAgent — actually start the server and
        confirm it serves a request.

        Returns the agent's output dict (same shape as build_verifier
        for fix-loop compatibility) or None if the agent itself
        couldn't run (not on a failure verdict — that's data we want
        to surface).
        """
        try:
            from skyn3t.agents.boot_verifier import BootVerifierAgent
        except Exception:
            logger.debug("boot_verifier not available", exc_info=True)
            return None
        from skyn3t.core.agent import TaskRequest
        try:
            agent = BootVerifierAgent(event_bus=self.event_bus)
            await agent.initialize()
            result = await agent.execute(
                TaskRequest(
                    title="boot-verify",
                    description=f"boot scaffold for: {brief[:120]}",
                    input_data={"scaffold_dir": scaffold_dir, "brief": brief},
                )
            )
        except Exception:
            logger.exception("boot_verifier execute failed")
            return None
        if not result.success or not isinstance(result.output, dict):
            return None
        return result.output

    async def _run_integration_verifier(self, scaffold_dir: str, brief: str) -> Optional[Dict[str, Any]]:
        """Invoke IntegrationContractVerifierAgent — checks frontend API
        calls against backend routes.

        Returns the agent's output dict (same shape as build_verifier
        for fix-loop compatibility) or None if the agent itself
        couldn't run.
        """
        try:
            from skyn3t.agents.integration_verifier import IntegrationContractVerifierAgent
        except Exception:
            logger.debug("integration_verifier not available", exc_info=True)
            return None
        from skyn3t.core.agent import TaskRequest
        try:
            agent = IntegrationContractVerifierAgent(event_bus=self.event_bus)
            await agent.initialize()
            result = await agent.execute(
                TaskRequest(
                    title="integration-verify",
                    description=f"verify integration for: {brief[:120]}",
                    input_data={"scaffold_dir": scaffold_dir, "brief": brief},
                )
            )
        except Exception:
            logger.exception("integration_verifier execute failed")
            return None
        if not result.success or not isinstance(result.output, dict):
            return None
        return result.output

    async def _kill_stray_server_processes(self, scaffold_dir: str) -> None:
        """Kill any lingering server processes that may be listening on ports.

        This prevents port reuse race conditions in the integration fix loop.
        The boot verifier kills processes internally, but we ensure a clean
        slate before rebooting.
        """
        try:
            import socket

            import psutil

            # Try to find and kill processes listening on common server ports
            # Look for Node or Python processes in the scaffold directory
            scaffold_path = Path(scaffold_dir).resolve()
            for port in [3000, 3100, 5000, 8000, 8080, 8888]:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.5)
                    result = s.connect_ex(("127.0.0.1", port))
                    s.close()
                    if result == 0:
                        # Port is listening, try to find the process
                        for proc in psutil.process_iter(["pid", "name", "cwd"]):
                            try:
                                if (
                                    proc.info["cwd"]
                                    and Path(proc.info["cwd"]).resolve() == scaffold_path
                                ):
                                    if proc.info["name"] in ("node", "python", "python3"):
                                        proc.terminate()
                                        await asyncio.sleep(0.5)
                                        if proc.is_running():
                                            proc.kill()
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                except Exception:
                    pass
        except ImportError:
            # psutil not available, skip this cleanup
            logger.debug("psutil not available for stray process cleanup")
        except Exception:
            logger.debug("stray process cleanup failed (non-fatal)", exc_info=True)

    # ─────────────────────────────────────────────────────────────────
    # Inter-agent conversation: critique + revise
    # ─────────────────────────────────────────────────────────────────
    # The mission's #1 missing feature is cross-model debate between
    # agents. A second model reads the first model's output, names
    # the 1-3 most important issues, and the original agent gets a
    # chance to revise. Bounded to one critique + one revision per
    # stage so wall time only goes up ~30%, not unbounded.

    _CRITIQUE_SKIP_STAGES = {
        # These stages either already ARE the critique step or are
        # too cheap to be worth critiquing.
        "brainstorm",     # divergent ideation, no "right answer"
        "reviewer",       # is itself the critique step
        "verifier",       # mechanical pass/fail
        "build_verifier", # mechanical pass/fail
        # Code used to be skipped because a revision re-ran the ENTIRE
        # per-file scaffold loop (~40 min). Now we use targeted_fix
        # for per-file regeneration, so code-stage critique is enabled.
    }

    async def _critique_and_revise(
        self,
        *,
        stage: Any,
        agent: Any,
        result: Any,
        artifact_dir: Path,
        brief: str,
        task: TaskRequest,
        manifest: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Agent-mediated critique with multi-round revision loop.

        Uses a real ReviewerAgent (not a temporary LLMClient) for cross-model
        debate. Supports multi-round critique → revision. For the code
        stage, uses targeted_fix for per-file regeneration instead of full
        re-scaffold.
        """
        import os as _os
        if _os.environ.get("SKYN3T_DISABLE_CRITIQUE") == "1":
            return result
        if stage.name in self._CRITIQUE_SKIP_STAGES:
            return result

        output = getattr(result, "output", None) or {}
        if stage.name == "code":
            missing_planned_files, unresolved_stub_files = self._code_stage_fast_retry_signals(
                artifact_dir=artifact_dir,
                brief=brief,
                output=output if isinstance(output, dict) else None,
            )
            if missing_planned_files or unresolved_stub_files:
                reasons: List[str] = []
                if missing_planned_files:
                    reasons.append(
                        f"{len(missing_planned_files)} planned file(s) still missing"
                    )
                if unresolved_stub_files:
                    reasons.append(
                        f"{len(unresolved_stub_files)} unresolved TODO stub(s)"
                    )
                if manifest is not None:
                    self._append_history(
                        manifest,
                        "CRITIQUE_SKIPPED",
                        status="running",
                        stage=stage.name,
                        message=(
                            "Skipped code critique because the scaffold already needs "
                            "fast retry: " + ", ".join(reasons) + "."
                        ),
                    )
                return result
        summary = self._summarize_stage_output(output)
        produced = self._collect_stage_artifacts(stage, output, artifact_dir)
        if not produced and not summary:
            return result

        # Instantiate a real ReviewerAgent for cross-model critique.
        try:
            reviewer = get_agent("ReviewerAgent", event_bus=self.event_bus, rag=self.rag)
            if hasattr(reviewer, "initialize"):
                maybe = reviewer.initialize()
                if hasattr(maybe, "__await__"):
                    await maybe
        except Exception as e:
            logger.warning(f"Failed to instantiate ReviewerAgent for critique: {e}")
            return result

        execution_profile = str((manifest or {}).get("execution_profile") or "balanced")
        max_rounds = self._critique_rounds_for(
            stage_name=stage.name,
            brief=brief,
            execution_profile=execution_profile,
        )
        current_result = result
        current_output = output

        for round_num in range(1, max_rounds + 1):
            if round_num == 1:
                self._publish(
                    "AGENT_CONVERSATION_STARTED",
                    {
                        "slug": artifact_dir.name,
                        "stage": stage.name,
                        "participants": [stage.agent, "ReviewerAgent"],
                        "max_rounds": max_rounds,
                    },
                )

            stage_files = self._normalize_stage_files(
                current_output.get("files") if isinstance(current_output, dict) else None,
                artifact_dir=artifact_dir,
            )

            # For code stage, critique the scaffold contents directly so
            # file paths in issues are relative to the scaffold root,
            # matching what apply_targeted_fix expects.
            critique_artifact_dir = (
                artifact_dir / "scaffold" if stage.name == "code" else artifact_dir
            )
            # Normalize produced_files to be relative to the critique dir.
            if stage.name == "code" and stage_files:
                critique_produced_files = [
                    f[len("scaffold/"):] if f.startswith("scaffold/") else f
                    for f in stage_files
                ]
            else:
                critique_produced_files = stage_files
            critique_timeout_seconds = 180.0
            try:
                critique_result = await asyncio.wait_for(
                    reviewer.critique(
                        brief=brief,
                        artifact_dir=critique_artifact_dir,
                        stage_name=stage.name,
                        produced_files=[str(f) for f in critique_produced_files]
                        if critique_produced_files
                        else None,
                    ),
                    timeout=critique_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "ReviewerAgent critique timed out for %s round %s",
                    stage.name,
                    round_num,
                )
                if manifest is not None:
                    self._append_history(
                        manifest,
                        "CRITIQUE_FAILED",
                        status="running",
                        stage=stage.name,
                        message=(
                            f"Critique round {round_num} timed out after "
                            f"{critique_timeout_seconds:.0f}s."
                        ),
                    )
                return current_result
            except Exception as e:
                logger.warning(
                    f"ReviewerAgent critique failed for {stage.name} round {round_num}: {e}"
                )
                if manifest is not None:
                    self._append_history(
                        manifest,
                        "CRITIQUE_FAILED",
                        status="running",
                        stage=stage.name,
                        message=f"Critique round {round_num} failed: {e}",
                    )
                return current_result

            has_issues = critique_result.get("has_issues", False)
            issues = critique_result.get("issues", [])
            critique_text = critique_result.get("critique_text", "")

            self._publish(
                "AGENT_CONVERSATION_TURN",
                {
                    "slug": artifact_dir.name,
                    "stage": stage.name,
                    "round": round_num,
                    "has_issues": has_issues,
                    "issue_count": len(issues),
                },
            )

            if not has_issues or not issues:
                try:
                    await agent.think(
                        f"critique pass on {stage.name}: clean (round {round_num})"
                    )
                except Exception:
                    pass
                self._publish(
                    "AGENT_CONVERSATION_ENDED",
                    {
                        "slug": artifact_dir.name,
                        "stage": stage.name,
                        "rounds": round_num,
                        "resolved": True,
                    },
                )
                return current_result

            try:
                await agent.think(
                    f"critique on {stage.name} round {round_num}:\n{critique_text[:400]}"
                )
            except Exception:
                pass

            if manifest is not None:
                self._append_history(
                    manifest,
                    "CRITIQUE_ISSUES_FOUND",
                    status="running",
                    stage=stage.name,
                    message=f"{len(issues)} issue(s) found in round {round_num}.",
                )

            # ── Apply revision ──
            if stage.name == "code":
                try:
                    from skyn3t.adapters import LLMClient
                    from skyn3t.agents.consistency_engine import check_consistency
                    from skyn3t.agents.targeted_fix import (
                        FileIssue,
                        apply_targeted_fix,
                    )

                    scaffold_dir = artifact_dir / "scaffold"
                    if not scaffold_dir.exists():
                        logger.warning(
                            "No scaffold dir for targeted fix; falling back to re-execute"
                        )
                    else:
                        file_issues = []
                        for i in issues:
                            raw_fp = i.get("file", "unknown")
                            # Some LLMs concatenate multiple files with " + "; split them.
                            for fp in [p.strip() for p in raw_fp.split(" + ")]:
                                if not fp or fp in ("unknown", "(unknown)"):
                                    continue
                                # Skip entries that don't look like file paths.
                                # Reviewers sometimes return prose like "Stack mismatch"
                                # or "package.json" as the file field when they mean
                                # something else.  A valid path has at least one dot
                                # or a slash (except for bare filenames like README).
                                if "/" not in fp and "." not in fp and fp not in ("README", "README.md"):
                                    logger.warning(
                                        "Skipping non-path file entry from reviewer: %s", fp
                                    )
                                    continue
                                resolved_fp = self._normalize_scaffold_issue_path(
                                    scaffold_dir,
                                    fp,
                                )
                                if not resolved_fp:
                                    logger.warning(
                                        "Skipping unresolved reviewer file entry from critique: %s",
                                        fp,
                                    )
                                    continue
                                file_issues.append(
                                    FileIssue(
                                        path=resolved_fp,
                                        error_message=i.get("problem", ""),
                                        suggested_action="regenerate",
                                    )
                                )
                        client = LLMClient(
                            event_bus=self.event_bus,
                            caller_name="code_critique_fix",
                        )
                        fix_result = await apply_targeted_fix(
                            scaffold_dir=scaffold_dir,
                            issues=file_issues,
                            llm_client=client,
                            brief=brief,
                        )
                        if manifest is not None:
                            fix_msg = (
                                f"Fixed {len(fix_result.files_changed)} file(s) "
                                f"via targeted fix (round {round_num})."
                            )
                            if fix_result.errors:
                                fix_msg += " Errors: " + "; ".join(fix_result.errors[:3])
                            self._append_history(
                                manifest,
                                "CODE_CRITIQUE_FIX_APPLIED",
                                status="running",
                                stage=stage.name,
                                message=fix_msg,
                            )
                        # Re-check after fix
                        report = check_consistency(scaffold_dir, brief=brief)
                        current_output = {
                            **current_output,
                            "files_changed": fix_result.files_changed,
                            "files_created": fix_result.files_created,
                            "consistency_ok": report.ok,
                            "consistency_issues": len(report.issues),
                        }
                        current_result = TaskResult(
                            task_id=task.task_id,
                            success=True,
                            output=current_output,
                        )
                        if round_num == max_rounds:
                            self._publish(
                                "AGENT_CONVERSATION_ENDED",
                                {
                                    "slug": artifact_dir.name,
                                    "stage": stage.name,
                                    "rounds": round_num,
                                    "resolved": report.ok,
                                },
                            )
                            return current_result
                        continue
                except Exception as e:
                    logger.warning(f"Targeted fix failed for code critique: {e}")
                    # Fall through to re-execute path

            # Non-code stage (or code fallback): append critique to brief and re-execute
            revise_input = dict(task.input_data or {})
            original_brief = str(revise_input.get("brief") or brief or "")
            revise_input["brief"] = (
                f"{original_brief}\n\n"
                f"---\n"
                f"REVISION NOTES from cross-model reviewer (round {round_num}) "
                f"(apply these BEFORE writing your output):\n\n"
                f"{critique_text}\n\n"
                f"Your previous output summary: {summary or '(none)'}\n"
                f"---\n"
            )
            revise_input["_critique"] = critique_text
            revise_input["_revision_round"] = round_num
            revise_input["_prior_output_summary"] = summary

            revise_task = TaskRequest(
                title=f"revise:{stage.name}:r{round_num}",
                description=f"Revise after critique for stage {stage.name} (round {round_num})",
                input_data=revise_input,
            )
            try:
                revised = await asyncio.wait_for(
                    agent.execute(revise_task), timeout=600.0
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"revision pass timed out for {stage.name} round {round_num}"
                )
                self._publish(
                    "AGENT_CONVERSATION_ENDED",
                    {
                        "slug": artifact_dir.name,
                        "stage": stage.name,
                        "rounds": round_num,
                        "resolved": False,
                    },
                )
                return current_result
            except Exception as e:
                logger.warning(
                    f"revision pass failed for {stage.name} round {round_num}: {e}"
                )
                self._publish(
                    "AGENT_CONVERSATION_ENDED",
                    {
                        "slug": artifact_dir.name,
                        "stage": stage.name,
                        "rounds": round_num,
                        "resolved": False,
                    },
                )
                return current_result

            if not getattr(revised, "success", False):
                self._publish(
                    "AGENT_CONVERSATION_ENDED",
                    {
                        "slug": artifact_dir.name,
                        "stage": stage.name,
                        "rounds": round_num,
                        "resolved": False,
                    },
                )
                return current_result

            current_result = revised
            current_output = getattr(revised, "output", None) or {}
            try:
                await agent.think(
                    f"revised {stage.name} after critique (round {round_num})"
                )
            except Exception:
                pass
            self._publish(
                "PROJECT_STAGE_REVISED",
                {
                    "slug": artifact_dir.name,
                    "stage": stage.name,
                    "agent": stage.agent,
                    "round": round_num,
                },
            )

            if round_num == max_rounds:
                self._publish(
                    "AGENT_CONVERSATION_ENDED",
                    {
                        "slug": artifact_dir.name,
                        "stage": stage.name,
                        "rounds": round_num,
                        "resolved": False,
                    },
                )
                return current_result

        # Defensive fallback
        self._publish(
            "AGENT_CONVERSATION_ENDED",
            {
                "slug": artifact_dir.name,
                "stage": stage.name,
                "rounds": max_rounds,
                "resolved": False,
            },
        )
        return current_result

    def _collect_stage_artifacts(
        self, stage: Any, output: Any, artifact_dir: Path,
    ) -> str:
        """Read produced .md/.json/.txt files for the critique prompt.

        Capped so the critique input doesn't balloon — we want the
        critic to see the SHAPE of what was produced, not every byte.
        """
        chunks: list[str] = []
        files: list[str] = []
        if isinstance(output, dict):
            files = output.get("files") or []
        for rel in files[:10]:
            try:
                p = (artifact_dir / rel) if not Path(rel).is_absolute() else Path(rel)
                if not p.exists() or not p.is_file():
                    continue
                body = p.read_text(encoding="utf-8", errors="ignore")
                # Limit per-file to keep critique prompt manageable.
                if len(body) > 1500:
                    body = body[:1500] + "\n[truncated]"
                chunks.append(f"### {p.name}\n\n{body}")
            except Exception:
                continue
        return "\n\n---\n\n".join(chunks)

    _SCAFFOLD_SHAPE_SKIP = {
        "node_modules", ".git", "dist", "build", ".next", "out",
        "coverage", ".pytest_cache", "__pycache__", ".mypy_cache",
        ".ruff_cache", ".DS_Store",
    }

    def _scaffold_shape(self, scaffold_dir: Path) -> List[str]:
        """Return sorted relative paths of source files in the scaffold.

        Excludes dependency dirs, build outputs, and system files so the
        scoreboard shape signature reflects the author's intent, not the
        output of ``npm install``.
        """
        if not scaffold_dir.exists():
            return []
        return sorted(
            p.relative_to(scaffold_dir).as_posix()
            for p in scaffold_dir.rglob("*")
            if p.is_file()
            and not any(part in self._SCAFFOLD_SHAPE_SKIP for part in p.parts)
        )

    def _stack_shape_mismatches(self, scaffold_dir: Path, brief: str) -> List[str]:
        from skyn3t.agents.stack_templates import detect_stack, validate_stack_shape

        detected = detect_stack(brief) or "unknown"
        if detected == "unknown":
            return []
        all_files = self._scaffold_shape(scaffold_dir)
        return validate_stack_shape(detected, all_files)

    @staticmethod
    def _scoreboard_prewarnings(brief: str) -> List[str]:
        """Return pre-warning strings derived from BuildPatternScoreboard.

        Looks up the planner-time shape for ``brief`` against the
        per-shape tag counts (e.g. ``missing_mount``). If a shape has
        accumulated enough failures of a known class, surface a one-
        line warning the CodeAgent can read.
        """
        warnings: List[str] = []
        try:
            from skyn3t.agents.stack_templates import detect_stack, plan_for_stack
            from skyn3t.intelligence.build_patterns import get_default_scoreboard

            stack = detect_stack(brief)
            if not stack:
                return warnings
            shape_files = plan_for_stack(stack, brief)
            if not shape_files:
                return warnings
            shape = [str(p) for p in shape_files]
            sb = get_default_scoreboard()

            # Threshold: 3+ occurrences of a tag for this (stack, shape)
            # combo earns a warning. Tunable; we picked 3 to match the
            # meta-agent's ``min_samples`` for shape-level decisions.
            mount_misses = sb.tag_count_for_shape(stack, shape, "missing_mount")
            if mount_misses >= 3:
                warnings.append(
                    f"⚠️ Past failure pattern for this {stack} shape: the "
                    f"server/routes/*.js routers got exported but never "
                    f"`app.use(...)`-mounted in server/index.js "
                    f"({mount_misses} prior occurrences). Double-check that "
                    f"every routes/*.js file is imported AND mounted in "
                    f"server/index.js before handoff."
                )
        except Exception:
            logger.debug("scoreboard pre-warning lookup failed", exc_info=True)
        return warnings

    def _code_stage_fast_retry_signals(
        self,
        *,
        artifact_dir: Path,
        brief: str,
        output: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], List[str]]:
        """Return missing-file and unresolved-stub signals for code-stage bailouts.

        When the scaffold already has missing planned files or visible TODO-style
        placeholders, the code-stage critique loop is low value: downstream retry
        logic and the dedicated reviewer/fix stages handle those cases better
        than spending several more minutes on targeted-fix attempts inside the
        code stage itself.
        """
        scaffold_dir = artifact_dir / "scaffold"
        missing_planned_files = self._remaining_missing_planned_files(output, scaffold_dir)
        unresolved_stub_files: List[str] = []
        if scaffold_dir.exists():
            try:
                from skyn3t.agents.consistency_engine import check_consistency

                report = check_consistency(scaffold_dir, brief)
                unresolved_stub_files = self._unresolved_todo_stub_files(report.issues)
            except Exception:
                logger.debug("code-stage fast-retry probe failed", exc_info=True)
        return missing_planned_files, unresolved_stub_files

    async def _run_post_code_checks(
        self,
        *,
        manifest: Dict[str, Any],
        artifact_dir: Path,
        brief: str,
        stage_name: str,
        stage_output: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Run post-code checks with parallel static verifiers."""
        scaffold_dir = artifact_dir / "scaffold"
        if not scaffold_dir.exists():
            return
        from skyn3t.agents.consistency_engine import check_consistency

        try:
            consistency_task = asyncio.to_thread(check_consistency, scaffold_dir, brief)
            shape_task = asyncio.to_thread(self._stack_shape_mismatches, scaffold_dir, brief)
            report, mismatches = await asyncio.gather(consistency_task, shape_task)
        except Exception:
            logger.exception("post-code checks failed")
            return

        manifest["consistency_check"] = {
            "ok": bool(report.ok),
            "issue_count": len(report.issues),
        }

        missing_planned_files = self._remaining_missing_planned_files(stage_output, scaffold_dir)
        if missing_planned_files:
            hint = self._missing_planned_files_retry_hint(missing_planned_files)
            manifest["consistency_check"]["missing_planned_files"] = missing_planned_files
            manifest["_retry_hint"] = hint
            manifest["next_action"] = "Retrying with the missing file witness as a hint."
            self._append_history(
                manifest,
                "MISSING_PLANNED_FILES",
                status="running",
                stage=stage_name,
                message=f"{len(missing_planned_files)} planned file(s) still missing after code stage.",
            )
            self._save_manifest(artifact_dir, manifest)
            raise MissingPlannedFilesError(hint)

        # Record per-category tags on the build-pattern scoreboard so the
        # planner can pre-warn future runs about shapes that historically
        # fail a specific way (e.g. lose the router mount).
        missing_mount_count = sum(
            1 for i in report.issues if i.category == "missing_mount"
        )
        if missing_mount_count:
            try:
                from skyn3t.agents.stack_templates import detect_stack
                from skyn3t.intelligence.build_patterns import (
                    get_default_scoreboard,
                )
                stack = detect_stack(brief) or "unknown"
                shape = self._scaffold_shape(scaffold_dir)
                if stack != "unknown" and shape:
                    get_default_scoreboard().record_tag(
                        stack, shape, "missing_mount",
                    )
            except Exception:
                logger.exception("missing_mount scoreboard tag failed")

        unresolved_stub_files = self._unresolved_todo_stub_files(report.issues)
        if unresolved_stub_files:
            hint = self._todo_stub_retry_hint(unresolved_stub_files)
            manifest["consistency_check"]["unresolved_todo_stubs"] = unresolved_stub_files
            manifest["_retry_hint"] = hint
            manifest["next_action"] = "Retrying with the unresolved stub failure as a hint."
            self._save_manifest(artifact_dir, manifest)
            raise UnresolvedScaffoldStubError(hint)

        # Fail fast on stack mismatch before mutating files with targeted fix.
        if mismatches:
            manifest["stack_shape_mismatches"] = mismatches
            self._append_history(
                manifest,
                "STACK_SHAPE_MISMATCH",
                status="running",
                stage=stage_name,
                message="; ".join(mismatches),
            )
            self._save_manifest(artifact_dir, manifest)
            raise StackShapeMismatchError(
                f"Stack shape mismatch found {len(mismatches)} inconsistent file(s): "
                + "; ".join(mismatches)
            )

        if not report.ok:
            from skyn3t.adapters import LLMClient
            from skyn3t.agents.targeted_fix import FileIssue, apply_targeted_fix

            consistency_issues = [
                FileIssue(
                    path=self._consistency_fix_target(
                        scaffold_dir=scaffold_dir,
                        issue_file=i.file,
                        category=i.category,
                    ),
                    error_message=i.message,
                    suggested_action=self._consistency_fix_action(i.category),
                )
                for i in report.issues
                if i.severity == "error"
            ]
            if consistency_issues:
                client = LLMClient(event_bus=self.event_bus, caller_name="consistency_fix")
                fix_result = await apply_targeted_fix(
                    scaffold_dir=scaffold_dir,
                    issues=consistency_issues,
                    llm_client=client,
                    brief=brief,
                )
                manifest["consistency_fix"] = {
                    "changed": fix_result.files_changed,
                    "created": fix_result.files_created,
                    "errors": fix_result.errors,
                }
                report = await asyncio.to_thread(check_consistency, scaffold_dir, brief)
                manifest["consistency_check"]["post_fix_ok"] = bool(report.ok)
                # Persist now so consistency_fix diagnostics survive a
                # crash in the next stage (otherwise the post-fix state
                # only saves later, and is lost if the run dies first).
                self._save_manifest(artifact_dir, manifest)
            if not report.ok:
                self._append_history(
                    manifest,
                    "CONSISTENCY_CHECK_FAILED",
                    status="running",
                    message=f"{len(report.issues)} consistency issues found after code stage.",
                )

        # ── Frontend build dry-run (proposal #3) ───────────────────────────
        # Shift Class-2 failures (vite build fails inside the final
        # BuildVerifier) from end-of-run into the critique window. We
        # run `vite build` against the frontend dir; if it fails, we
        # feed the error log through apply_targeted_fix so the LLM
        # repairs it now instead of after another reviewer pass.
        await self._run_frontend_build_dryrun(
            manifest=manifest,
            artifact_dir=artifact_dir,
            scaffold_dir=scaffold_dir,
            brief=brief,
        )

    @staticmethod
    def _unresolved_todo_stub_files(issues: List[Any]) -> List[str]:
        files: List[str] = []
        seen: Set[str] = set()
        for issue in issues:
            category = getattr(issue, "category", None)
            severity = getattr(issue, "severity", None)
            file_path = str(getattr(issue, "file", "") or "").strip()
            if category != "todo_stub" or severity != "error" or not file_path or file_path in seen:
                continue
            seen.add(file_path)
            files.append(file_path)
        return files

    @staticmethod
    def _todo_stub_retry_hint(files: List[str]) -> str:
        ordered = [str(path).strip() for path in files if str(path).strip()]
        if not ordered:
            return (
                "Generated scaffold still contains unresolved TODO stubs. "
                "Regenerate those files with real implementations; do not ship placeholders."
            )
        preview = ", ".join(ordered[:5])
        more = f" (+{len(ordered) - 5} more)" if len(ordered) > 5 else ""
        return (
            "Generated scaffold still contains unresolved TODO stubs: "
            f"{preview}{more}. Regenerate those files with real implementations; "
            "do not ship placeholders."
        )

    @staticmethod
    def _remaining_missing_planned_files(
        stage_output: Optional[Dict[str, Any]],
        scaffold_dir: Path,
    ) -> List[str]:
        if not isinstance(stage_output, dict):
            return []
        raw_missing = stage_output.get("missing_files")
        if not isinstance(raw_missing, list):
            return []
        remaining: List[str] = []
        seen: Set[str] = set()
        scaffold_root = scaffold_dir.resolve()
        for raw_path in raw_missing:
            rel_path = str(raw_path or "").lstrip("/").strip()
            if not rel_path or rel_path in seen:
                continue
            seen.add(rel_path)
            target_path = (scaffold_dir / rel_path).resolve()
            try:
                target_path.relative_to(scaffold_root)
            except ValueError:
                continue
            if not target_path.exists():
                remaining.append(rel_path)
        return remaining

    @staticmethod
    def _missing_planned_files_retry_hint(files: List[str]) -> str:
        ordered = [str(path).strip() for path in files if str(path).strip()]
        if not ordered:
            return (
                "Generated scaffold is still missing planned files. "
                "Write the full file set from the scaffold plan before handoff."
            )
        preview = ", ".join(ordered[:5])
        more = f" (+{len(ordered) - 5} more)" if len(ordered) > 5 else ""
        return (
            "Generated scaffold is still missing planned files: "
            f"{preview}{more}. Write the missing files with real implementations "
            "before handoff."
        )

    async def _run_frontend_build_dryrun(
        self,
        *,
        manifest: Dict[str, Any],
        artifact_dir: Path,
        scaffold_dir: Path,
        brief: str,
    ) -> None:
        """Run `vite build` to catch frontend errors before BuildVerifier.

        Skipped when:
          * No frontend exists in this scaffold (server-only / python_cli).
          * No package.json declares vite as a script or devDep (would
            run for ~20s discovering nothing).
          * `npm`/`node` aren't on PATH.
        """
        import shutil as _sh
        if not scaffold_dir.exists():
            return
        # Cheap front-end detection — anchored to the files we'd be
        # building. If there's no index.html plus no JSX/TSX/JS/TS
        # files under src/, there's nothing for vite to build.
        has_index = (scaffold_dir / "index.html").is_file()
        has_src_entry = False
        src_dir = scaffold_dir / "src"
        if src_dir.is_dir():
            for ext in ("*.jsx", "*.tsx", "*.js", "*.ts"):
                if next(src_dir.rglob(ext), None) is not None:
                    has_src_entry = True
                    break
        if not (has_index and has_src_entry):
            return

        pkg_path = scaffold_dir / "package.json"
        if not pkg_path.is_file():
            return
        try:
            pkg_text = pkg_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        if "vite" not in pkg_text:
            return

        npm_bin = _sh.which("npm")
        node_bin = _sh.which("node")
        if not npm_bin or not node_bin:
            return

        # We need node_modules to run vite — but vite is heavy, so cap
        # the install + build at the same total budget as the eventual
        # BuildVerifier so we don't blow the stage timeout here.
        cmd_install = [
            npm_bin, "install", "--no-audit", "--no-fund",
            "--silent", "--prefer-offline",
        ]
        cmd_build = [npm_bin, "run", "build", "--silent"]
        env = os.environ.copy()
        env["CI"] = "1"  # vite/npm: non-interactive

        try:
            install_proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd_install,
                    cwd=str(scaffold_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                ),
                timeout=180.0,
            )
            install_stdout, install_stderr = await asyncio.wait_for(
                install_proc.communicate(), timeout=180.0
            )
        except asyncio.TimeoutError:
            logger.info("frontend build dry-run: npm install timed out — skipping")
            return
        except (OSError, FileNotFoundError):
            return

        if install_proc.returncode != 0:
            stderr_text = install_stderr.decode(errors="replace") if install_stderr else ""
            # Network failure is not a code defect — skip the build phase
            # rather than spuriously failing the run.
            if any(
                phrase in stderr_text
                for phrase in ("ECONNREFUSED", "ENOTFOUND", "network", "unable to connect")
            ):
                logger.info("frontend build dry-run: npm install network error — skipping")
                return
            # Unknown install failure: still skip the build verifier
            # would catch it later anyway.
            logger.info("frontend build dry-run: npm install failed — leaving for BuildVerifier")
            return

        try:
            build_proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd_build,
                    cwd=str(scaffold_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                ),
                timeout=120.0,
            )
            build_stdout, build_stderr = await asyncio.wait_for(
                build_proc.communicate(), timeout=120.0
            )
        except asyncio.TimeoutError:
            logger.info("frontend build dry-run: vite build timed out — skipping")
            return
        except (OSError, FileNotFoundError):
            return

        if build_proc.returncode == 0:
            manifest["frontend_build_dryrun"] = {"ok": True}
            return

        # Build failed. Capture the tail of stderr (most informative
        # part of a vite/rollup error) and feed it through targeted_fix.
        build_stderr_text = build_stderr.decode(errors="replace") if build_stderr else ""
        build_stdout_text = build_stdout.decode(errors="replace") if build_stdout else ""
        error_blob = (build_stderr_text or build_stdout_text or "").strip()
        error_tail = error_blob[-3500:]  # vite error tail is usually self-contained

        manifest["frontend_build_dryrun"] = {
            "ok": False,
            "error_tail": error_tail,
        }
        self._save_manifest(artifact_dir, manifest)
        self._append_history(
            manifest,
            "FRONTEND_BUILD_DRYRUN_FAILED",
            status="running",
            message=(
                error_tail.splitlines()[0]
                if error_tail
                else "vite build failed (no output)"
            ),
        )

        # Hand the build error to targeted_fix so the LLM can repair.
        # We don't know which file is at fault — vite/rollup error
        # text usually names it. Let the LLM parse it.
        from skyn3t.adapters import LLMClient
        from skyn3t.agents.targeted_fix import FileIssue, apply_targeted_fix

        # Try to extract the offending file path from the error tail.
        # Common vite/rollup shapes: "src/foo.jsx:12:5", "error during
        # build: file: src/foo.jsx".
        target_file = self._guess_failed_file(error_tail, scaffold_dir)
        client = LLMClient(event_bus=self.event_bus, caller_name="vite_dryrun_fix")
        issues = [
            FileIssue(
                path=target_file or "src/main.jsx",
                error_message=(
                    "vite build failed during code-stage dry-run. "
                    "Tail of build output:\n\n" + error_tail
                ),
                suggested_action="regenerate",
            )
        ]
        try:
            fix_result = await apply_targeted_fix(
                scaffold_dir=scaffold_dir,
                issues=issues,
                llm_client=client,
                brief=brief,
            )
            manifest["frontend_build_dryrun"]["fix"] = {
                "changed": fix_result.files_changed,
                "created": fix_result.files_created,
                "errors": fix_result.errors,
            }
            self._save_manifest(artifact_dir, manifest)
        except Exception:
            logger.exception("frontend build dry-run targeted fix crashed")

    @staticmethod
    def _guess_failed_file(error_text: str, scaffold_dir: Path) -> Optional[str]:
        """Pull a scaffold-relative file path out of a vite/rollup error tail."""
        if not error_text:
            return None
        # Match common shapes: "src/foo.jsx:12:5" or "/abs/.../scaffold/src/foo.jsx".
        scaffold_str = str(scaffold_dir.resolve())
        for line in error_text.splitlines():
            m = re.search(r"([A-Za-z0-9_\-./]+\.(?:jsx|tsx|js|ts|css|html))(?::\d+)?", line)
            if not m:
                continue
            candidate = m.group(1)
            if candidate.startswith(scaffold_str):
                candidate = candidate[len(scaffold_str):].lstrip("/")
            # Sanity-check that file exists in scaffold
            if (scaffold_dir / candidate).is_file():
                return candidate
        return None

    def _scan_artifacts(self, d: Path) -> List[str]:
        """Return relative paths of files under ``d`` (capped at 200)."""
        if not d.exists():
            return []
        entries: List[str] = []
        for p in sorted(d.rglob("*")):
            if p.is_file() and not any(
                part in self._SCAFFOLD_SHAPE_SKIP for part in p.parts
            ):
                try:
                    rel = p.relative_to(d).as_posix()
                except ValueError:
                    rel = p.name
                entries.append(rel)
                if len(entries) >= 200:
                    break
        return entries

    def _normalize_manifest(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        manifest.setdefault("stages", [])
        manifest.setdefault("artifacts", [])
        manifest.setdefault("history", [])
        manifest.setdefault("status", "queued")
        manifest.setdefault("execution_profile", "balanced")
        manifest["quality_summary"] = self._normalize_quality_summary(
            manifest.get("quality_summary")
        )
        manifest["benchmark"] = self._normalize_benchmark_summary(
            manifest.get("benchmark")
        )
        manifest.setdefault("created_at", manifest.get("started_at") or time.time())
        manifest.setdefault("updated_at", manifest.get("created_at") or time.time())
        manifest.setdefault("current_stage", None)
        manifest.setdefault("current_agent", None)
        manifest.setdefault("next_action", "")
        manifest["mission_setup"] = normalize_mission_setup(manifest.get("mission_setup"))
        manifest["repo_target"] = normalize_repo_target(manifest.get("repo_target"))
        if "workflow_summary" not in manifest:
            template_key = str(manifest.get("template") or "").strip()
            workflow = self._empty_workflow_summary(template_key, manifest.get("title") or "")
            if template_key:
                try:
                    workflow = self._workflow_from_template(
                        get_template(template_key), template_key
                    )
                except Exception:
                    pass
            manifest["workflow_summary"] = workflow
        slug = str(manifest.get("slug") or "").strip()
        artifact_dir = (self.projects_root / slug) if slug else None
        manifest["artifacts"] = self._normalize_stage_files(
            manifest.get("artifacts"),
            artifact_dir=artifact_dir,
        )
        stage_plan_map = self._workflow_stage_map(manifest.get("workflow_summary"))
        manifest["stages"] = [
            self._normalize_stage_record(
                entry,
                stage_plan_map.get(str(entry.get("name") or "")),
                artifact_dir=artifact_dir,
            )
            for entry in manifest.get("stages", [])
            if isinstance(entry, dict)
        ]
        return manifest

    @staticmethod
    def _quality_source_for_stage(stage: Any) -> Optional[str]:
        agent_name = str(getattr(stage, "agent", "") or "").strip().lower()
        if agent_name == "revieweragent":
            return "reviewer"
        if agent_name == "verifieragent":
            return "verifier"
        return None

    @staticmethod
    def _normalize_quality_verdict(source: str, raw_verdict: Any) -> Optional[str]:
        raw = str(raw_verdict or "").strip().lower()
        if not raw:
            return None
        if source == "reviewer":
            return {
                "go": "go",
                "go-with-fixes": "go-with-fixes",
                "no-go": "no-go",
            }.get(raw)
        if source == "verifier":
            return {
                "yes": "go",
                "partial": "go-with-fixes",
                "no": "no-go",
            }.get(raw)
        return None

    def _normalize_quality_summary(self, value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict):
            return None

        source = str(value.get("source") or "").strip().lower()
        if source not in {"reviewer", "verifier"}:
            return None

        raw_verdict = str(value.get("raw_verdict") or "").strip().lower()
        verdict: Optional[str] = str(value.get("verdict") or "").strip().lower()
        if verdict not in {"go", "go-with-fixes", "no-go"}:
            verdict = self._normalize_quality_verdict(source, raw_verdict)
        if verdict not in {"go", "go-with-fixes", "no-go"}:
            return None

        score_value = value.get("score")
        if score_value is None:
            return None
        try:
            score = int(round(float(score_value)))
        except (TypeError, ValueError):
            return None
        score = max(0, min(100, score))

        summary = self._truncate_stage_text(value.get("summary"), limit=240)
        if not summary:
            summary = f"{source.title()} quality pass recorded {verdict} at {score}/100."

        updated_at = value.get("updated_at")
        try:
            updated_at = float(updated_at) if updated_at is not None else None
        except (TypeError, ValueError):
            updated_at = None

        review_file = str(value.get("review_file") or "").strip() or None
        return {
            "source": source,
            "verdict": verdict,
            "raw_verdict": raw_verdict or verdict,
            "score": score,
            "summary": summary,
            "review_file": review_file,
            "updated_at": updated_at,
        }

    # Composite gate threshold — final project outcome is "done" only
    # when every signal lines up. The reviewer's verdict alone isn't
    # enough: v15 scored 100/100 but the program didn't boot until
    # four manual fixes. Now we require boot=yes AND build=yes AND
    # reviewer >= REVIEWER_SCORE_THRESHOLD before declaring done.
    REVIEWER_SCORE_THRESHOLD = 75

    def _finalize_project_outcome(
        self,
        quality_summary: Any,
        *,
        manifest: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, str, Optional[str]]:
        """Determine the final project status from ALL quality signals.

        Composite gate: a run only earns "done" when
          - reviewer verdict is "go" AND reviewer score >= threshold, AND
          - build_verification verdict is "yes" (or "skipped"), AND
          - boot_verification verdict is "yes" (or "skipped").

        Any failure of those three downgrades to "needs_fixes" (when
        recoverable) or "failed" (when irrecoverable). The previous
        version only looked at the reviewer verdict, so a 100/100
        scaffold that didn't boot still got marked "done".
        """
        quality = self._normalize_quality_summary(quality_summary)

        # Build / boot verifier verdicts — pulled from manifest when
        # available. These are independent of the reviewer.
        build_verdict = None
        boot_verdict = None
        if manifest is not None:
            bv = manifest.get("build_verification") or {}
            boot = manifest.get("boot_verification") or {}
            if isinstance(bv, dict):
                build_verdict = bv.get("verdict")
            if isinstance(boot, dict):
                boot_verdict = boot.get("verdict")

        # 1. Hard failures from verifiers — these can't be reviewer-
        # overridden. A program that doesn't boot isn't "done".
        if build_verdict == "no":
            return (
                "needs_fixes",
                "Build verifier rejected the scaffold — see "
                "build_verification.failure_hint for the cross-file issue.",
                "build verifier said no",
            )
        if boot_verdict == "no":
            return (
                "needs_fixes",
                "Server failed to boot — see boot_verification.failure_hint "
                "for the diagnosed cross-file issue.",
                "boot verifier said no",
            )

        # 2. Reviewer-driven outcome, with the score threshold layered on.
        if quality is None:
            # No reviewer ran at all — fall back to verifier signals.
            # If verifiers said yes (or skipped), that's enough for done.
            return "done", "Project finished — open an artifact or download the zip.", None

        verdict = str(quality.get("verdict") or "").strip().lower()
        score_raw = quality.get("score")
        try:
            score = int(score_raw) if score_raw is not None else None
        except (TypeError, ValueError):
            score = None
        summary = self._truncate_stage_text(quality.get("summary"), limit=240)

        # Reviewer says go but score is below threshold → needs fixes.
        # This catches the "100/100 marketing reviewer score on a
        # half-baked scaffold" case AND the inverse (low score even
        # though verdict said go).
        if verdict == "go":
            if score is not None and score < self.REVIEWER_SCORE_THRESHOLD:
                return (
                    "needs_fixes",
                    f"Reviewer said go but score {score}/100 is below "
                    f"the {self.REVIEWER_SCORE_THRESHOLD} threshold for "
                    f"shipping. Review the findings before merging.",
                    None,
                )
            return "done", "Project finished — open an artifact or download the zip.", None

        if verdict == "go-with-fixes":
            return (
                "needs_fixes",
                summary or "Project finished with follow-up fixes still needed.",
                None,
            )
        return (
            "failed",
            summary or "Project finished, but the reviewer marked it as no-go.",
            summary or "Reviewer marked the project as no-go.",
        )

    @staticmethod
    def _quality_source_priority(source: Any) -> int:
        normalized = str(source or "").strip().lower()
        if normalized == "reviewer":
            return 2
        if normalized == "verifier":
            return 1
        return 0

    def _merge_quality_summary(
        self,
        current: Any,
        candidate: Any,
    ) -> Optional[Dict[str, Any]]:
        current_summary = self._normalize_quality_summary(current)
        candidate_summary = self._normalize_quality_summary(candidate)
        if candidate_summary is None:
            return current_summary
        if current_summary is None:
            return candidate_summary

        current_priority = self._quality_source_priority(current_summary.get("source"))
        candidate_priority = self._quality_source_priority(candidate_summary.get("source"))
        if candidate_priority > current_priority:
            return candidate_summary
        if candidate_priority < current_priority:
            return current_summary

        current_updated = float(current_summary.get("updated_at") or 0.0)
        candidate_updated = float(candidate_summary.get("updated_at") or 0.0)
        return candidate_summary if candidate_updated >= current_updated else current_summary

    def _clear_quality_summary(self, manifest: Dict[str, Any]) -> None:
        manifest["quality_summary"] = None

    @staticmethod
    def _normalize_benchmark_summary(value: Any) -> Dict[str, Any]:
        data = value if isinstance(value, dict) else {}
        stage_durations = data.get("stage_durations_ms")
        if not isinstance(stage_durations, dict):
            stage_durations = {}
        cleaned_stage_durations: Dict[str, int] = {}
        for name, ms in stage_durations.items():
            key = str(name or "").strip()
            if not key:
                continue
            try:
                cleaned_stage_durations[key] = max(0, int(ms))
            except (TypeError, ValueError):
                continue
        return {
            "execution_profile": str(data.get("execution_profile") or "balanced"),
            "total_duration_ms": max(0, int(data.get("total_duration_ms") or 0)),
            "stage_durations_ms": cleaned_stage_durations,
            "stage_failures": max(0, int(data.get("stage_failures") or 0)),
            "retry_launched": bool(data.get("retry_launched", False)),
            "final_status": str(data.get("final_status") or ""),
            "updated_at": float(data.get("updated_at") or 0.0),
        }

    def _init_benchmark(self, manifest: Dict[str, Any]) -> None:
        benchmark = self._normalize_benchmark_summary(manifest.get("benchmark"))
        benchmark["execution_profile"] = str(
            manifest.get("execution_profile") or benchmark.get("execution_profile") or "balanced"
        )
        manifest["benchmark"] = benchmark

    def _finalize_benchmark(self, manifest: Dict[str, Any]) -> None:
        benchmark = self._normalize_benchmark_summary(manifest.get("benchmark"))
        stages = manifest.get("stages") or []
        stage_durations: Dict[str, int] = {}
        stage_failures = 0
        total_ms = 0
        for entry in stages:
            if not isinstance(entry, dict):
                continue
            stage_name = str(entry.get("name") or "").strip()
            started = entry.get("started_at")
            completed = entry.get("completed_at")
            if stage_name and started is not None and completed is not None:
                try:
                    duration_ms = max(0, int((float(completed) - float(started)) * 1000))
                    stage_durations[stage_name] = duration_ms
                    total_ms += duration_ms
                except (TypeError, ValueError):
                    pass
            status = self._normalize_stage_status(entry.get("status"), entry.get("ok"))
            if status == "failed":
                stage_failures += 1
        benchmark["execution_profile"] = str(
            manifest.get("execution_profile") or benchmark.get("execution_profile") or "balanced"
        )
        benchmark["stage_durations_ms"] = stage_durations
        benchmark["stage_failures"] = stage_failures
        benchmark["total_duration_ms"] = total_ms
        benchmark["final_status"] = str(manifest.get("status") or "")
        benchmark["retry_launched"] = bool(manifest.get("_retry_slug"))
        benchmark["updated_at"] = time.time()
        manifest["benchmark"] = benchmark

    @staticmethod
    def _infer_execution_profile(brief: str, extra: Optional[dict]) -> str:
        if isinstance(extra, dict):
            override = str(extra.get("execution_profile") or "").strip().lower()
            if override in {"fast", "balanced", "deep"}:
                return override
        text = (brief or "").lower()
        words = [w for w in text.split() if w.strip()]
        integration_cues = (
            "api", "integration", "oauth", "webhook", "docker", "kubernetes",
            "sonarr", "radarr", "plex", "jellyfin", "unifi", "home assistant",
        )
        cue_hits = sum(1 for cue in integration_cues if cue in text)
        if len(words) <= 12 and cue_hits == 0:
            return "fast"
        if len(words) >= 80 or cue_hits >= 2:
            return "deep"
        return "balanced"

    @staticmethod
    def _stage_timeout_for(
        stage_name: str,
        execution_profile: str,
        explicit_timeout: Optional[float],
    ) -> float:
        if explicit_timeout is not None:
            return float(explicit_timeout)
        heavy_stages = {"code", "research", "reviewer", "codeimprover"}
        # consistency_reviewer ran 8 minutes (480s) in observed
        # multi-blocker reviews on the deep profile. 300s base * 1.4 =
        # 420s wasn't enough. Treat it as medium.
        medium_stages = {"designer", "architect", "consistency_reviewer"}
        if stage_name in heavy_stages:
            base = 1800.0
        elif stage_name in medium_stages:
            base = 600.0
        else:
            base = 300.0
        profile = (execution_profile or "balanced").strip().lower()
        if profile == "fast":
            return max(180.0, base * 0.6)
        if profile == "deep":
            return base * 1.4
        return base

    @staticmethod
    def _consistency_fix_target(
        scaffold_dir: Path,
        issue_file: str,
        category: str,
    ) -> str:
        if category != "missing_mount":
            return issue_file
        entry = StudioRunner._server_entry_for(scaffold_dir)
        return entry or issue_file

    @staticmethod
    def _server_entry_for(scaffold_dir: Path) -> Optional[str]:
        candidates = (
            "server/index.js",
            "server/index.ts",
            "server/app.js",
            "server/app.ts",
            "server/main.js",
            "server/main.ts",
        )
        for rel in candidates:
            if (scaffold_dir / rel).exists():
                return rel
        return None

    @staticmethod
    def _normalize_scaffold_issue_path(
        scaffold_dir: Path,
        raw_issue_file: str,
    ) -> Optional[str]:
        """Resolve reviewer/fix-loop file references to a real scaffold path.

        Reviewer critique lines sometimes include section labels or annotations
        after a real filename, e.g. ``src/App.jsx Drawer`` or
        ``src/App.jsx: missing import``. Normalize those back to the concrete
        file so targeted_fix doesn't create bogus placeholders like
        ``src/App.jsx Drawer``.
        """
        cleaned = str(raw_issue_file or "").strip()
        if not cleaned or cleaned in {"unknown", "(unknown)"}:
            return None
        if cleaned.startswith("scaffold/"):
            cleaned = cleaned[len("scaffold/"):]

        exact = scaffold_dir / cleaned
        if exact.exists() and exact.is_file():
            return cleaned

        file_token_match = re.search(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", cleaned)
        if file_token_match:
            token = file_token_match.group(1).lstrip("/")
            if token.startswith("scaffold/"):
                token = token[len("scaffold/"):]
            token_path = scaffold_dir / token
            if token_path.exists() and token_path.is_file():
                return token
            cleaned = token

        common_exts = (
            ".jsx", ".js", ".tsx", ".ts", ".mjs", ".cjs",
            ".py", ".json", ".css", ".scss", ".html", ".md", ".yml", ".yaml",
        )
        if "." not in Path(cleaned).name:
            stem_matches = [
                f"{cleaned}{ext}"
                for ext in common_exts
                if (scaffold_dir / f"{cleaned}{ext}").exists()
                and (scaffold_dir / f"{cleaned}{ext}").is_file()
            ]
            if len(stem_matches) == 1:
                return stem_matches[0]

        basename = Path(cleaned).name
        if not basename:
            return None

        basename_matches: List[str] = []
        if "." in basename:
            basename_matches = [
                p.relative_to(scaffold_dir).as_posix()
                for p in scaffold_dir.rglob(basename)
                if p.is_file()
            ]
        else:
            for ext in common_exts:
                basename_matches.extend(
                    p.relative_to(scaffold_dir).as_posix()
                    for p in scaffold_dir.rglob(f"{basename}{ext}")
                    if p.is_file()
                )
        deduped = sorted(set(basename_matches))
        if len(deduped) == 1:
            return deduped[0]

        return None

    @staticmethod
    def _api_slug_from_route(path: str) -> Optional[str]:
        m = re.match(r"^/api/([A-Za-z0-9_-]+)(?:/|$)", str(path or "").strip())
        if not m:
            return None
        return m.group(1)

    @staticmethod
    def _consistency_fix_action(category: str) -> str:
        if category in {"broken_import", "missing_mount", "todo_stub"}:
            return "regenerate"
        return "create_placeholder"

    @staticmethod
    def _integration_fix_targets(
        scaffold_dir: Path,
        integration_result: Dict[str, Any],
    ) -> List[str]:
        issues = integration_result.get("issues")
        if not isinstance(issues, list):
            return []

        candidates: List[str] = []
        seen: Set[str] = set()
        route_exts = (".js", ".ts", ".mjs", ".cjs")

        def _add(rel: Optional[str]) -> None:
            if not rel or rel in seen:
                return
            seen.add(rel)
            candidates.append(rel)

        for issue in issues:
            if not isinstance(issue, dict) or issue.get("issue") != "missing":
                continue
            frontend_path = str(issue.get("frontend_path") or "")
            backend_match = str(issue.get("backend_match") or "")

            slug = StudioRunner._api_slug_from_route(frontend_path)
            if slug is None and backend_match:
                _, _, backend_path = backend_match.partition(" ")
                slug = StudioRunner._api_slug_from_route(backend_path)
            if not slug:
                continue

            matched_route_file = None
            for ext in route_exts:
                rel = f"server/routes/{slug}{ext}"
                if (scaffold_dir / rel).exists():
                    matched_route_file = rel
                    break
            _add(matched_route_file)

        if not candidates:
            _add(StudioRunner._server_entry_for(scaffold_dir))

        return candidates

    @staticmethod
    def _critique_rounds_for(
        stage_name: str,
        brief: str,
        execution_profile: str = "balanced",
    ) -> int:
        normalized = (stage_name or "").strip().lower()
        if normalized != "code":
            return 2 if (execution_profile or "").strip().lower() == "fast" else 3
        text = (brief or "").lower()
        visual_signals = (
            "dashboard", "frontend", "front end", "landing page", "website",
            "web app", "ui", "ux", "design", "visual", "theme", "tailwind",
        )
        has_visual_signal = any(
            re.search(rf"(?<!\w){re.escape(signal)}(?!\w)", text, re.IGNORECASE)
            for signal in visual_signals
        )
        profile = (execution_profile or "").strip().lower()
        if profile == "fast":
            return 3 if has_visual_signal else 2
        return 4 if has_visual_signal else 3

    @staticmethod
    def _critique_timeout_for(
        stage_name: str,
        execution_profile: str = "balanced",
    ) -> float:
        normalized = (stage_name or "").strip().lower()
        if normalized == "code":
            base = 420.0
        elif normalized in {"designer", "architect", "research", "marketer", "business"}:
            base = 180.0
        else:
            base = 120.0

        profile = (execution_profile or "balanced").strip().lower()
        if profile == "fast":
            return max(60.0, base * 0.6)
        if profile == "deep":
            return base * 1.3
        return base

    @staticmethod
    def _relativize_artifact_path(artifact_dir: Path, value: Any) -> Optional[str]:
        cleaned = str(value or "").strip()
        if not cleaned:
            return None

        path = Path(cleaned)
        if path.is_absolute():
            try:
                return path.resolve().relative_to(artifact_dir.resolve()).as_posix()
            except (OSError, RuntimeError, ValueError):
                return None

        if any(part in {"", ".", ".."} for part in path.parts):
            return None

        parts = path.parts
        slug = artifact_dir.name
        if parts and parts[0] == "projects":
            if len(parts) >= 3 and parts[1] == slug:
                trimmed = Path(*parts[2:]).as_posix()
                return trimmed or None
            return None

        if parts and parts[0] == slug:
            trimmed = Path(*parts[1:]).as_posix()
            return trimmed or None

        return path.as_posix()

    def _extract_quality_candidate(
        self,
        *,
        stage: Any,
        output: Any,
        artifact_dir: Path,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(output, dict):
            return None

        source = self._quality_source_for_stage(stage)
        if not source:
            return None

        raw_verdict = str(output.get("verdict") or "").strip().lower()
        verdict = self._normalize_quality_verdict(source, raw_verdict)
        if verdict is None:
            return None

        score_value = output.get("score")
        if score_value is None:
            return None
        try:
            score = int(round(float(score_value)))
        except (TypeError, ValueError):
            return None
        score = max(0, min(100, score))

        summary = self._truncate_stage_text(output.get("summary"), limit=240)
        if not summary and source == "verifier":
            reasons = output.get("reasons")
            if isinstance(reasons, list):
                summary = self._truncate_stage_text(
                    "; ".join(str(reason) for reason in reasons[:2])
                )

        review_file = None
        if source == "reviewer":
            for file_path in self._normalize_stage_files(output.get("files")):
                if Path(file_path).name.lower() == "review.md":
                    review_file = self._relativize_artifact_path(artifact_dir, file_path)
                    break

        return self._normalize_quality_summary(
            {
                "source": source,
                "verdict": verdict,
                "raw_verdict": raw_verdict,
                "score": score,
                "summary": summary,
                "review_file": review_file,
                "updated_at": time.time(),
            }
        )

    def _save_manifest(self, artifact_dir: Path, manifest: Dict[str, Any]) -> None:
        manifest = self._normalize_manifest(manifest)
        history = manifest.get("history") or []
        if len(history) > 200:
            manifest["history"] = history[-200:]
        manifest["updated_at"] = time.time()
        (artifact_dir / "project.json").write_text(json.dumps(manifest, indent=2))

    def mark_project_failed(
        self,
        slug: str,
        error: str,
        *,
        next_action: str = "Project stopped because the runner crashed.",
    ) -> Optional[Dict[str, Any]]:
        """Persist a visible failed terminal state for an accepted project."""
        artifact_dir = self.projects_root / slug
        manifest_path = artifact_dir / "project.json"
        if not manifest_path.exists():
            return None
        manifest = self._normalize_manifest(json.loads(manifest_path.read_text()))
        history = manifest.get("history") or []
        already_failed = manifest.get("status") == "failed" and (
            bool(history) and history[-1].get("event") == "PROJECT_FAILED"
        )
        manifest["status"] = "failed"
        manifest["error"] = error
        manifest["current_stage"] = None
        manifest["current_agent"] = None
        self._clear_quality_summary(manifest)
        manifest["next_action"] = next_action
        manifest["completed_at"] = manifest.get("completed_at") or time.time()
        manifest["artifacts"] = self._scan_artifacts(artifact_dir)
        failure_message = str(error).splitlines()[0][:240] if error else "Project failed."
        if not already_failed:
            self._append_history(
                manifest,
                "PROJECT_FAILED",
                status="failed",
                message=failure_message,
            )
        self._save_manifest(artifact_dir, manifest)
        if not already_failed:
            self._publish(
                "PROJECT_FAILED",
                {
                    "slug": slug,
                    "error": failure_message,
                    "next_action": next_action,
                },
            )
        return manifest

    def _append_history(
        self,
        manifest: Dict[str, Any],
        event: str,
        *,
        status: Optional[str] = None,
        stage: Optional[str] = None,
        agent: Optional[str] = None,
        message: Optional[str] = None,
        error: Optional[str] = None,
        question_count: Optional[int] = None,
        answer_count: Optional[int] = None,
    ) -> None:
        entry: Dict[str, Any] = {"event": event, "ts": time.time()}
        if status:
            entry["status"] = status
        if stage:
            entry["stage"] = stage
        if agent:
            entry["agent"] = agent
        if message:
            entry["message"] = message
        if error:
            entry["error"] = error
        if question_count is not None:
            entry["question_count"] = question_count
        if answer_count is not None:
            entry["answer_count"] = answer_count
        manifest.setdefault("history", []).append(entry)

    def _workflow_from_template(self, template: Any, template_key: str) -> Dict[str, Any]:
        stage_plans = [self._build_stage_plan(stage) for stage in getattr(template, "stages", [])]
        expected_outputs: List[str] = []
        for stage in stage_plans:
            for output in self._split_expected_outputs(stage.get("expected_artifact", "")):
                if output not in expected_outputs:
                    expected_outputs.append(output)
        agents = [stage["agent"] for stage in stage_plans]
        return {
            "template": template_key,
            "title": getattr(template, "title", template_key),
            "description": getattr(template, "description", ""),
            "stage_count": len(stage_plans),
            "agent_count": len(set(agents)),
            "agents": agents,
            "expected_outputs": expected_outputs,
            "stages": stage_plans,
        }

    @staticmethod
    def _empty_workflow_summary(template_key: str, title: str) -> Dict[str, Any]:
        return {
            "template": template_key,
            "title": title or template_key,
            "description": "",
            "stage_count": 0,
            "agent_count": 0,
            "agents": [],
            "expected_outputs": [],
            "stages": [],
        }

    def _build_stage_plan(self, stage: Any) -> Dict[str, Any]:
        extra = getattr(stage, "input_extra", None) or {}
        expected_artifact = str(
            extra.get("expected_artifact") or self._default_stage_artifact(stage)
        )
        rationale = str(
            extra.get("planned_rationale")
            or self._default_stage_rationale(stage, expected_artifact)
        )
        return {
            "name": getattr(stage, "name", ""),
            "agent": getattr(stage, "agent", ""),
            "capability": getattr(stage, "capability", ""),
            "expected_artifact": expected_artifact,
            "handoff_to": getattr(stage, "handoff_to", None),
            "rationale": rationale,
        }

    @staticmethod
    def _workflow_stage_map(workflow_summary: Any) -> Dict[str, Dict[str, Any]]:
        stages = (workflow_summary or {}).get("stages", []) if isinstance(workflow_summary, dict) else []
        out: Dict[str, Dict[str, Any]] = {}
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            name = str(stage.get("name") or "").strip()
            if name and name not in out:
                out[name] = stage
        return out

    @staticmethod
    def _normalize_stage_status(status: Any, ok: Any) -> str:
        raw = str(status or "").strip().lower()
        aliases = {
            "awaiting_clarification": "waiting",
            "complete": "done",
            "completed": "done",
            "success": "done",
            "succeeded": "done",
        }
        raw = aliases.get(raw, raw)
        if raw in {"pending", "queued", "running", "waiting", "done", "failed", "needs_fixes"}:
            return raw
        if ok is True:
            return "done"
        if ok is False:
            return "failed"
        return "pending"

    def _normalize_stage_files(
        self,
        files_value: Any,
        *,
        artifact_dir: Optional[Path] = None,
    ) -> List[str]:
        if isinstance(files_value, list):
            raw_files = files_value
        elif isinstance(files_value, str):
            raw_files = [files_value]
        else:
            raw_files = []

        files: List[str] = []
        for item in raw_files:
            cleaned = str(item or "").strip()
            if not cleaned:
                continue
            if artifact_dir is not None:
                relativized = self._relativize_artifact_path(artifact_dir, cleaned)
                if relativized is None:
                    continue
                cleaned = relativized
            if cleaned not in files:
                files.append(cleaned)
        return files[:20]

    @staticmethod
    def _truncate_stage_text(value: Any, *, limit: int = 240) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    def _normalize_stage_record(
        self,
        record: Dict[str, Any],
        planned_stage: Optional[Dict[str, Any]] = None,
        artifact_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        planned_stage = planned_stage or {}
        output = record.get("output")
        status = self._normalize_stage_status(record.get("status"), record.get("ok"))
        files = self._normalize_stage_files(record.get("files"), artifact_dir=artifact_dir)
        if not files and isinstance(output, dict):
            files = self._normalize_stage_files(output.get("files"), artifact_dir=artifact_dir)

        summary = self._truncate_stage_text(record.get("summary") or record.get("message"))
        if not summary and isinstance(output, dict):
            summary = self._summarize_stage_output(output)

        error = self._truncate_stage_text(record.get("error"), limit=400)
        if not error and status == "failed" and isinstance(output, dict):
            error = self._truncate_stage_text(output.get("reason"), limit=400)
        if not error and status == "failed":
            error = summary

        next_action = self._truncate_stage_text(record.get("next_action"))
        if not next_action:
            next_action = summary or error

        if not summary:
            if status == "running":
                summary = next_action or "Stage in progress."
            elif status == "waiting":
                summary = next_action or "Waiting for clarification."
            elif status == "failed":
                summary = error or "Stage failed."

        question_count = record.get("question_count")
        if question_count is None and isinstance(output, dict):
            questions = output.get("questions")
            if isinstance(questions, list):
                question_count = len(questions)

        ok = record.get("ok")
        if ok is None:
            if status == "done":
                ok = True
            elif status == "failed":
                ok = False

        normalized: Dict[str, Any] = {
            "name": str(record.get("name") or planned_stage.get("name") or ""),
            "agent": str(record.get("agent") or planned_stage.get("agent") or ""),
            "capability": str(record.get("capability") or planned_stage.get("capability") or ""),
            "expected_artifact": str(
                record.get("expected_artifact") or planned_stage.get("expected_artifact") or ""
            ),
            "handoff_to": record.get("handoff_to") or planned_stage.get("handoff_to"),
            "rationale": str(record.get("rationale") or planned_stage.get("rationale") or ""),
            "status": status,
            "ok": ok,
            "task_id": record.get("task_id"),
            "started_at": record.get("started_at"),
            "completed_at": record.get("completed_at"),
            "summary": summary,
            "files": files,
            "error": error,
            "next_action": next_action,
        }
        if question_count is not None:
            try:
                normalized["question_count"] = int(question_count)
            except (TypeError, ValueError):
                pass
        return normalized

    @staticmethod
    def _find_stage_record_index(
        stages: List[Dict[str, Any]], stage_name: str, agent_name: str
    ) -> int:
        for idx, entry in enumerate(stages):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name") or "") != stage_name:
                continue
            entry_agent = str(entry.get("agent") or "")
            if entry_agent and agent_name and entry_agent != agent_name:
                continue
            return idx
        return -1

    def _set_stage_record(
        self,
        manifest: Dict[str, Any],
        stage: Any,
        *,
        status: str,
        started_at: Optional[float] = None,
        completed_at: Optional[float] = None,
        task_id: Optional[str] = None,
        summary: Optional[str] = None,
        files: Optional[List[str]] = None,
        error: Optional[str] = None,
        next_action: Optional[str] = None,
        question_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        stage_plan = self._build_stage_plan(stage)
        stage_name = str(stage_plan.get("name") or "")
        agent_name = str(stage_plan.get("agent") or "")
        stages = manifest.setdefault("stages", [])
        idx = self._find_stage_record_index(stages, stage_name, agent_name)
        existing = stages[idx] if idx >= 0 and isinstance(stages[idx], dict) else {}

        record: Dict[str, Any] = {**existing, **stage_plan, "status": status}
        if started_at is not None:
            record["started_at"] = started_at
        elif not record.get("started_at"):
            record["started_at"] = time.time()

        if completed_at is not None:
            record["completed_at"] = completed_at
        elif status == "running":
            record["completed_at"] = None
        if task_id is not None:
            record["task_id"] = task_id

        if summary is not None:
            record["summary"] = summary
        if files is not None:
            record["files"] = files
        if next_action is not None:
            record["next_action"] = next_action

        if status == "failed":
            record["error"] = error or record.get("error") or record.get("summary") or ""
            record["ok"] = False
        else:
            record.pop("error", None)
            if status == "done":
                record["ok"] = True
            elif status in {"running", "waiting", "pending"}:
                record.setdefault("ok", None)

        if question_count is not None:
            record["question_count"] = question_count
        elif status != "waiting":
            record.pop("question_count", None)

        normalized = self._normalize_stage_record(record, stage_plan)
        if idx >= 0:
            stages[idx] = normalized
        else:
            stages.append(normalized)
        return normalized

    def _default_stage_artifact(self, stage: Any) -> str:
        extra = getattr(stage, "input_extra", None) or {}
        expected = str(extra.get("expected_artifact") or "").strip()
        if expected:
            return expected
        writer_kind = str(extra.get("kind") or "").strip()
        if getattr(stage, "agent", "") == "WriterAgent" and writer_kind:
            return f"{writer_kind}.md"
        try:
            from skyn3t.studio.planner import AGENT_CATALOG

            for entry in AGENT_CATALOG:
                if entry.get("agent") == getattr(stage, "agent", ""):
                    return str(entry.get("artifact") or "")
        except Exception:
            pass
        return ""

    @staticmethod
    def _default_stage_rationale(stage: Any, expected_artifact: str) -> str:
        capability = getattr(stage, "capability", "general")
        agent = getattr(stage, "agent", "Agent")
        if expected_artifact:
            return (
                f"{agent} handles the {capability} step and is expected to produce "
                f"{expected_artifact}."
            )
        return f"{agent} handles the {capability} step for this mission."

    @staticmethod
    def _split_expected_outputs(expected_artifact: str) -> List[str]:
        outputs: List[str] = []
        for piece in str(expected_artifact or "").split(","):
            cleaned = piece.strip().strip("()")
            if cleaned and cleaned not in outputs:
                outputs.append(cleaned)
        return outputs

    @staticmethod
    def _summarize_stage_output(output: Any) -> str:
        if not isinstance(output, dict):
            return ""
        summary = str(output.get("summary") or "").strip()
        if summary:
            return summary[:240]
        files = output.get("files")
        if isinstance(files, list) and files:
            return f"Produced {len(files)} file{'s' if len(files) != 1 else ''}."
        reason = str(output.get("reason") or "").strip()
        if reason:
            return reason[:240]
        return ""

    async def _maybe_auto_retry(self, manifest: Dict[str, Any], brief: str, slug: str) -> None:
        """If a project failed and hasn't already been retried, launch a second
        attempt with the dynamic "auto" planner and the failure context as a
        lesson. Designed to be a small, observable nudge — not an infinite
        retry loop. Capped at one retry per project.

        The retry runs as a background task so the original `start()` returns
        immediately and the dashboard sees PROJECT_COMPLETED with status=failed
        first, then a PROJECT_RETRY_LAUNCHED event, then a fresh project.
        """
        # Cap on retry depth — derived from the slug, not the manifest,
        # because each retry creates a fresh manifest with _retry_count=0.
        # Slug suffix is the ground truth: "foo-retry-retry-retry" already
        # tried 3 times; another retry would be the 4th.
        retry_suffix_count = (slug or "").count("-retry")
        if retry_suffix_count >= 1 or manifest.get("_retry_count", 0) >= 1:
            return  # already retried once; don't loop
        # Don't retry trivially-bad inputs (empty brief, etc) where retry won't help.
        if not brief or not brief.strip():
            return
        original_template = manifest.get("template", "auto")
        error_summary = (manifest.get("error") or manifest.get("next_action") or "")[:500]
        failed_stages = [
            s.get("name") for s in manifest.get("stages", [])
            if isinstance(s, dict) and self._normalize_stage_status(
                s.get("status"), s.get("ok")
            ) == "failed"
        ]
        # Build the augmented brief — the original brief plus a "previous
        # attempt failed because X" hint that the planner will treat as a
        # constraint when picking a new shape. If BuildVerifier left a
        # `_retry_hint` (compact tail of build log), prefer that as the
        # lesson — it's much more actionable than a stage name.
        build_hint = manifest.get("_retry_hint") or ""
        # Cross-model debate: if a build_verification record knows which
        # backend produced the failing scaffold, suggest a different one to
        # the retry's brief so the next attempt naturally falls through to
        # a sibling model. The auto chain (claude_cli → copilot_cli →
        # openai_cli → kimi_cli) gives a fresh perspective for free.
        prior_backend = ""
        bv = manifest.get("build_verification") or {}
        try:
            prior_backend = (bv.get("backend") or manifest.get("_used_backend") or "").strip()
        except Exception:
            prior_backend = ""
        debate_note = ""
        if prior_backend:
            debate_note = (
                f"\n\nPrior attempt was generated by '{prior_backend}'. "
                f"For the retry, prefer a DIFFERENT model so a second "
                f"perspective gets a shot at the problem."
            )
        if build_hint:
            lesson_block = (
                "\n\nPrior attempt failed during build verification. "
                "Use this as a constraint when picking the next shape:\n"
                f"{build_hint}\n"
                "Fix the specific error above. "
                "Keep the same stack and file structure unless the error is "
                "fundamentally unfixable in this ecosystem."
                f"{debate_note}"
            )
        else:
            lesson_block = (
                f"\n\nPrior attempt ({original_template}) failed at stages "
                f"{failed_stages or '?'}: {error_summary}\n"
                f"Try a different shape — pick alternative agents or a simpler stack."
                f"{debate_note}"
            )
        retry_brief = (brief or "").rstrip() + lesson_block
        retry_slug = f"{slug}-retry"
        try:
            self._publish(
                "PROJECT_RETRY_LAUNCHED",
                {
                    "slug": slug,
                    "retry_slug": retry_slug,
                    "original_template": original_template,
                    "error_summary": error_summary,
                },
            )
            manifest["_retry_count"] = 1
            manifest["_retry_slug"] = retry_slug
            artifact_dir = self.projects_root / slug
            try:
                self._save_manifest(artifact_dir, manifest)
            except Exception:
                # If this write fails we could double-retry on next
                # restart (no `_retry_count` on disk → auto-retry path
                # would fire again).
                logger.warning(
                    "retry-state manifest write FAILED for slug=%s — "
                    "auto-retry may fire twice on restart",
                    slug, exc_info=True,
                )
            # Run retry as a fire-and-forget background task with strong ref
            # so it can't be GC'd.
            task = asyncio.create_task(
                self.start(
                    "auto",
                    retry_brief,
                    slug=retry_slug,
                    mission_setup=manifest.get("mission_setup"),
                    repo_target=manifest.get("repo_target"),
                )
            )
            self._retry_tasks.add(task)
            task.add_done_callback(self._retry_tasks.discard)
        except Exception:
            logger.exception("auto-retry launch failed for slug=%s", slug)

    def _publish(self, name: str, payload: dict) -> None:
        """Publish a project event onto the shared event bus.

        We ride the generic ``SYSTEM_ALERT`` event type so no new
        ``EventType`` member is required - the dashboard distinguishes
        project events via the ``kind`` field on the payload.
        """
        try:
            from skyn3t.core.events import Event, EventType

            normalized_payload = {
                "kind": name,
                "project_slug": payload.get("project_slug") or payload.get("slug"),
                **payload,
            }
            self.event_bus.publish(
                Event(
                    event_type=EventType.SYSTEM_ALERT,
                    source="studio",
                    payload=normalized_payload,
                )
            )
        except Exception:
            pass
