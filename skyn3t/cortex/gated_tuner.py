from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("skyn3t.cortex.gated_tuner")

CONFIG_DIR = Path("data/config")
SNAPSHOTS_DIR = Path("data/config/snapshots")

class GatedTuner:
    """Wraps SelfTuningEngine: turns its raw suggestions into review-gated proposals.

    Listens to the same SUGGESTION events the existing tuner emits, packages them
    as Proposal(kind='tuning'), and registers an apply handler that:
      1. snapshots current config to data/config/snapshots/<ts>.json
      2. writes new config
      3. emits TUNING_APPLIED event
    """

    def __init__(self, event_bus, *, config_path: Path | str = "data/config/runtime.json"):
        self.event_bus = event_bus
        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
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

    def _on_event(self, event) -> None:
        try:
            payload = getattr(event, "payload", {}) or {}
            etype = getattr(event, "event_type", None)
            etype_value = getattr(etype, "value", str(etype)) if etype else ""
            kind = payload.get("kind", "")
            # The known "raw suggestion" channel from SelfTuningEngine — be liberal
            if "TUNING" in etype_value.upper() or kind in ("tuning_suggestion", "self_tune"):
                self._propose_from_event(payload)
        except Exception:
            logger.exception("on_event error")

    def _propose_from_event(self, payload: Dict[str, Any]) -> None:
        from skyn3t.cortex import get_store
        agent = payload.get("agent", "?")
        change = payload.get("change") or payload.get("update") or {}
        reason = payload.get("reason") or payload.get("rationale") or "self-tuning suggestion"
        if not change:
            return
        title = f"Tune {agent}"
        summary = reason
        detail_lines = ["**proposed config change**", "", "```json", json.dumps(change, indent=2), "```", "", f"_reason_: {reason}"]
        get_store().create(kind="tuning", title=title, summary=summary,
                           detail="\n".join(detail_lines),
                           payload={"agent": agent, "change": change, "reason": reason},
                           source="self_tuner")

    async def _apply_tuning(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        change: Dict[str, Any] = payload.get("change") or {}
        if not change:
            return {"ok": False, "error": "empty change"}
        # snapshot current config
        existing: Dict[str, Any] = {}
        if self.config_path.exists():
            try:
                existing = json.loads(self.config_path.read_text())
            except Exception:
                existing = {}
        snap_path = SNAPSHOTS_DIR / f"{int(time.time())}.json"
        snap_path.write_text(json.dumps(existing, indent=2))
        # merge: simple key replacement
        merged = {**existing, **change}
        self.config_path.write_text(json.dumps(merged, indent=2))
        # publish event
        try:
            from skyn3t.core.events import Event, EventType
            self.event_bus.publish(Event(event_type=EventType.SYSTEM_ALERT, source="gated_tuner",
                                         payload={"kind": "TUNING_APPLIED", "change": change,
                                                  "snapshot": str(snap_path)}))
        except Exception:
            logger.debug("TUNING_APPLIED event publish failed", exc_info=True)
        return {"applied": True, "snapshot": str(snap_path), "config_path": str(self.config_path)}

    def rollback(self, snapshot_name: str) -> Dict[str, Any]:
        snap = SNAPSHOTS_DIR / snapshot_name
        if not snap.exists():
            return {"ok": False, "error": "snapshot not found"}
        self.config_path.write_text(snap.read_text())
        return {"ok": True, "restored_from": str(snap)}
