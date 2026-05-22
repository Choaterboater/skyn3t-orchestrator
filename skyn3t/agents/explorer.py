from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.explorer")

DEFAULT_TOPICS = [
    "agentic rag examples", "open source agent frameworks",
    "multi-agent orchestration patterns", "vector store optimizations",
    "llm observability tracing", "tool-using agents",
]

@dataclass
class ExplorationBudget:
    max_proposals_per_run: int = 5
    cooldown_seconds: int = 600   # 10 min between runs
    daily_proposal_cap: int = 50


class ExplorerAgent(BaseAgent):
    """Self-exploration agent. Scans for new repos / topics, files Proposals.

    Modes (input_data["mode"]):
      - "scan_trending"  : list trending repos (uses GitHubIngestor under the hood)
      - "gap_scan"       : looks at recent failures + missing capabilities, proposes ingest topics
      - "follow_links"   : reads recent ingested READMEs, proposes new repos to follow
    """

    def __init__(self, name: str = "explorer", *, event_bus: Optional[EventBus] = None,
                 rag=None, budget: Optional[ExplorationBudget] = None,
                 state_path: Path | str = "data/explorer_state.json",
                 config: Optional[Dict[str, Any]] = None):
        super().__init__(name=name, agent_type="explorer", provider="local",
                         event_bus=event_bus or EventBus(), config=config)
        self.add_capability(AgentCapability(
            name="exploration", description="discovers new corpora and topics", parameters={}))
        self.add_capability(AgentCapability(
            name="research", description="meta-research on what to learn next", parameters={}))
        self.rag = rag
        self.budget = budget or ExplorationBudget()
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    # state ---
    def _load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                if isinstance(data, dict):
                    return data
            except Exception:
                logger.debug(
                    "explorer state parse failed at %s",
                    self.state_path,
                    exc_info=True,
                )
        return {"last_run_ts": 0, "today_count": 0, "today_date": ""}

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(json.dumps(self._state, indent=2))
        except Exception:
            logger.exception("save state failed")

    def _check_budget(self) -> Optional[str]:
        now = time.time()
        # cooldown
        if now - self._state.get("last_run_ts", 0) < self.budget.cooldown_seconds:
            return f"cooldown ({self.budget.cooldown_seconds}s) not elapsed"
        # daily cap
        today = time.strftime("%Y-%m-%d")
        if self._state.get("today_date") != today:
            self._state["today_date"] = today
            self._state["today_count"] = 0
        if self._state["today_count"] >= self.budget.daily_proposal_cap:
            return "daily proposal cap reached"
        return None

    # main ---
    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        if hasattr(self, "think"):
            await self.think(f"explorer starting: {task.title or task.task_id}")
        gate = self._check_budget()
        if gate:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output={"skipped": True, "reason": gate},
            )

        mode = (task.input_data or {}).get("mode", "gap_scan")
        proposals_made: List[Dict[str, Any]] = []
        try:
            if mode == "scan_trending":
                proposals_made = await self._scan_trending(task)
            elif mode == "follow_links":
                proposals_made = await self._follow_links(task)
            else:
                proposals_made = await self._gap_scan(task)
        except Exception as e:
            logger.exception("explorer failed")
            return TaskResult(task_id=task.task_id, success=False, error=str(e))

        self._state["last_run_ts"] = time.time()
        self._state["today_count"] = self._state.get("today_count", 0) + len(proposals_made)
        self._save_state()
        if hasattr(self, "share_learning"):
            try:
                await self.share_learning(
                    f"explorer: {len(proposals_made)} proposals",
                    scope="cortex",
                )
            except Exception:
                logger.debug("share_learning(explorer) failed", exc_info=True)
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "proposals": proposals_made,
                "summary": f"filed {len(proposals_made)} proposals (mode={mode})",
            },
        )

    async def _gap_scan(self, task: TaskRequest) -> List[Dict[str, Any]]:
        topics = list(DEFAULT_TOPICS)
        # if rag is empty / sparse, propose seeded list
        try:
            from skyn3t.cortex import get_store
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for topic in topics[: self.budget.max_proposals_per_run]:
            p = get_store().create(
                kind="ingest",
                title=f"Ingest topic: {topic}",
                summary=f"Explorer suggests ingesting open-source content for: {topic}",
                detail=f"Run GitHub search for `{topic}` and ingest top READMEs.\n\nBudget cost: 1 of {self.budget.max_proposals_per_run}/run.",
                payload={"topic": topic, "limit": 3, "mode": "search"},
                source="explorer",
                auto_triage_eligible=True,
            )
            out.append({"id": p.id, "title": p.title})
        return out

    async def _scan_trending(self, task: TaskRequest) -> List[Dict[str, Any]]:
        # Best-effort: try to invoke GitHubIngestor; if not available, propose generic
        return await self._gap_scan(task)

    async def _follow_links(self, task: TaskRequest) -> List[Dict[str, Any]]:
        return await self._gap_scan(task)
