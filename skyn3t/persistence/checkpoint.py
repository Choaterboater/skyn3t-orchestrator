"""Checkpoint system for state persistence."""

import json
import shutil
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Checkpoint:
    """A snapshot of system state."""

    checkpoint_id: str
    timestamp: str
    agent_states: List[Dict[str, Any]] = field(default_factory=list)
    task_states: List[Dict[str, Any]] = field(default_factory=list)
    pipeline_states: List[Dict[str, Any]] = field(default_factory=list)
    event_position: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        """Serialize to compressed bytes."""
        data = json.dumps(asdict(self), default=str).encode("utf-8")
        return zlib.compress(data)

    @classmethod
    def from_bytes(cls, data: bytes) -> "Checkpoint":
        """Deserialize from compressed bytes."""
        decompressed = zlib.decompress(data)
        parsed = json.loads(decompressed.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise TypeError(
                f"Checkpoint payload must be a JSON object, got {type(parsed).__name__}"
            )
        try:
            return cls(**parsed)
        except TypeError as e:
            raise TypeError(
                f"Checkpoint payload does not match Checkpoint schema: {e}"
            ) from e


class CheckpointManager:
    """Manages system checkpoints."""

    def __init__(
        self,
        checkpoint_dir: str = "./data/checkpoints",
        auto_interval_seconds: int = 60,
        max_checkpoints: int = 10,
    ):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.auto_interval = auto_interval_seconds
        self.max_checkpoints = max_checkpoints
        self._checkpoint_count = 0

    def save(
        self,
        agent_states: List[Dict[str, Any]],
        task_states: List[Dict[str, Any]],
        pipeline_states: Optional[List[Dict[str, Any]]] = None,
        event_position: int = 0,
    ) -> str:
        """Save a new checkpoint."""
        checkpoint = Checkpoint(
            checkpoint_id=f"cp-{datetime.now(timezone.utc).isoformat()}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_states=agent_states,
            task_states=task_states,
            pipeline_states=pipeline_states or [],
            event_position=event_position,
        )

        path = self.checkpoint_dir / f"{checkpoint.checkpoint_id}.cp"
        path.write_bytes(checkpoint.to_bytes())

        self._cleanup_old_checkpoints()
        self._checkpoint_count += 1

        return checkpoint.checkpoint_id

    def load_latest(self) -> Optional[Checkpoint]:
        """Load the most recent checkpoint."""
        checkpoints = sorted(self.checkpoint_dir.glob("*.cp"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not checkpoints:
            return None

        return Checkpoint.from_bytes(checkpoints[0].read_bytes())

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        """List all available checkpoints."""
        checkpoints = []
        for path in sorted(self.checkpoint_dir.glob("*.cp"), key=lambda p: p.stat().st_mtime, reverse=True):
            cp = Checkpoint.from_bytes(path.read_bytes())
            checkpoints.append(
                {
                    "id": cp.checkpoint_id,
                    "timestamp": cp.timestamp,
                    "agents": len(cp.agent_states),
                    "tasks": len(cp.task_states),
                    "pipelines": len(cp.pipeline_states),
                    "event_position": cp.event_position,
                }
            )
        return checkpoints

    def _cleanup_old_checkpoints(self) -> None:
        """Remove old checkpoints beyond max_checkpoints."""
        checkpoints = sorted(self.checkpoint_dir.glob("*.cp"), key=lambda p: p.stat().st_mtime)
        while len(checkpoints) > self.max_checkpoints:
            checkpoints[0].unlink()
            checkpoints.pop(0)

    def delete_all(self) -> None:
        """Delete all checkpoints."""
        shutil.rmtree(self.checkpoint_dir, ignore_errors=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
