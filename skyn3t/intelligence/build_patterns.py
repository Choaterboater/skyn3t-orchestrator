"""BuildPatternScoreboard — learns which scaffold shapes actually build.

The lesson scoreboard (lesson_attribution.py) tracks whether *injected
lessons* helped or hurt at the task level. That's the inner loop.

This module is the OUTER loop: for every Studio project that ran a
scaffold + BuildVerifier, record (stack, shape_signature, verdict) so
we can answer questions like:

    "When stack=next, do scaffolds that include tsconfig.json succeed
     more often than those that don't?"
    "When stack=python_cli, does shipping a tests/ directory correlate
     with build success?"

The shape signature is just a sorted tuple of the relative file paths
the scaffold produced — small, hashable, comparable across projects.

Persistence shape (data/build_patterns.json):

    {
      "<stack>": {
        "<shape_hash>": {
          "stack": "next",
          "shape": ["app/page.tsx", "package.json", ...],
          "success": 12,
          "failure": 3,
          "last_seen_at": 1729012345.6
        },
        ...
      },
      ...
    }

The meta-agent can read this store and surface biases as Cortex
proposals: "On 5+ recent fastapi builds, those missing
tests/test_health.py had a 70% failure rate. Recommend always including
it." That's the closed-loop self-improvement the user asked for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger("skyn3t.intelligence.build_patterns")


@dataclass
class BuildPatternStats:
    """Running tally for one (stack, shape) combo.

    ``tags`` records per-tag occurrence counts (e.g. ``{"missing_mount":
    4}`` means this shape lost the router mount four times). The planner
    reads tags to pre-warn the CodeAgent on shapes that historically fail
    a specific way.
    """

    stack: str
    shape: List[str] = field(default_factory=list)
    success: int = 0
    failure: int = 0
    skipped: int = 0
    tags: Dict[str, int] = field(default_factory=dict)
    last_seen_at: float = field(default_factory=time.time)

    @property
    def total(self) -> int:
        return self.success + self.failure + self.skipped

    @property
    def success_rate(self) -> float:
        """In [0, 1]. Returns 0 when no scored attempts (success+failure==0)."""
        denom = self.success + self.failure
        if denom == 0:
            return 0.0
        return self.success / denom

    def to_dict(self) -> Dict:
        return {
            "stack": self.stack,
            "shape": list(self.shape),
            "success": self.success,
            "failure": self.failure,
            "skipped": self.skipped,
            "tags": dict(self.tags),
            "last_seen_at": self.last_seen_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "BuildPatternStats":
        raw_tags = data.get("tags") or {}
        tags: Dict[str, int] = {}
        if isinstance(raw_tags, dict):
            for k, v in raw_tags.items():
                try:
                    tags[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        return cls(
            stack=str(data.get("stack", "")),
            shape=[str(p) for p in (data.get("shape") or [])],
            success=int(data.get("success", 0)),
            failure=int(data.get("failure", 0)),
            skipped=int(data.get("skipped", 0)),
            tags=tags,
            last_seen_at=float(data.get("last_seen_at", time.time())),
        )


class BuildPatternScoreboard:
    """Outer-loop store: (stack, shape_signature) → outcomes.

    Thread-safe, atomic-write persistence. Designed for cheap reads
    (meta-agent dashboards) and bounded writes (one per project).
    """

    def __init__(
        self,
        store_path: Optional[Path] = None,
        *,
        flush_every: int = 4,
    ):
        self.store_path = Path(store_path) if store_path else Path("data/build_patterns.json")
        self._flush_every = int(flush_every)
        self._unflushed = 0
        self._lock = threading.Lock()
        # stack → shape_hash → stats
        self._stats: Dict[str, Dict[str, BuildPatternStats]] = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _shape_hash(shape: Iterable[str]) -> str:
        """Stable, short hash of a normalized shape signature."""
        # Normalize: lowercase, sorted, dedup so order-of-creation noise
        # doesn't fragment the bucket.
        normalized = sorted({str(p).strip() for p in shape if str(p).strip()})
        joined = "\n".join(normalized)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    def _load(self) -> Dict[str, Dict[str, BuildPatternStats]]:
        try:
            if self.store_path.exists():
                raw = json.loads(self.store_path.read_text())
                if isinstance(raw, dict):
                    out: Dict[str, Dict[str, BuildPatternStats]] = {}
                    for stack, by_hash in raw.items():
                        if not isinstance(by_hash, dict):
                            continue
                        bucket = {
                            str(h): BuildPatternStats.from_dict(v)
                            for h, v in by_hash.items()
                            if isinstance(v, dict)
                        }
                        if bucket:
                            out[str(stack)] = bucket
                    return out
        except Exception:
            logger.exception("build_patterns load failed")
        return {}

    def _flush_locked(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                stack: {h: s.to_dict() for h, s in bucket.items()}
                for stack, bucket in self._stats.items()
            }
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.store_path)
            self._unflushed = 0
        except Exception:
            logger.exception("build_patterns flush failed")

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, stack: str, shape: Iterable[str], verdict: str) -> None:
        """Tally a build verifier outcome for this (stack, shape) combo.

        ``verdict`` is one of: 'yes' | 'no' | 'skipped'. Anything else
        is treated as 'skipped' (defensive — never crash on a bad signal).
        """
        if not stack:
            return
        shape_list = sorted({str(p).strip() for p in shape if str(p).strip()})
        if not shape_list:
            return
        v = (verdict or "").lower().strip()
        if v not in ("yes", "no", "skipped"):
            v = "skipped"
        h = self._shape_hash(shape_list)
        with self._lock:
            bucket = self._stats.setdefault(stack, {})
            stats = bucket.get(h)
            if stats is None:
                stats = BuildPatternStats(stack=stack, shape=shape_list)
                bucket[h] = stats
            if v == "yes":
                stats.success += 1
            elif v == "no":
                stats.failure += 1
            else:
                stats.skipped += 1
            stats.last_seen_at = time.time()
            self._unflushed += 1
            if self._unflushed >= self._flush_every:
                self._flush_locked()

    def record_tag(self, stack: str, shape: Iterable[str], tag: str) -> None:
        """Increment a named occurrence tag on a (stack, shape) bucket.

        Used for per-class failure attribution — e.g. when the
        consistency engine emits a ``missing_mount`` blocker, the
        runner records ``("node-express", shape, "missing_mount")``
        so the planner can later pre-warn the CodeAgent that this
        scaffold shape has historically lost the router mount.

        Tags are independent of the success/failure counters: a build
        can succeed AND have a tag recorded earlier in its lifecycle.
        """
        if not stack or not tag:
            return
        shape_list = sorted({str(p).strip() for p in shape if str(p).strip()})
        if not shape_list:
            return
        h = self._shape_hash(shape_list)
        with self._lock:
            bucket = self._stats.setdefault(stack, {})
            stats = bucket.get(h)
            if stats is None:
                stats = BuildPatternStats(stack=stack, shape=shape_list)
                bucket[h] = stats
            stats.tags[tag] = stats.tags.get(tag, 0) + 1
            stats.last_seen_at = time.time()
            self._unflushed += 1
            if self._unflushed >= self._flush_every:
                self._flush_locked()

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def tag_count_for_shape(
        self, stack: str, shape: Iterable[str], tag: str,
    ) -> int:
        """How many times ``tag`` has been recorded for this exact shape.

        Returns 0 when the bucket is unknown. Planner uses this to
        decide whether to inject a pre-warning into CodeAgent.
        """
        if not stack or not tag:
            return 0
        shape_list = sorted({str(p).strip() for p in shape if str(p).strip()})
        if not shape_list:
            return 0
        h = self._shape_hash(shape_list)
        with self._lock:
            bucket = self._stats.get(stack, {})
            stats = bucket.get(h)
            if stats is None:
                return 0
            return int(stats.tags.get(tag, 0))

    def best_shape(self, stack: str, *, min_samples: int = 3) -> Optional[BuildPatternStats]:
        """Return the highest-success-rate shape for the stack with at
        least ``min_samples`` graded attempts (success + failure)."""
        with self._lock:
            bucket = self._stats.get(stack, {})
            ranked = [
                s for s in bucket.values()
                if (s.success + s.failure) >= min_samples
            ]
            if not ranked:
                return None
            ranked.sort(key=lambda s: (s.success_rate, s.success), reverse=True)
            return BuildPatternStats.from_dict(ranked[0].to_dict())

    def worst_shape(self, stack: str, *, min_samples: int = 3) -> Optional[BuildPatternStats]:
        with self._lock:
            bucket = self._stats.get(stack, {})
            ranked = [
                s for s in bucket.values()
                if (s.success + s.failure) >= min_samples
            ]
            if not ranked:
                return None
            ranked.sort(key=lambda s: (s.success_rate, -s.failure))
            return BuildPatternStats.from_dict(ranked[0].to_dict())

    def summary(self) -> Dict:
        """Cheap aggregate suitable for the dashboard."""
        with self._lock:
            stacks_count = len(self._stats)
            total_shapes = sum(len(bucket) for bucket in self._stats.values())
            success_total = sum(
                s.success for bucket in self._stats.values() for s in bucket.values()
            )
            failure_total = sum(
                s.failure for bucket in self._stats.values() for s in bucket.values()
            )
        return {
            "stacks_tracked": stacks_count,
            "shapes_tracked": total_shapes,
            "total_success": success_total,
            "total_failure": failure_total,
        }

    def all_stats_for(self, stack: str) -> List[BuildPatternStats]:
        """Snapshot every recorded shape for a stack — used by meta-agent
        to compare A-shape vs B-shape success rates."""
        with self._lock:
            bucket = self._stats.get(stack, {})
            return [BuildPatternStats.from_dict(s.to_dict()) for s in bucket.values()]


# Module-level singleton — same shape as LessonScoreboard's discovery
# pattern but lighter weight (just a global accessor).
_default_scoreboard: Optional[BuildPatternScoreboard] = None


def get_default_scoreboard() -> BuildPatternScoreboard:
    """Process-wide scoreboard instance backed by the default JSON path."""
    global _default_scoreboard
    if _default_scoreboard is None:
        _default_scoreboard = BuildPatternScoreboard()
    return _default_scoreboard
