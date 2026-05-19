"""Human approval gate for StudioRunner stage handoffs.

Reads/writes two JSON files under ``data/``:

* ``approval_gates.json`` — config: which agents gate, notify channels,
  whether the system is globally disabled, graduation threshold.
* ``approval_skill.json`` — per-(brief_shape, agent) counter of
  consecutive clean approvals. After ``graduate_after`` clean approves
  for the same brief-shape + stage, ``should_gate`` returns False so the
  handoff resumes automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from skyn3t.config.settings import get_settings

logger = logging.getLogger(__name__)


_CONFIG_PATH: Optional[Path] = None
_SKILL_PATH: Optional[Path] = None


def _config_path() -> Path:
    if _CONFIG_PATH is not None:
        return _CONFIG_PATH
    return get_settings().data_dir / "approval_gates.json"


def _skill_path() -> Path:
    if _SKILL_PATH is not None:
        return _SKILL_PATH
    return get_settings().data_dir / "approval_skill.json"


_DEFAULT_CONFIG: Dict[str, Any] = {
    "gates": ["ArchitectAgent"],
    "notify": {"discord_webhook": ""},
    "disabled": False,
    "graduate_after": 5,
}


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_gate_config() -> Dict[str, Any]:
    path = _config_path()
    if not path.exists():
        _atomic_write(path, _DEFAULT_CONFIG)
        return dict(_DEFAULT_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("approval_gates.json unreadable; falling back to defaults", exc_info=True)
        return dict(_DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return dict(_DEFAULT_CONFIG)
    merged = {**_DEFAULT_CONFIG, **raw}
    if not isinstance(merged.get("gates"), list):
        merged["gates"] = list(_DEFAULT_CONFIG["gates"])
    if not isinstance(merged.get("notify"), dict):
        merged["notify"] = dict(_DEFAULT_CONFIG["notify"])
    return merged


def save_gate_config(cfg: Dict[str, Any]) -> None:
    if not isinstance(cfg, dict):
        raise ValueError("config must be a dict")
    merged = {**_DEFAULT_CONFIG, **cfg}
    merged["gates"] = [str(g) for g in (merged.get("gates") or []) if isinstance(g, str) and g.strip()]
    notify = merged.get("notify") or {}
    if not isinstance(notify, dict):
        notify = {}
    notify["discord_webhook"] = str(notify.get("discord_webhook") or "")
    merged["notify"] = notify
    merged["disabled"] = bool(merged.get("disabled", False))
    try:
        merged["graduate_after"] = int(merged.get("graduate_after", 5))
    except (TypeError, ValueError):
        merged["graduate_after"] = 5
    _atomic_write(_config_path(), merged)


def _load_skill() -> Dict[str, Any]:
    path = _skill_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("approval_skill.json unreadable; resetting", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _save_skill(data: Dict[str, Any]) -> None:
    _atomic_write(_skill_path(), data)


_BRIEF_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def brief_shape(brief: str) -> str:
    """Stable hash of the brief's "shape" — first 200 chars after
    lowercasing and collapsing non-alphanumerics. Two briefs that differ
    only in whitespace or punctuation hash identically."""
    text = (brief or "").lower()
    normalized = _BRIEF_NORMALIZE_RE.sub(" ", text).strip()
    normalized = normalized[:200]
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def is_graduated(brief: str, agent_name: str, threshold: int) -> bool:
    if threshold <= 0:
        return False
    skill = _load_skill()
    entry = skill.get(brief_shape(brief), {}).get(agent_name, {})
    return int(entry.get("approved_unchanged", 0)) >= threshold


def should_gate(
    agent_name: str,
    brief: str,
    *,
    autonomy: Optional[str] = None,
) -> bool:
    """True if the pipeline should halt for human approval after
    ``agent_name`` finishes.

    ``autonomy`` honors the project's mission_setup choice. From
    skyn3t/studio/mission_setup.py the ``move_fast`` mode is
    documented as "Do not pause for kickoff clarification questions.
    Make reasonable assumptions, keep momentum, and only stop if the
    work is truly blocked." Approval gates squarely contradict that,
    so under ``move_fast`` we skip ALL gates regardless of the global
    approval_gates.json config or graduation status.
    """
    if (autonomy or "").strip().lower() == "move_fast":
        return False
    cfg = load_gate_config()
    if cfg.get("disabled"):
        return False
    gates = cfg.get("gates") or []
    if agent_name not in gates:
        return False
    threshold = int(cfg.get("graduate_after", 5))
    if is_graduated(brief, agent_name, threshold):
        return False
    return True


def record_decision(
    brief: str, agent_name: str, decision: str, edited: bool
) -> None:
    """Increment the clean-approve counter on `approve + edited=False`,
    reset to 0 on reject or any edits."""
    skill = _load_skill()
    shape = brief_shape(brief)
    bucket = skill.setdefault(shape, {})
    entry = bucket.setdefault(
        agent_name, {"approved_unchanged": 0, "last_updated": 0}
    )
    if decision == "approve" and not edited:
        entry["approved_unchanged"] = int(entry.get("approved_unchanged", 0)) + 1
    else:
        entry["approved_unchanged"] = 0
    entry["last_updated"] = time.time()
    _save_skill(skill)
