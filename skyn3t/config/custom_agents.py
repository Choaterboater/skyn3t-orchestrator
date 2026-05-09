"""Persistent store for user-created custom agents."""
from __future__ import annotations
import json, logging, threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.config.custom_agents")

ALLOWED_KEYS = {
    "name", "base_type", "backend", "model", "provider",
    "system_prompt", "temperature", "max_tokens", "capabilities", "enabled",
}


class CustomAgentStore:
    def __init__(self, path: Path | str = "data/custom_agents.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                logger.exception("custom_agents read failed")
        return {}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        except Exception:
            logger.exception("custom_agents write failed")

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._data.values()]

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            v = self._data.get(name)
            return dict(v) if v else None

    def upsert(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        clean = {k: v for k, v in (spec or {}).items() if k in ALLOWED_KEYS}
        name = clean.get("name")
        if not name:
            raise ValueError("name required")
        with self._lock:
            self._data[name] = clean
            self._save()
            return dict(clean)

    def delete(self, name: str) -> bool:
        with self._lock:
            existed = name in self._data
            self._data.pop(name, None)
            self._save()
            return existed


_store: Optional[CustomAgentStore] = None


def get_custom_store() -> CustomAgentStore:
    global _store
    if _store is None:
        _store = CustomAgentStore()
    return _store
