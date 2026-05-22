"""Persisted stage-routing policy store."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("skyn3t.config.model_routing")


class ModelRoutingStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: Dict[str, str] = self._load()
        self._meta: Dict[str, Dict[str, str]] = self._load_meta()

    def _meta_path(self) -> Path:
        if self.path.suffix:
            return self.path.with_name(f"{self.path.stem}.meta{self.path.suffix}")
        return self.path.with_name(f"{self.path.name}.meta")

    def _load(self) -> Dict[str, str]:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    out: Dict[str, str] = {}
                    for name, tier in loaded.items():
                        if isinstance(name, str) and isinstance(tier, str):
                            out[name.strip().lower()] = tier.strip()
                    return out
            except Exception:
                logger.exception("model routing store read failed")
        return {}

    def _load_meta(self) -> Dict[str, Dict[str, str]]:
        path = self._meta_path()
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    out: Dict[str, Dict[str, str]] = {}
                    for name, meta in loaded.items():
                        if not isinstance(name, str) or not isinstance(meta, dict):
                            continue
                        applied_via = str(meta.get("applied_via") or "").strip().lower()
                        if applied_via in {"manual", "recommendation"}:
                            out[name.strip().lower()] = {"applied_via": applied_via}
                    return out
            except Exception:
                logger.exception("model routing metadata store read failed")
        return {}

    def _atomic_write(self, path: Path, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, indent=2, sort_keys=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _save(self) -> None:
        try:
            self._atomic_write(self.path, self._data)
            meta_path = self._meta_path()
            if self._meta:
                self._atomic_write(meta_path, self._meta)
            elif meta_path.exists():
                meta_path.unlink()
        except Exception:
            logger.exception("model routing store write failed")

    def _entries_snapshot(self) -> Dict[str, Dict[str, Optional[str]]]:
        return {
            stage: {
                "tier": tier,
                "applied_via": (self._meta.get(stage) or {}).get("applied_via"),
            }
            for stage, tier in self._data.items()
        }

    def all(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._data)

    def entries(self) -> Dict[str, Dict[str, Optional[str]]]:
        with self._lock:
            return self._entries_snapshot()

    def set_entries(self, updates: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Optional[str]]]:
        clean_tiers: Dict[str, str] = {}
        clean_meta: Dict[str, Optional[str]] = {}
        for stage, entry in (updates or {}).items():
            stage_name = str(stage).strip().lower()
            if not stage_name or not isinstance(entry, dict):
                continue
            tier_name = str(entry.get("tier") or "").strip()
            if not tier_name:
                continue
            clean_tiers[stage_name] = tier_name
            if "applied_via" in entry:
                applied_via = str(entry.get("applied_via") or "").strip().lower()
                clean_meta[stage_name] = (
                    applied_via if applied_via in {"manual", "recommendation"} else None
                )
        with self._lock:
            self._data.update(clean_tiers)
            for stage_name, meta_applied_via in clean_meta.items():
                if meta_applied_via is None:
                    self._meta.pop(stage_name, None)
                else:
                    self._meta[stage_name] = {"applied_via": meta_applied_via}
            self._save()
            return self._entries_snapshot()

    def set_many(
        self,
        updates: Dict[str, str],
        *,
        applied_via: Optional[str] = None,
    ) -> Dict[str, Dict[str, Optional[str]]]:
        payload: Dict[str, Dict[str, Any]] = {}
        for stage, tier in (updates or {}).items():
            stage_name = str(stage).strip().lower()
            tier_name = str(tier).strip()
            if not stage_name or not tier_name:
                continue
            entry: Dict[str, Any] = {"tier": tier_name}
            if applied_via is not None:
                entry["applied_via"] = applied_via
            payload[stage_name] = entry
        return self.set_entries(payload)

    def delete(self, stage: str) -> bool:
        key = str(stage or "").strip().lower()
        if not key:
            return False
        with self._lock:
            existed = key in self._data
            self._data.pop(key, None)
            self._meta.pop(key, None)
            self._save()
            return existed


_store: Optional[ModelRoutingStore] = None


def get_model_routing_store() -> ModelRoutingStore:
    global _store
    if _store is None:
        from skyn3t.config.settings import get_settings

        _store = ModelRoutingStore(get_settings().model_routing_path)
    return _store
