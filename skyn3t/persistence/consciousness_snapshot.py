"""Consciousness snapshots — durable orchestrator state beyond task logs.

A consciousness snapshot captures the in-memory "mind" of the orchestrator:
working memory, insights, token rollups, meta-agent counters, autonomous loop
state, and recent events.  It intentionally does NOT replay running tasks or
pipelines; those are either rehydrated from persistent stores or marked
interrupted and requeued by the crash-recovery path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from skyn3t.persistence.checkpoint import Checkpoint, CheckpointManager

logger = logging.getLogger("skyn3t.persistence.consciousness_snapshot")


class ConsciousnessSnapshot:
    """Collect and restore orchestrator consciousness state."""

    def __init__(self, checkpoint_manager: Optional[CheckpointManager] = None):
        if checkpoint_manager is not None:
            self.manager = checkpoint_manager
        else:
            from skyn3t.config.settings import get_settings

            settings = get_settings()
            self.manager = CheckpointManager(
                checkpoint_dir=str(settings.snapshot_dir),
                max_checkpoints=settings.snapshot_max_kept,
            )

    @staticmethod
    async def from_orchestrator(orchestrator: Any) -> Dict[str, Any]:
        """Build a serializable consciousness payload from an orchestrator."""
        consciousness = getattr(orchestrator, "_consciousness", None)
        meta_agent = getattr(orchestrator, "_meta_agent", None)
        autonomous = getattr(orchestrator, "_autonomous_coordinator", None)
        event_bus = getattr(orchestrator, "event_bus", None)

        snapshot: Dict[str, Any] = {
            "version": 1,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

        # 1. Shared working memory + sessions + insights
        if consciousness is not None:
            try:
                snapshot["consciousness"] = await consciousness.to_snapshot()
            except Exception:
                logger.debug("consciousness snapshot failed", exc_info=True)

        # 2. Token rollups (singleton)
        try:
            from skyn3t.observability.token_tracker import get_default_tracker

            snapshot["token_tracker"] = get_default_tracker().to_snapshot()
        except Exception:
            logger.debug("token tracker snapshot failed", exc_info=True)

        # 3. Autonomous loop durable counters/queue
        if autonomous is not None:
            try:
                snapshot["autonomous_loop"] = autonomous.to_snapshot()
            except Exception:
                logger.debug("autonomous loop snapshot failed", exc_info=True)

        # 4. Meta-agent threshold state
        if meta_agent is not None:
            try:
                snapshot["meta_agent"] = meta_agent.to_snapshot()
            except Exception:
                logger.debug("meta agent snapshot failed", exc_info=True)

        # 5. Recent event history (forensics, not replay)
        if event_bus is not None:
            try:
                snapshot["event_bus_history"] = event_bus.to_snapshot(limit=250)
            except Exception:
                logger.debug("event bus snapshot failed", exc_info=True)

        # 6. Orchestrator metadata that is otherwise in-memory only
        snapshot["orchestrator"] = ConsciousnessSnapshot._orchestrator_metadata(
            orchestrator
        )

        return snapshot

    @staticmethod
    def _orchestrator_metadata(orchestrator: Any) -> Dict[str, Any]:
        """Serialize non-execution orchestrator metadata."""
        meta: Dict[str, Any] = {"agent_configs": {}}
        for name, agent in getattr(orchestrator, "agents", {}).items():
            try:
                view = getattr(agent, "get_config_view", None)
                stats = getattr(agent, "get_stats", None)
                meta["agent_configs"][name] = {
                    "config_view": view() if view else {},
                    "stats": stats() if stats else {},
                }
            except Exception:
                logger.debug("agent config snapshot failed for %s", name, exc_info=True)

        # Idempotency keys with ISO timestamps so JSON round-trips.
        idem: Dict[str, Any] = {}
        for key, (task_id, ts) in getattr(orchestrator, "_idempotency_keys", {}).items():
            iso_ts = ts.isoformat() if isinstance(ts, datetime) else ts
            idem[key] = {"task_id": task_id, "timestamp": iso_ts}
        meta["idempotency_keys"] = idem
        meta["cancelled_tasks"] = list(getattr(orchestrator, "_cancelled_tasks", set()))
        meta["agent_registry"] = dict(getattr(orchestrator, "agent_registry", {}))
        return meta

    async def restore_into_orchestrator(
        self,
        orchestrator: Any,
        checkpoint: Checkpoint,
    ) -> Dict[str, Any]:
        """Restore safe subsets of a checkpoint into a running orchestrator."""
        restored: Dict[str, Any] = {"restored": [], "skipped": []}

        consciousness = getattr(orchestrator, "_consciousness", None)
        if consciousness is not None and checkpoint.consciousness_state:
            try:
                await consciousness.restore_snapshot(checkpoint.consciousness_state)
                restored["restored"].append("consciousness")
            except Exception:
                logger.exception("failed to restore consciousness state")
                restored["skipped"].append("consciousness")

        if checkpoint.token_tracker_state:
            try:
                from skyn3t.observability.token_tracker import get_default_tracker

                get_default_tracker().restore_snapshot(checkpoint.token_tracker_state)
                restored["restored"].append("token_tracker")
            except Exception:
                logger.exception("failed to restore token tracker state")
                restored["skipped"].append("token_tracker")

        autonomous = getattr(orchestrator, "_autonomous_coordinator", None)
        if autonomous is not None and checkpoint.autonomous_loop_state:
            try:
                autonomous.restore_snapshot(checkpoint.autonomous_loop_state)
                restored["restored"].append("autonomous_loop")
            except Exception:
                logger.exception("failed to restore autonomous loop state")
                restored["skipped"].append("autonomous_loop")

        meta_agent = getattr(orchestrator, "_meta_agent", None)
        if meta_agent is not None and checkpoint.meta_agent_state:
            try:
                meta_agent.restore_snapshot(checkpoint.meta_agent_state)
                restored["restored"].append("meta_agent")
            except Exception:
                logger.exception("failed to restore meta agent state")
                restored["skipped"].append("meta_agent")

        event_bus = getattr(orchestrator, "event_bus", None)
        if event_bus is not None and checkpoint.event_bus_history:
            try:
                event_bus.restore_snapshot(checkpoint.event_bus_history)
                restored["restored"].append("event_bus_history")
            except Exception:
                logger.exception("failed to restore event bus history")
                restored["skipped"].append("event_bus_history")

        # We deliberately do NOT restore running_tasks, task_results, or
        # pipeline states.  The crash-recovery path reaps interrupted projects
        # and the persistent MemoryStore owns completed work.
        if checkpoint.orchestrator_metadata:
            try:
                ConsciousnessSnapshot._restore_orchestrator_metadata(
                    orchestrator, checkpoint.orchestrator_metadata
                )
                restored["restored"].append("orchestrator_metadata")
            except Exception:
                logger.exception("failed to restore orchestrator metadata")
                restored["skipped"].append("orchestrator_metadata")

        return restored

    @staticmethod
    def _restore_orchestrator_metadata(
        orchestrator: Any, metadata: Dict[str, Any]
    ) -> None:
        """Restore idempotency keys, cancelled tasks, and registry."""
        idem_raw = metadata.get("idempotency_keys") or {}
        for key, info in idem_raw.items():
            ts_raw = info.get("timestamp")
            ts: datetime
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw)
                except Exception:
                    ts = datetime.now(timezone.utc)
            elif isinstance(ts_raw, datetime):
                ts = ts_raw
            else:
                ts = datetime.now(timezone.utc)
            orchestrator._idempotency_keys[key] = (info.get("task_id", ""), ts)

        cancelled = metadata.get("cancelled_tasks")
        if cancelled:
            orchestrator._cancelled_tasks.update(set(cancelled))

        registry = metadata.get("agent_registry")
        if registry:
            orchestrator.agent_registry.update(registry)

    async def create(self, orchestrator: Any) -> str:
        """Create and persist a consciousness snapshot."""
        snapshot = await self.from_orchestrator(orchestrator)
        return self.manager.save(
            agent_states=[],
            task_states=[],
            pipeline_states=[],
            event_position=0,
            consciousness_state=snapshot.get("consciousness", {}),
            token_tracker_state=snapshot.get("token_tracker", {}),
            autonomous_loop_state=snapshot.get("autonomous_loop", {}),
            meta_agent_state=snapshot.get("meta_agent", {}),
            event_bus_history=snapshot.get("event_bus_history", []),
            orchestrator_metadata=snapshot.get("orchestrator", {}),
        )

    def list(self) -> List[Dict[str, Any]]:
        """List available snapshots."""
        return self.manager.list_checkpoints()

    async def restore_latest(self, orchestrator: Any) -> Optional[Dict[str, Any]]:
        """Load the most recent compatible snapshot and restore it."""
        checkpoint = self.manager.load_latest()
        if checkpoint is None:
            return None
        return await self.restore_into_orchestrator(orchestrator, checkpoint)
