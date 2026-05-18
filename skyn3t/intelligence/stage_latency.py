"""Per-stage latency tracking.

A simple aggregate store keyed by stage name (brainstorm, architect,
designer, code, ...). Each entry holds count + cumulative seconds +
moving min/max so the UI can render "Architect: avg 187s (last 14
runs, range 95-302s)" without reading every project.json on the disk.

Read-mostly. Writes go through ``record_stage_duration`` which is
called once per stage completion. Persisted to
``data/stage_latency.json`` with the same atomic-write pattern as the
rest of the intelligence layer.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StageLatencyStats:
    """Aggregate stats for one stage across many runs.

    We keep a small running window of recent durations (last 50) so
    median / p95 stay representative without unbounded growth. Anything
    older is folded into the cumulative count + total only.
    """
    stage: str
    count: int = 0
    total_seconds: float = 0.0
    min_seconds: Optional[float] = None
    max_seconds: Optional[float] = None
    last_seconds: float = 0.0
    last_updated_at: float = 0.0
    recent: List[float] = field(default_factory=list)

    @property
    def avg_seconds(self) -> float:
        if self.count <= 0:
            return 0.0
        return self.total_seconds / self.count

    def add(self, duration: float) -> None:
        if duration <= 0 or duration != duration:  # 0 or NaN
            return
        self.count += 1
        self.total_seconds += duration
        self.last_seconds = duration
        self.last_updated_at = time.time()
        if self.min_seconds is None or duration < self.min_seconds:
            self.min_seconds = duration
        if self.max_seconds is None or duration > self.max_seconds:
            self.max_seconds = duration
        self.recent.append(duration)
        # Bounded window so the file doesn't grow forever
        if len(self.recent) > 50:
            self.recent = self.recent[-50:]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["avg_seconds"] = self.avg_seconds
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "StageLatencyStats":
        return cls(
            stage=str(data.get("stage") or ""),
            count=int(data.get("count") or 0),
            total_seconds=float(data.get("total_seconds") or 0.0),
            min_seconds=(
                None if data.get("min_seconds") is None
                else float(data.get("min_seconds"))
            ),
            max_seconds=(
                None if data.get("max_seconds") is None
                else float(data.get("max_seconds"))
            ),
            last_seconds=float(data.get("last_seconds") or 0.0),
            last_updated_at=float(data.get("last_updated_at") or 0.0),
            recent=[float(x) for x in (data.get("recent") or []) if isinstance(x, (int, float))],
        )


def _store_path() -> Path:
    try:
        from skyn3t.config.settings import get_settings
        return Path(get_settings().data_dir) / "stage_latency.json"
    except Exception:  # noqa: BLE001
        return Path("data/stage_latency.json")


_lock = threading.Lock()
_cache: Optional[Dict[str, StageLatencyStats]] = None


def _load() -> Dict[str, StageLatencyStats]:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("stage_latency.json unreadable; starting fresh", exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, StageLatencyStats] = {}
    for stage, entry in raw.items():
        if isinstance(entry, dict):
            try:
                out[str(stage)] = StageLatencyStats.from_dict(entry)
            except Exception:  # noqa: BLE001
                continue
    return out


def _save_locked(stats_by_stage: Dict[str, StageLatencyStats]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {s: stat.to_dict() for s, stat in stats_by_stage.items()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.warning("stage_latency flush failed", exc_info=True)


def record_stage_duration(stage: str, duration_seconds: float) -> None:
    """Add one stage outcome to the aggregate.

    Thread-safe. Lazy-loads the store on first call, writes every time.
    Cost of a single call is one file read + write (~few KB). Negligible
    compared to the LLM calls each stage runs.
    """
    if not stage:
        return
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        entry = _cache.get(stage)
        if entry is None:
            entry = StageLatencyStats(stage=stage)
            _cache[stage] = entry
        entry.add(float(duration_seconds))
        _save_locked(_cache)


def snapshot() -> Dict[str, dict]:
    """Read-only view of every stage's stats as dicts. Used by the UI."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        return {s: stat.to_dict() for s, stat in _cache.items()}


def format_human_summary() -> str:
    """One-line-per-stage human summary suitable for Telegram or logs."""
    snap = snapshot()
    if not snap:
        return "(no stage latency data yet)"
    lines = ["Stage latency (across all runs):"]
    # Sort by usual pipeline order so the summary reads top-to-bottom.
    order = [
        "brainstorm", "research", "architect", "designer",
        "code", "contract_verifier", "consistency_reviewer", "reviewer",
        "build_verifier", "boot_verifier", "integration_verifier",
    ]
    seen: set = set()
    ordered: List[str] = []
    for s in order:
        if s in snap:
            ordered.append(s)
            seen.add(s)
    for s in sorted(snap.keys()):
        if s not in seen:
            ordered.append(s)
    for s in ordered:
        d = snap[s]
        n = int(d.get("count") or 0)
        avg = float(d.get("avg_seconds") or 0.0)
        last = float(d.get("last_seconds") or 0.0)
        mn = d.get("min_seconds")
        mx = d.get("max_seconds")
        range_str = f"{int(mn)}–{int(mx)}s" if mn and mx else "—"
        lines.append(
            f"  {s:24} n={n:>3} avg={int(avg):>4}s last={int(last):>4}s "
            f"range={range_str}"
        )
    return "\n".join(lines)
