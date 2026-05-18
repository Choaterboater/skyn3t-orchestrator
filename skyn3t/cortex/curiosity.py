"""Continuous-curiosity loop.

Runs in the background. Wakes on a cadence (default every 5 min). When the
swarm is idle, picks a low-cost autonomous action:

  - every 6h: ExplorerAgent.execute() — propose new repos to ingest
  - every 12h: GitHubIngestorAgent — refresh seed repos
  - every 7d: DocsIngestorAgent — refresh provider docs

When the swarm is busy (running tasks > 0), skip and try later. Hard daily
budget so we don't burn quota.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("skyn3t.cortex.curiosity")

STATE_FILE = Path("data/curiosity_state.json")


@dataclass
class CuriosityState:
    last_explorer: float = 0.0      # epoch ts
    last_github_ingest: float = 0.0
    last_docs_refresh: float = 0.0
    daily_actions: int = 0
    today_date: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"last_explorer": self.last_explorer,
                "last_github_ingest": self.last_github_ingest,
                "last_docs_refresh": self.last_docs_refresh,
                "daily_actions": self.daily_actions,
                "today_date": self.today_date}


class CuriosityLoop:
    """Autonomous self-improvement scheduler."""

    EXPLORER_INTERVAL = 6 * 3600          # 6h
    GITHUB_INGEST_INTERVAL = 12 * 3600    # 12h
    DOCS_REFRESH_INTERVAL = 7 * 86400     # weekly
    TICK_INTERVAL = 300                    # check every 5 min
    DAILY_ACTION_CAP = 6
    IDLE_REQUIRED_SECONDS = 60            # only act when busy=0 for >60s

    def __init__(self, *, orchestrator, event_bus):
        self.orchestrator = orchestrator
        self.event_bus = event_bus
        self.state = self._load_state()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def _load_state(self) -> CuriosityState:
        try:
            if STATE_FILE.exists():
                d = json.loads(STATE_FILE.read_text())
                return CuriosityState(**d)
        except Exception:
            pass
        return CuriosityState()

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self.state.to_dict(), indent=2))
        except Exception:
            logger.exception("save state failed")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        # First tick after a short warmup
        await asyncio.sleep(60)
        last_idle_since = time.time()
        while self._running:
            try:
                # Reset daily counter
                today = time.strftime("%Y-%m-%d")
                if self.state.today_date != today:
                    self.state.today_date = today
                    self.state.daily_actions = 0

                if self.state.daily_actions >= self.DAILY_ACTION_CAP:
                    await asyncio.sleep(self.TICK_INTERVAL)
                    continue

                # Idle check
                running = len(getattr(self.orchestrator, "running_tasks", {}) or {})
                if running > 0:
                    last_idle_since = time.time()
                    await asyncio.sleep(self.TICK_INTERVAL)
                    continue
                if (time.time() - last_idle_since) < self.IDLE_REQUIRED_SECONDS:
                    await asyncio.sleep(self.TICK_INTERVAL)
                    continue

                # Pick the most-overdue scheduled task
                action = self._pick_action()
                if action is None:
                    await asyncio.sleep(self.TICK_INTERVAL)
                    continue

                logger.info("curiosity loop firing: %s", action)
                ok = await self._fire(action)
                if ok:
                    self.state.daily_actions += 1
                    setattr(self.state, f"last_{action}", time.time())
                    self._save_state()
                await asyncio.sleep(self.TICK_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("curiosity loop error")
                await asyncio.sleep(self.TICK_INTERVAL)

    def _pick_action(self) -> Optional[str]:
        now = time.time()
        candidates = []
        if now - self.state.last_explorer >= self.EXPLORER_INTERVAL:
            candidates.append(("explorer", now - self.state.last_explorer - self.EXPLORER_INTERVAL))
        if now - self.state.last_github_ingest >= self.GITHUB_INGEST_INTERVAL:
            candidates.append(("github_ingest", now - self.state.last_github_ingest - self.GITHUB_INGEST_INTERVAL))
        if now - self.state.last_docs_refresh >= self.DOCS_REFRESH_INTERVAL:
            candidates.append(("docs_refresh", now - self.state.last_docs_refresh - self.DOCS_REFRESH_INTERVAL))
        if not candidates:
            return None
        candidates.sort(key=lambda x: -x[1])  # most-overdue first
        return candidates[0][0]

    async def _fire(self, action: str) -> bool:
        from skyn3t.core.agent import TaskRequest
        try:
            agent_name = {"explorer": "explorer",
                          "github_ingest": "github_ingestor",
                          "docs_refresh": "docs_ingestor"}.get(action)
            if not agent_name:
                return False
            agent = self.orchestrator.agents.get(agent_name)
            if agent is None:
                logger.warning("curiosity: agent %s not registered", agent_name)
                return False
            req_input = {"mode": "gap_scan"} if action == "explorer" else \
                        {"mode": "seed_list", "max_files": 10} if action == "github_ingest" else \
                        {}  # docs_ingestor takes no params
            req = TaskRequest(title=f"curiosity:{action}", input_data=req_input)
            # Run with a generous timeout but don't block forever
            await asyncio.wait_for(agent.execute(req), timeout=600)
            try:
                from skyn3t.core.events import Event, EventType
                self.event_bus.publish(Event(
                    event_type=EventType.SYSTEM_ALERT, source="curiosity",
                    payload={"kind": "CURIOSITY_FIRED", "action": action}))
            except Exception:
                pass
            return True
        except asyncio.TimeoutError:
            logger.warning("curiosity %s timed out", action)
            return False
        except Exception:
            logger.exception("curiosity %s failed", action)
            return False
