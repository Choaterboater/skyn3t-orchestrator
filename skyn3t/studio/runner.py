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
import re
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.config.settings import get_settings
from skyn3t.core.agent import AgentCapability, TaskRequest
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
                        pass
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
            effective_brief = augment_brief_with_repo_target(
                augment_brief_with_mission_setup(brief, setup),
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
            effective_brief = augment_brief_with_repo_target(
                augment_brief_with_mission_setup(brief, setup),
                repo,
            )
            manifest["template"] = template_key
            manifest["title"] = template.title
            manifest["brief"] = brief
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
                        **(extra or {}),
                    },
                )
        except Exception as exc:
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
        try:
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

            for stage in template.stages:
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
                    _stage_to = (extra or {}).get("stage_timeout") if isinstance(extra, dict) else None
                    try:
                        _stage_to = float(_stage_to) if _stage_to else None
                    except Exception:
                        _stage_to = None
                    if _stage_to is None:
                        # Per-stage defaults. Code and research are the
                        # heavy-LLM stages — code does N per-file calls
                        # with the full prior-artifact context (research +
                        # arch + design = ~30KB per call) and 5+ files,
                        # research can do real web search via MCP. Both
                        # routinely exceed the old flat 300s cap. Other
                        # stages stay at 300s; nothing else should need
                        # more than a single big LLM call.
                        heavy_stages = {"code", "research"}
                        _stage_to = 1800.0 if stage.name in heavy_stages else 300.0
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
                    stage_error = f"stage timeout (>{int(_stage_to)}s)"
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
                        message=f"Stage timeout (>{int(_stage_to)}s).",
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
                                shape = sorted(
                                    p.relative_to(scaffold_dir).as_posix()
                                    for p in scaffold_dir.rglob("*")
                                    if p.is_file() and "__pycache__" not in p.parts
                                )
                            get_default_scoreboard().record(stack, shape, "no")
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
                                shape = sorted(
                                    p.relative_to(scaffold_dir).as_posix()
                                    for p in scaffold_dir.rglob("*")
                                    if p.is_file() and "__pycache__" not in p.parts
                                )
                            get_default_scoreboard().record(stack, shape, "no")
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
                    self._finalize_project_outcome(manifest.get("quality_summary"))
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
            # "Docs-only" failure detection: if the user's brief implied code
            # work but every artifact produced was markdown/text, the project
            # ran the wrong shape. Mark it failed so the auto-retry hook
            # spawns a new attempt with a forced code stage. Skip this check
            # when the brief was clearly docs-oriented (write/draft/produce
            # docs-noun) — those projects are expected to produce only docs.
            if manifest["status"] in {"done", "needs_fixes"}:
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
                    # If a scaffold dir was produced, run BuildVerifier as a
                    # post-stage gate. A scaffold that doesn't compile / parse
                    # is a failure even if every stage reported success — the
                    # user wants programs that RUN, not just files that exist.
                    # Verifier failure surfaces a `failure_hint` we attach to
                    # the manifest so the auto-retry can inject it as a lesson.
                    scaffold_dir = artifact_dir / "scaffold"
                    if scaffold_dir.exists() and scaffold_dir.is_dir():
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
                            # On failure: try a surgical in-place fix loop
                            # BEFORE falling back to a full-pipeline retry.
                            # This is the cheap path — only re-generates
                            # broken files, keeps the rest of the scaffold.
                            # Up to FIX_ATTEMPTS rounds; each round re-runs
                            # BuildVerifier and either declares success or
                            # collects a fresher failure hint.
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
                                # Re-verify after the fix attempt.
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
                                    # Persist the fix as a learned skill —
                                    # "this kind of build failure was solved
                                    # this way." Next time the same stack
                                    # sees a similar error tail, the skill
                                    # is already in the system prompt.
                                    try:
                                        self._persist_fix_as_skill(
                                            stack=(build_result or {}).get("stack") or "unknown",
                                            fix_round=attempt,
                                            prior_summary=manifest.get("build_verification", {}).get("summary"),
                                        )
                                    except Exception:
                                        logger.exception("persist fix-as-skill failed")
                                    break
                            # Still failing after the fix loop → mark failed
                            # so PR #2's auto-retry can take a different shape.
                            if verdict == "no":
                                manifest["status"] = "failed"
                                manifest["error"] = (
                                    build_result.get("summary")
                                    or "Build verifier rejected the scaffold."
                                )
                                manifest["next_action"] = (
                                    "Retrying with the build failure as a hint."
                                )
                                manifest["_retry_hint"] = (
                                    build_result.get("failure_hint") or ""
                                )
                            # Record the (stack, shape, verdict) outcome in
                            # the build-pattern scoreboard regardless of
                            # success/fail — the meta-agent reads this to
                            # spot which shapes correlate with success so
                            # future scaffolds can be biased toward them.
                            try:
                                from skyn3t.intelligence.build_patterns import (
                                    get_default_scoreboard,
                                )
                                stack = (build_result or {}).get("stack") or "unknown"
                                shape = sorted(
                                    p.relative_to(scaffold_dir).as_posix()
                                    for p in scaffold_dir.rglob("*")
                                    if p.is_file() and "__pycache__" not in p.parts
                                )
                                get_default_scoreboard().record(stack, shape, verdict)
                            except Exception:
                                logger.exception("build_pattern record failed")
            completion_message = (
                manifest["next_action"]
                if manifest.get("status") in {"done", "needs_fixes"}
                else manifest.get("error") or manifest["next_action"]
            )
            self._append_history(
                manifest,
                "PROJECT_COMPLETED",
                status=manifest["status"],
                message=completion_message,
            )
            self._save_manifest(artifact_dir, manifest)
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
                },
            )
            # Auto-retry hook: if this attempt failed and we haven't already
            # retried, launch a second attempt with the dynamic "auto" planner
            # and inject the failure context as a lesson. The retry runs as a
            # background task so the original call returns immediately.
            if manifest.get("status") == "failed":
                await self._maybe_auto_retry(manifest, brief, slug)
            return manifest
        except Exception as exc:
            # The runner itself crashed (agent init blew up, get_agent raised, etc).
            # Without this guard the manifest stays at {stages: [], completed_at: None}
            # and the user sees a project that ran 0 stages with no error.
            logger.exception("studio runner failed for slug=%s", slug)
            manifest["status"] = "failed"
            manifest["error"] = (
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}"
            )
            manifest["completed_at"] = time.time()
            manifest["current_stage"] = None
            manifest["current_agent"] = None
            self._clear_quality_summary(manifest)
            manifest["next_action"] = "Project stopped because the runner crashed."
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
                pass
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
        """Surgical fix: ask the LLM to rewrite ONLY the files that look
        broken given the verifier's stderr. Returns True if at least one
        file was rewritten (so the caller knows to re-verify).

        This is the cheap retry shape — closer to how Paperclip / OpenCLAw
        iterate. We don't burn a fresh pipeline; we just edit the scaffold
        in place and re-run the gate.
        """
        from skyn3t.adapters import LLMClient
        import re as _re

        stderr = (build_result.get("stderr") or "")
        stdout = (build_result.get("stdout") or "")
        stack = build_result.get("stack") or "unknown"
        log_tail = (stderr or stdout).strip()
        if not log_tail:
            return False

        # Map current scaffold files → (relpath, content); cap individual
        # bodies so a giant file doesn't blow the prompt budget.
        files_on_disk: List[tuple[str, str]] = []
        for p in sorted(scaffold_dir.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
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
                prompt, system=system, max_tokens=8000, temperature=0.2,
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

    def _scan_artifacts(self, d: Path) -> List[str]:
        """Return relative paths of files under ``d`` (capped at 200)."""
        if not d.exists():
            return []
        entries: List[str] = []
        for p in sorted(d.rglob("*")):
            if p.is_file():
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
        manifest["quality_summary"] = self._normalize_quality_summary(
            manifest.get("quality_summary")
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

    def _finalize_project_outcome(
        self,
        quality_summary: Any,
    ) -> tuple[str, str, Optional[str]]:
        quality = self._normalize_quality_summary(quality_summary)
        if quality is None:
            return "done", "Project finished — open an artifact or download the zip.", None

        verdict = str(quality.get("verdict") or "").strip().lower()
        summary = self._truncate_stage_text(quality.get("summary"), limit=240)
        if verdict == "go":
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
        if manifest.get("_retry_count", 0) >= 1:
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
                "Try a different stack, or fix the specific error above."
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
                pass
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
            self._retry_tasks = getattr(self, "_retry_tasks", set())
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
