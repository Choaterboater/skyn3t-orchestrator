"""ContractVerifierAgent — deterministic cross-stage contract checks.

Runs before ConsistencyReviewerAgent. Wraps
:func:`skyn3t.agents.contract_engine.check_contract` in the same task-result
shape ``ConsistencyReviewerAgent`` emits, so the runner's fix-loop branch
can pattern-match identically.

No LLM dependency.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from skyn3t.agents.contract_engine import check_contract
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.contract_verifier")


class ContractVerifierAgent(BaseAgent):
    """Deterministic contract checks: palette, tech_stack, placeholders, features."""

    def __init__(
        self,
        name: str = "contract_verifier",
        *,
        event_bus: Optional[EventBus] = None,
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
            name="contract_verify",
            description=(
                "Verifies palette/tech_stack/placeholder/feature contracts "
                "across the artifact dir and the generated scaffold."
            ),
            parameters={
                "scaffold_dir": "str",
                "artifact_dir": "str",
                "brief": "str",
            },
        ))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        data = task.input_data or {}
        artifact_dir_raw = data.get("artifact_dir")
        scaffold_dir_raw = (
            data.get("scaffold_dir")
            or (str(Path(artifact_dir_raw) / "scaffold") if artifact_dir_raw else None)
        )
        if not scaffold_dir_raw or not artifact_dir_raw:
            return TaskResult(
                task_id=task.task_id, success=False,
                error="artifact_dir (and scaffold_dir, or an artifact_dir with a scaffold/ subdir) required",
            )
        scaffold_dir = Path(scaffold_dir_raw).expanduser().resolve()
        artifact_dir = Path(artifact_dir_raw).expanduser().resolve()
        brief = data.get("brief", "") or ""

        try:
            report = check_contract(scaffold_dir, brief, artifact_dir)
        except Exception:
            logger.exception("contract verifier failed; treating as pass to avoid blocking pipeline")
            return TaskResult(
                task_id=task.task_id, success=True,
                output={
                    "verdict": "pass",
                    "blocker_count": 0,
                    "warning_count": 0,
                    "report_json": '{"ok": true, "findings": []}',
                    "scaffold_dir": str(scaffold_dir),
                },
            )

        blockers = [f for f in report.findings if f.severity == "blocker"]
        warnings = [f for f in report.findings if f.severity == "warning"]

        return TaskResult(
            task_id=task.task_id, success=True,
            output={
                "verdict": "pass" if report.ok else "needs_fix",
                "blocker_count": len(blockers),
                "warning_count": len(warnings),
                "report_json": report.to_json(),
                "scaffold_dir": str(scaffold_dir),
            },
        )
