"""Trajectory logger — captures agent execution traces for dataset export.

Subscribes to the event bus and writes structured trajectory records to
JSONL files under ``data/trajectories/``.  Each trajectory covers one
task from start to finish, including LLM calls, tool executions, and
outcomes.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from skyn3t.config.settings import get_settings
from skyn3t.core.events import Event, EventBus, EventType

logger = logging.getLogger("skyn3t.observability.trajectory_logger")


class TrajectoryLogger:
    """Capture task-level trajectories and write them as JSONL.

    Usage::

        logger = TrajectoryLogger()
        logger.subscribe(event_bus)
        # ... run the system ...
        logger.export_jsonl(Path("out.jsonl"))
    """

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        self._output_dir = output_dir or self._default_dir()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # task_id -> trajectory dict (in-flight)
        self._active: Dict[str, Dict[str, Any]] = {}
        # session_id -> user-facing metadata
        self._session_meta: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def subscribe(self, event_bus: EventBus) -> None:
        """Wire into the event bus."""
        event_bus.subscribe(self._on_task_started, EventType.TASK_STARTED)
        event_bus.subscribe(self._on_task_completed, EventType.TASK_COMPLETED)
        event_bus.subscribe(self._on_task_failed, EventType.TASK_FAILED)
        event_bus.subscribe(self._on_llm_exchange, EventType.LLM_EXCHANGE)
        event_bus.subscribe(self._on_agent_thought, EventType.AGENT_THOUGHT)
        event_bus.subscribe(self._on_pipeline_started, EventType.PIPELINE_STARTED)
        logger.info("TrajectoryLogger subscribed to event bus")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_task_started(self, event: Event) -> None:
        payload = event.payload
        task_id = payload.get("task_id") or payload.get("id")
        if not task_id:
            return
        with self._lock:
            self._active[task_id] = {
                "trajectory_id": str(uuid4()),
                "task_id": task_id,
                "session_id": payload.get("session_id"),
                "agent": event.source,
                "stage": payload.get("stage"),
                "project_slug": payload.get("project_slug")
                    or self._session_meta.get(payload.get("session_id"), {}).get("project_slug"),
                "start_time": event.timestamp.isoformat(),
                "end_time": None,
                "events": [],
                "outcome": None,
                "error_message": None,
            }

    def _on_task_completed(self, event: Event) -> None:
        payload = event.payload
        task_id = payload.get("task_id") or payload.get("id")
        if not task_id:
            return
        with self._lock:
            traj = self._active.pop(task_id, None)
        if traj is None:
            return
        traj["end_time"] = event.timestamp.isoformat()
        traj["outcome"] = "success"
        traj["error_message"] = None
        self._write(traj)

    def _on_task_failed(self, event: Event) -> None:
        payload = event.payload
        task_id = payload.get("task_id") or payload.get("id")
        if not task_id:
            return
        with self._lock:
            traj = self._active.pop(task_id, None)
        if traj is None:
            return
        traj["end_time"] = event.timestamp.isoformat()
        traj["outcome"] = "failure"
        traj["error_message"] = payload.get("error") or payload.get("error_message")
        self._write(traj)

    def _on_llm_exchange(self, event: Event) -> None:
        payload = event.payload
        task_id = payload.get("task_id")
        if not task_id:
            return
        with self._lock:
            traj = self._active.get(task_id)
        if traj is None:
            return
        traj["events"].append({
            "type": "llm_call",
            "timestamp": event.timestamp.isoformat(),
            "backend": payload.get("backend"),
            "model": payload.get("model"),
            "prompt_tokens": payload.get("prompt_tokens"),
            "response_tokens": payload.get("response_tokens"),
            "total_tokens": payload.get("total_tokens"),
        })

    def _on_agent_thought(self, event: Event) -> None:
        payload = event.payload
        task_id = payload.get("task_id")
        if not task_id:
            return
        with self._lock:
            traj = self._active.get(task_id)
        if traj is None:
            return
        traj["events"].append({
            "type": "thought",
            "timestamp": event.timestamp.isoformat(),
            "content": payload.get("content", ""),
        })

    def _on_pipeline_started(self, event: Event) -> None:
        payload = event.payload
        session_id = payload.get("session_id")
        project_slug = payload.get("project_slug") or payload.get("slug")
        if session_id and project_slug:
            with self._lock:
                self._session_meta[session_id] = {
                    "project_slug": project_slug,
                    "brief": payload.get("brief"),
                }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _write(self, trajectory: Dict[str, Any]) -> None:
        """Append a completed trajectory to today's JSONL file."""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._output_dir / f"{date_str}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(trajectory, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("Failed to write trajectory to %s", path)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_jsonl(
        self,
        output_path: Path,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        agent: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> int:
        """Export trajectories matching filters to a single JSONL file.

        Returns the number of records written.
        """
        count = 0
        files = sorted(self._output_dir.glob("*.jsonl"))
        with open(output_path, "w", encoding="utf-8") as out_fh:
            for path in files:
                # Filename is YYYY-MM-DD.jsonl
                file_date = path.stem
                if from_date and file_date < from_date:
                    continue
                if to_date and file_date > to_date:
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as in_fh:
                        for line in in_fh:
                            line = line.strip()
                            if not line:
                                continue
                            record = json.loads(line)
                            if agent and record.get("agent") != agent:
                                continue
                            if outcome and record.get("outcome") != outcome:
                                continue
                            out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                            count += 1
                except Exception:
                    logger.exception("Failed to read %s", path)
        return count

    def list_files(self) -> List[Dict[str, Any]]:
        """List available trajectory files with record counts."""
        files = sorted(self._output_dir.glob("*.jsonl"))
        result: List[Dict[str, Any]] = []
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    lines = sum(1 for _ in fh if _.strip())
                result.append({
                    "date": path.stem,
                    "path": str(path),
                    "records": lines,
                })
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_dir() -> Path:
        try:
            return Path(get_settings().data_dir) / "trajectories"
        except Exception:
            return Path("data/trajectories")
