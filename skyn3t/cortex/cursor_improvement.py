"""Queue SkyN3t-repo improvement tasks for Cursor IDE agents.

The autonomous fleet improves generated projects under ``PROJECTS_DIR``.
This module writes ``data/cursor_tasks.json`` when the improvement flywheel
detects quality regression or scout finds competitor patterns applicable to
SkyN3t itself.

In Cursor chat, run: **Process cursor_tasks.json**
Or: ``./scripts/cursor_improve.sh`` for the next task + smoke checks.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.cortex.cursor_improvement")

CURSOR_TASKS_FILENAME = "cursor_tasks.json"
MAX_TASKS = 40


def _tasks_path(settings: Any | None = None) -> Path:
    if settings is None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
    return Path(settings.data_dir) / CURSOR_TASKS_FILENAME


def load_tasks(settings: Any | None = None) -> Dict[str, Any]:
    path = _tasks_path(settings)
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("tasks"), list):
                return raw
    except Exception:
        logger.debug("cursor_tasks load failed", exc_info=True)
    return {"tasks": [], "updated_at": 0.0}


def save_tasks(data: Dict[str, Any], settings: Any | None = None) -> None:
    path = _tasks_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = time.time()
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("cursor_tasks save failed")


def enqueue_task(
    *,
    priority: int,
    brief: str,
    files: Optional[List[str]] = None,
    source: str = "manual",
    settings: Any | None = None,
) -> bool:
    """Append a task unless an equivalent brief already exists."""
    text = (brief or "").strip()
    if not text:
        return False

    data = load_tasks(settings)
    tasks: List[Dict[str, Any]] = list(data.get("tasks") or [])
    normalized = " ".join(text.lower().split())
    for existing in tasks:
        if " ".join(str(existing.get("brief") or "").lower().split()) == normalized:
            return False

    tasks.append(
        {
            "priority": int(priority),
            "brief": text[:2000],
            "files": list(files or [])[:20],
            "source": str(source)[:120],
            "created_at": time.time(),
        }
    )
    tasks.sort(key=lambda t: (-int(t.get("priority") or 0), float(t.get("created_at") or 0)))
    data["tasks"] = tasks[-MAX_TASKS:]
    save_tasks(data, settings)
    logger.info("cursor task queued source=%s priority=%s", source, priority)
    return True


def pop_highest_priority_task(settings: Any | None = None) -> Optional[Dict[str, Any]]:
    """Remove and return the highest-priority task."""
    data = load_tasks(settings)
    tasks: List[Dict[str, Any]] = list(data.get("tasks") or [])
    if not tasks:
        return None
    tasks.sort(key=lambda t: (-int(t.get("priority") or 0), float(t.get("created_at") or 0)))
    chosen = tasks.pop(0)
    data["tasks"] = tasks
    save_tasks(data, settings)
    return chosen


def peek_next_task(settings: Any | None = None) -> Optional[Dict[str, Any]]:
    data = load_tasks(settings)
    tasks: List[Dict[str, Any]] = list(data.get("tasks") or [])
    if not tasks:
        return None
    tasks.sort(key=lambda t: (-int(t.get("priority") or 0), float(t.get("created_at") or 0)))
    return dict(tasks[0])


def enqueue_regression_task(
    *,
    stack: str,
    avg_score: float,
    threshold: float,
    samples: int,
    settings: Any | None = None,
) -> bool:
    """Loop D regression → Cursor should investigate SkyN3t quality."""
    brief = (
        f"SkyN3t quality regression on stack `{stack}`: rolling reviewer avg "
        f"{avg_score:.0f} < {threshold:.0f} over {samples} builds. "
        "Investigate Studio pipeline, reviewer thresholds, and code-agent prompts; "
        "ship a small fix with tests."
    )
    return enqueue_task(
        priority=85,
        brief=brief,
        files=[
            "skyn3t/studio/runner.py",
            "skyn3t/agents/reviewer.py",
            "skyn3t/agents/code_agent.py",
            "skyn3t/core/model_router.py",
        ],
        source="continuous_improvement:regression",
        settings=settings,
    )


def enqueue_competitive_task(
    *,
    repo: str,
    pattern: str,
    targets: Optional[List[str]] = None,
    description: str = "",
    settings: Any | None = None,
) -> bool:
    """Scout competitor pattern applicable to SkyN3t repo itself."""
    target_list = targets or []
    target_hint = ", ".join(target_list[:4]) if target_list else "skyn3t/"
    brief = (
        f"Close a Hermes/competitor gap in SkyN3t: adopt «{pattern}» pattern from "
        f"`{repo}` into {target_hint}. Minimal shippable diff + tests; "
        "do not copy competitor code."
    )
    if description.strip():
        brief += f" Scout note: {description.strip()[:200]}"
    return enqueue_task(
        priority=70,
        brief=brief,
        files=target_list[:12] or ["docs/CONTINUE.md"],
        source="competitive_intel:skyn3t",
        settings=settings,
    )


def maybe_enqueue_from_competitive_adaptation(
    repo: str,
    *,
    description: str = "",
    ingested_paths: Optional[List[str]] = None,
    settings: Any | None = None,
) -> bool:
    """Queue a Cursor task when scout ingest matches a catalogued competitor."""
    from skyn3t.cortex.competitive_intel import match_competitor

    match = match_competitor(repo)
    if match is None:
        return False
    patterns = list(match.get("patterns") or [])
    pattern = patterns[0] if patterns else "workflow automation"
    targets = list(match.get("skyn3t_targets") or [])
    return enqueue_competitive_task(
        repo=repo,
        pattern=pattern,
        targets=targets,
        description=description,
        settings=settings,
    )
