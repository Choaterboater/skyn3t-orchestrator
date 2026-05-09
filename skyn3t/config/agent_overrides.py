"""Per-agent override store, persisted to data/agent_overrides.json."""
from __future__ import annotations
import json, logging, threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("skyn3t.config.overrides")

ALLOWED_KEYS = {
    "backend", "model", "provider", "system_prompt",
    "temperature", "max_tokens", "capabilities", "enabled",
}


class AgentOverrideStore:
    def __init__(self, path: Path | str = "data/agent_overrides.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                logger.exception("override store read failed")
        return {}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        except Exception:
            logger.exception("override store write failed")

    def get(self, name: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data.get(name, {}))

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def set(self, name: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        clean = {k: v for k, v in (patch or {}).items() if k in ALLOWED_KEYS}
        with self._lock:
            cur = dict(self._data.get(name, {}))
            cur.update(clean)
            self._data[name] = cur
            self._save()
            return dict(cur)

    def delete(self, name: str) -> None:
        with self._lock:
            self._data.pop(name, None)
            self._save()


_store: Optional[AgentOverrideStore] = None


def get_override_store() -> AgentOverrideStore:
    global _store
    if _store is None:
        _store = AgentOverrideStore()
    return _store
