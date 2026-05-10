"""Checkpoint system for state persistence."""

import json
import logging
import os
import shutil
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.persistence.checkpoint")


# Bump when the on-disk Checkpoint shape changes in a non-additive way.
# from_bytes refuses to load schema versions newer than CURRENT_SCHEMA_VERSION
# rather than silently dropping fields it doesn't understand.
CURRENT_SCHEMA_VERSION = 1


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
    schema_version: int = CURRENT_SCHEMA_VERSION

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
        version = int(parsed.get("schema_version", 1))
        if version > CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"Checkpoint schema_version={version} is newer than this "
                f"build's CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION}; "
                f"refusing to load to avoid silent field loss."
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
        # Atomic write: serialize to a sibling .tmp file, fsync, then rename.
        # A crash mid-write leaves the previous newest checkpoint intact rather
        # than producing a corrupt half-written file that load_latest would crash on.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "wb") as fh:
            fh.write(checkpoint.to_bytes())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)

        self._cleanup_old_checkpoints()
        self._checkpoint_count += 1

        return checkpoint.checkpoint_id

    def load_latest(self) -> Optional[Checkpoint]:
        """Load the most recent valid checkpoint.

        Walks newest-to-oldest and skips any file that fails to decode, so a
        corrupt checkpoint at the head doesn't make recovery impossible.
        """
        checkpoints = sorted(
            self.checkpoint_dir.glob("*.cp"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for cp_path in checkpoints:
            try:
                return Checkpoint.from_bytes(cp_path.read_bytes())
            except Exception as exc:
                logger.warning(
                    "Failed to load checkpoint %s (%s); trying older checkpoint.",
                    cp_path.name, exc,
                )
        return None

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
