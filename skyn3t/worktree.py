"""Git worktree helpers for parallel CodeAgent isolation (Railyard/ATC pattern).

Creates per-track worktrees under ``<artifact_dir>/.worktrees/<track_id>`` so
fleet slots or retry passes can edit scaffold code without stomping each other.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("skyn3t.worktree")

_DEFAULT_EMAIL = "skyn3t@local"
_DEFAULT_NAME = "SkyN3t"


@dataclass(frozen=True)
class WorktreeInfo:
    repo_path: Path
    worktree_path: Path
    branch: str
    created: bool


def is_git_repo(path: Path) -> bool:
    """Return True when ``path`` is inside a git work tree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_git(cwd: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _ensure_initial_commit(repo_path: Path) -> None:
    """Create an empty initial commit when the repo has no HEAD yet."""
    head = _run_git(repo_path, "rev-parse", "--verify", "HEAD")
    if head.returncode == 0:
        return
    keep = repo_path / ".gitkeep"
    if not keep.exists():
        keep.write_text("", encoding="utf-8")
    _run_git(repo_path, "add", "-A")
    _run_git(repo_path, "commit", "-m", "chore: skyn3t scaffold init", "--allow-empty")


def ensure_worktree(
    scaffold_dir: Path,
    *,
    track_id: str,
    worktrees_root: Optional[Path] = None,
) -> WorktreeInfo:
    """Ensure a git worktree exists for ``track_id`` and return its path.

    ``scaffold_dir`` is the main checkout (``artifact_dir/scaffold``). Worktrees
    live under ``artifact_dir/.worktrees/<track_id>`` by default.
    """
    scaffold_dir = scaffold_dir.resolve()
    scaffold_dir.mkdir(parents=True, exist_ok=True)
    safe_track = "".join(c if c.isalnum() or c in "-_" else "-" for c in track_id.strip())[:48]
    if not safe_track:
        safe_track = "default"

    wt_root = (worktrees_root or scaffold_dir.parent / ".worktrees").resolve()
    wt_path = wt_root / safe_track
    branch = f"skyn3t/{safe_track}"
    created = False

    if not is_git_repo(scaffold_dir):
        init = _run_git(scaffold_dir, "init")
        if init.returncode != 0:
            raise RuntimeError(f"git init failed: {init.stderr.strip() or init.stdout}")
        _run_git(scaffold_dir, "config", "user.email", _DEFAULT_EMAIL)
        _run_git(scaffold_dir, "config", "user.name", _DEFAULT_NAME)
        _ensure_initial_commit(scaffold_dir)

    if wt_path.exists() and is_git_repo(wt_path):
        return WorktreeInfo(
            repo_path=scaffold_dir,
            worktree_path=wt_path,
            branch=branch,
            created=False,
        )

    wt_root.mkdir(parents=True, exist_ok=True)
    if wt_path.exists():
        import shutil

        shutil.rmtree(wt_path, ignore_errors=True)

    add = _run_git(
        scaffold_dir,
        "worktree",
        "add",
        "-B",
        branch,
        str(wt_path),
    )
    if add.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {add.stderr.strip() or add.stdout}")
    created = True
    logger.info("worktree ready track=%s path=%s branch=%s", safe_track, wt_path, branch)
    return WorktreeInfo(
        repo_path=scaffold_dir,
        worktree_path=wt_path,
        branch=branch,
        created=created,
    )


def remove_worktree(scaffold_dir: Path, worktree_path: Path, *, force: bool = False) -> bool:
    """Remove a worktree created by :func:`ensure_worktree`."""
    scaffold_dir = scaffold_dir.resolve()
    worktree_path = worktree_path.resolve()
    args: List[str] = ["worktree", "remove", str(worktree_path)]
    if force:
        args.insert(2, "--force")
    result = _run_git(scaffold_dir, *args)
    return result.returncode == 0


def worktree_enabled_for_extra(extra: Optional[dict]) -> bool:
    """True when Studio ``extra`` or env requests worktree isolation."""
    import os

    if isinstance(extra, dict):
        if extra.get("worktree") is True:
            return True
        if extra.get("fleet_slot") is not None and os.environ.get(
            "SKYN3T_STUDIO_WORKTREE", "1"
        ).strip().lower() not in ("0", "off", "false", "no"):
            return True
    raw = os.environ.get("SKYN3T_STUDIO_WORKTREE", "").strip().lower()
    return raw in ("1", "on", "true", "yes")
