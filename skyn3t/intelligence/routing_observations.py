from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, cast

from skyn3t.config.settings import get_settings

_lock = threading.Lock()
_cache: Optional[Dict[str, Dict[str, Any]]] = None


def snapshot(*, trajectory_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
            if not _cache:
                warmed = _warm_from_trajectories(trajectory_dir=trajectory_dir)
                if warmed:
                    _cache = warmed
                    _save_locked(_cache)
        return cast(Dict[str, Dict[str, Any]], json.loads(json.dumps(_cache)))


def record_trajectory(trajectory: Dict[str, Any]) -> None:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        _apply_trajectory(_cache, trajectory)
        _save_locked(_cache)


def reset_cache_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


def _store_path() -> Path:
    return Path(get_settings().data_dir) / "routing_observations.json"


def _trajectory_root(explicit: Optional[Path]) -> Path:
    if explicit is not None:
        return explicit
    return Path(get_settings().data_dir) / "trajectories"


def _load() -> Dict[str, Dict[str, Any]]:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    stages = raw.get("stages") if isinstance(raw, dict) else None
    if not isinstance(stages, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for stage, entry in stages.items():
        if isinstance(stage, str) and isinstance(entry, dict):
            out[stage] = entry
    return out


def _save_locked(data: Dict[str, Dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"stages": data}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _warm_from_trajectories(*, trajectory_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    root = _trajectory_root(trajectory_dir)
    if not root.exists():
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for path in sorted(root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            _apply_trajectory(out, record)
    return out


def _apply_trajectory(summary: Dict[str, Dict[str, Any]], record: Dict[str, Any]) -> None:
    from skyn3t.core.model_router import tier_for_backend_model

    llm_events = [
        event
        for event in list(record.get("events") or [])
        if isinstance(event, dict) and event.get("type") == "llm_call"
    ]
    if not llm_events:
        return
    top_level_stage = _normalize_stage(record.get("stage"))
    stage_groups: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
    for event in llm_events:
        event_stage = _normalize_stage(event.get("project_stage")) or top_level_stage
        if event_stage:
            stage_groups[event_stage].append(event)
    if not stage_groups and top_level_stage:
        stage_groups[top_level_stage] = llm_events
    for stage, events in stage_groups.items():
        stage_row = summary.setdefault(
            stage,
            {
                "total_tokens": 0,
                "trajectory_samples": 0,
                "mixed_route_samples": 0,
                "route_stats": {},
            },
        )
        stage_row["trajectory_samples"] = int(stage_row.get("trajectory_samples") or 0) + 1
        tokens = 0
        mapped_tiers = set()
        for event in events:
            try:
                tokens += int(event.get("total_tokens") or 0)
            except (TypeError, ValueError):
                pass
            tier = tier_for_backend_model(
                str(event.get("backend") or ""),
                event.get("model"),
            )
            if tier:
                mapped_tiers.add(tier)
        stage_row["total_tokens"] = int(stage_row.get("total_tokens") or 0) + tokens
        if len(mapped_tiers) != 1:
            stage_row["mixed_route_samples"] = int(stage_row.get("mixed_route_samples") or 0) + 1
            continue
        tier_name = next(iter(mapped_tiers))
        route_stats = stage_row.setdefault("route_stats", {})
        stat = route_stats.setdefault(
            tier_name,
            {
                "tier": tier_name,
                "backend": events[0].get("backend"),
                "model": events[0].get("model"),
                "samples": 0,
                "successes": 0,
                "failures": 0,
                "success_rate": 0.0,
                "total_tokens": 0,
            },
        )
        stat["samples"] = int(stat.get("samples") or 0) + 1
        stat["total_tokens"] = int(stat.get("total_tokens") or 0) + tokens
        if record.get("outcome") == "success":
            stat["successes"] = int(stat.get("successes") or 0) + 1
        elif record.get("outcome") == "failure":
            stat["failures"] = int(stat.get("failures") or 0) + 1
        samples = int(stat.get("samples") or 0)
        stat["success_rate"] = (
            float(stat.get("successes") or 0) / samples if samples else 0.0
        )


def _normalize_stage(value: Any) -> str:
    return str(value or "").strip().lower()
