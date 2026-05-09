"""Audit logging for SkyN3t.

Records every security-relevant action in a tamper-resistant,
append-only log with support for forensic querying.
"""

import hashlib
import json
import logging
import threading
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class AuditEntry:
    """A single audit log entry."""

    timestamp: str
    actor: str
    action: str
    resource: str
    result: str
    before_state: Optional[Dict[str, Any]] = None
    after_state: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    entry_hash: str = ""
    prev_hash: str = ""
    sequence: int = 0

    def compute_hash(self) -> str:
        """Compute a hash of this entry for tamper resistance."""
        data = {
            "timestamp": self.timestamp,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "result": self.result,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "metadata": self.metadata,
            "sequence": self.sequence,
            "prev_hash": self.prev_hash,
        }
        payload = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def verify(self) -> bool:
        return self.compute_hash() == self.entry_hash

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "result": self.result,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "metadata": self.metadata,
            "entry_hash": self.entry_hash,
            "prev_hash": self.prev_hash,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditEntry":
        return cls(
            timestamp=data["timestamp"],
            actor=data["actor"],
            action=data["action"],
            resource=data["resource"],
            result=data["result"],
            before_state=data.get("before_state"),
            after_state=data.get("after_state"),
            metadata=data.get("metadata", {}),
            entry_hash=data.get("entry_hash", ""),
            prev_hash=data.get("prev_hash", ""),
            sequence=data.get("sequence", 0),
        )


class AuditLog:
    """Tamper-resistant append-only audit log.

    Each entry includes a hash of its contents plus the previous
    entry's hash, forming a simple blockchain. If any entry is
    modified, verify_chain() will detect the break.
    """

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        max_entries_per_file: int = 10000,
    ):
        self.log_dir = log_dir or Path("./logs/audit")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_entries_per_file = max_entries_per_file
        self._entries: List[AuditEntry] = []
        self._lock = threading.RLock()
        self._sequence = 0
        self._current_file: Optional[Path] = None
        self._load_latest()

    def _get_log_files(self) -> List[Path]:
        """Get all audit log files sorted by name."""
        if not self.log_dir.exists():
            return []
        return sorted(self.log_dir.glob("audit_*.jsonl"))

    def _load_latest(self) -> None:
        """Load the most recent audit log file."""
        files = self._get_log_files()
        if not files:
            self._current_file = self.log_dir / f"audit_{int(time.time())}.jsonl"
            return

        self._current_file = files[-1]
        try:
            with self._current_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = AuditEntry.from_dict(json.loads(line))
                        self._entries.append(entry)
                        if entry.sequence > self._sequence:
                            self._sequence = entry.sequence
                    except json.JSONDecodeError:
                        logger.warning("Corrupted audit line: %s", line[:80])
        except Exception as e:
            logger.error("Failed to load audit log %s: %s", self._current_file, e)

    def _rotate_file(self) -> None:
        """Start a new log file if the current one is too large."""
        if len(self._entries) >= self.max_entries_per_file:
            self._current_file = self.log_dir / f"audit_{int(time.time())}.jsonl"
            self._entries = []

    def record(
        self,
        actor: str,
        action: str,
        resource: str,
        result: str,
        before_state: Optional[Dict[str, Any]] = None,
        after_state: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AuditEntry:
        """Record an action in the audit log.

        Args:
            actor: Who/what performed the action (e.g. agent name).
            action: What was done (e.g. "task_executed").
            resource: What was affected (e.g. task_id or file path).
            result: Outcome (e.g. "success", "denied", "error").
            before_state: Optional state before the action.
            after_state: Optional state after the action.
            metadata: Any extra context.

        Returns:
            The created AuditEntry.
        """
        with self._lock:
            self._sequence += 1
            prev_hash = self._entries[-1].entry_hash if self._entries else "0" * 64

            entry = AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                actor=actor,
                action=action,
                resource=resource,
                result=result,
                before_state=deepcopy(before_state) if before_state else None,
                after_state=deepcopy(after_state) if after_state else None,
                metadata=metadata or {},
                prev_hash=prev_hash,
                sequence=self._sequence,
            )
            entry.entry_hash = entry.compute_hash()
            self._entries.append(entry)
            self._append_to_file(entry)
            self._rotate_file()

        logger.debug(
            "Audit: %s %s %s → %s",
            actor, action, resource, result,
        )
        return entry

    def _append_to_file(self, entry: AuditEntry) -> None:
        """Append a single entry to the current log file."""
        if not self._current_file:
            return
        try:
            with self._current_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict(), default=str) + "\n")
                f.flush()
        except Exception as e:
            logger.error("Failed to write audit entry: %s", e)

    def verify_chain(self) -> Tuple[bool, Optional[int]]:
        """Verify the integrity of the entire audit chain.

        Returns:
            (is_valid, first_broken_sequence) where first_broken_sequence
            is None if the chain is fully valid.
        """
        with self._lock:
            for i, entry in enumerate(self._entries):
                if not entry.verify():
                    return False, entry.sequence
                if i > 0:
                    expected_prev = self._entries[i - 1].entry_hash
                    if entry.prev_hash != expected_prev:
                        return False, entry.sequence
                else:
                    if entry.prev_hash != "0" * 64:
                        return False, entry.sequence
            return True, None

    def query(
        self,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        resource: Optional[str] = None,
        result: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AuditEntry]:
        """Query audit entries with filters.

        All filters are ANDed together. Supports simple wildcards
        using '*' in string fields.
        """

        def _match(value: Optional[str], pattern: Optional[str]) -> bool:
            if pattern is None:
                return True
            if value is None:
                return False
            if "*" in pattern:
                import fnmatch
                return fnmatch.fnmatch(value, pattern)
            return value == pattern

        def _in_range(ts: str) -> bool:
            dt = datetime.fromisoformat(ts)
            if start_time and dt < start_time:
                return False
            if end_time and dt > end_time:
                return False
            return True

        with self._lock:
            matches = [
                e for e in self._entries
                if _match(e.actor, actor)
                and _match(e.action, action)
                and _match(e.resource, resource)
                and _match(e.result, result)
                and _in_range(e.timestamp)
            ]
            return matches[offset:offset + limit]

    def query_by_actor(self, actor: str, limit: int = 100) -> List[AuditEntry]:
        """Get all entries for a specific actor."""
        return self.query(actor=actor, limit=limit)

    def query_by_resource(self, resource: str, limit: int = 100) -> List[AuditEntry]:
        """Get all entries affecting a specific resource."""
        return self.query(resource=resource, limit=limit)

    def query_failures(self, limit: int = 100) -> List[AuditEntry]:
        """Get all entries with non-success results."""
        with self._lock:
            matches = [
                e for e in self._entries
                if e.result not in ("success", "allowed", "granted")
            ]
            return matches[-limit:]

    def export(self, path: Path) -> None:
        """Export the full audit log to a JSON file."""
        with self._lock:
            data = {
                "exported_at": datetime.utcnow().isoformat(),
                "total_entries": len(self._entries),
                "chain_valid": self.verify_chain()[0],
                "entries": [e.to_dict() for e in self._entries],
            }
        path.write_text(json.dumps(data, indent=2, default=str))

    def get_stats(self) -> Dict[str, Any]:
        """Get audit log statistics."""
        with self._lock:
            valid, broken = self.verify_chain()
            return {
                "total_entries": len(self._entries),
                "current_file": str(self._current_file) if self._current_file else None,
                "chain_valid": valid,
                "first_broken_sequence": broken,
                "oldest_entry": self._entries[0].timestamp if self._entries else None,
                "newest_entry": self._entries[-1].timestamp if self._entries else None,
            }
