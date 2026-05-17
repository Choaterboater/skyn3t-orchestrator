from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

from skyn3t.memory.tuner import apply_adjustments_to_config

logger = logging.getLogger("skyn3t.cortex.gated_tuner")

DEFAULT_CONFIG_PATH = Path("data/config/runtime.json")

class GatedTuner:
    """Wraps SelfTuningEngine: turns its raw suggestions into review-gated proposals.

    Listens to the same SUGGESTION events the existing tuner emits, packages them
    as Proposal(kind='tuning'), and registers an apply handler that:
      1. snapshots current config to data/config/snapshots/<ts>.json
      2. writes new config
      3. emits TUNING_APPLIED event
    """

    def __init__(self, event_bus, *, config_path: Path | str = DEFAULT_CONFIG_PATH):
        self.event_bus = event_bus
        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._snapshots_dir = self.config_path.parent / "snapshots"
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._wired = False

    def start(self) -> None:
        if self._wired:
            return
        self._wired = True
        # register handler with the proposal store
        try:
            from skyn3t.cortex import get_store
            get_store().register_handler("tuning", self._apply_tuning)
        except Exception:
            logger.exception("could not register tuning handler")
        # SelfTuningEngine publishes via SYSTEM_ALERT with kind="tuning_suggestion".
        # Subscribe specifically rather than to every event in the system, which
        # added a per-event tax on a busy bus.
        try:
            from skyn3t.core.events import EventType
            self.event_bus.subscribe(self._on_event, EventType.SYSTEM_ALERT)
        except Exception:
            logger.exception("could not subscribe to event bus")

    async def stop(self) -> None:
        if not self._wired:
            return
        self._wired = False
        try:
            from skyn3t.core.events import EventType

            self.event_bus.unsubscribe(self._on_event, EventType.SYSTEM_ALERT)
        except Exception:
            logger.exception("could not unsubscribe from event bus")

    def _on_event(self, event) -> None:
        try:
            payload = getattr(event, "payload", {}) or {}
            kind = payload.get("kind", "")
            if kind == "tuning_suggestion":
                self._propose_from_event(payload)
        except Exception:
            logger.exception("on_event error")

    def _propose_from_event(self, payload: Dict[str, Any]) -> None:
        from skyn3t.cortex import get_store
        agent = payload.get("agent", "?")
        adjustments = payload.get("adjustments") or []
        reason = payload.get("reason") or payload.get("rationale") or "self-tuning suggestion"
        if not isinstance(adjustments, list) or not adjustments:
            return
        title = f"Tune {agent}"
        summary = reason
        detail_lines = [
            "**proposed tuning adjustments**",
            "",
            "```json",
            json.dumps(adjustments, indent=2),
            "```",
            "",
            f"_reason_: {reason}",
        ]
        get_store().create(
            kind="tuning",
            title=title,
            summary=summary,
            detail="\n".join(detail_lines),
            payload={
                "agent": agent,
                "adjustments": adjustments,
                "patterns": payload.get("patterns") or [],
                "reason": reason,
            },
            source="self_tuner",
        )

    async def _apply_tuning(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        adjustments = payload.get("adjustments") or []
        agent = str(payload.get("agent") or "").strip() or "unknown"
        if not isinstance(adjustments, list) or not adjustments:
            return {"ok": False, "error": "empty adjustments"}
        # snapshot current config
        existing: Dict[str, Any] = {}
        if self.config_path.exists():
            try:
                existing = json.loads(self.config_path.read_text())
            except Exception:
                existing = {}
        snap_path = self._snapshots_dir / f"{int(time.time())}.json"
        snap_path.write_text(json.dumps(existing, indent=2))

        existing_agents = existing.get("agents") if isinstance(existing.get("agents"), dict) else {}
        current_agent_config = existing_agents.get(agent) if isinstance(existing_agents, dict) else {}
        updated_agent_config = apply_adjustments_to_config(
            current_agent_config if isinstance(current_agent_config, dict) else {},
            list(adjustments),
        )

        merged = dict(existing)
        merged_agents = dict(existing_agents or {})
        merged_agents[agent] = updated_agent_config
        merged["agents"] = merged_agents
        self.config_path.write_text(json.dumps(merged, indent=2))
        # publish event
        try:
            from skyn3t.core.events import Event, EventType
            self.event_bus.publish(
                Event(
                    event_type=EventType.SYSTEM_ALERT,
                    source="gated_tuner",
                    payload={
                        "kind": "tuning_applied",
                        "agent": agent,
                        "adjustments": adjustments,
                        "snapshot": str(snap_path),
                    },
                )
            )
        except Exception:
            logger.debug("TUNING_APPLIED event publish failed", exc_info=True)
        return {
            "ok": True,
            "applied": True,
            "agent": agent,
            "snapshot": str(snap_path),
            "config_path": str(self.config_path),
        }

    def rollback(self, snapshot_name: str) -> Dict[str, Any]:
        snap = self._snapshots_dir / snapshot_name
        if not snap.exists():
            return {"ok": False, "error": "snapshot not found"}
        self.config_path.write_text(snap.read_text())
        return {"ok": True, "restored_from": str(snap)}

    def get_status(self) -> Dict[str, Any]:
        return {
            "wired": self._wired,
            "config_path": str(self.config_path),
            "snapshots_dir": str(self._snapshots_dir),
        }
