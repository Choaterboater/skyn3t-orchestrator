"""Cleanup utilities — projects, proposals, auto-branches."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from skyn3t.config.settings import get_settings

logger = logging.getLogger("skyn3t.cli.cleanup")

REPO_ROOT = Path(__file__).resolve().parents[2]
PROPOSALS_DIR = REPO_ROOT / "data" / "proposals"


def _projects_dir() -> Path:
    return get_settings().projects_dir


def _is_git_repo() -> bool:
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _list_projects() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    projects_dir = _projects_dir()
    if not projects_dir.exists():
        return out
    for p in sorted(projects_dir.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        manifest = p / "project.json"
        ts = p.stat().st_mtime
        title = p.name
        if manifest.exists():
            try:
                d = json.loads(manifest.read_text())
                ts = d.get("started_at") or d.get("completed_at") or ts
                title = d.get("title") or title
            except Exception:
                logger.debug("project.json parse failed at %s", manifest, exc_info=True)
        out.append({
            "slug": p.name,
            "title": title,
            "ts": ts,
            "path": str(p),
            "size": _dir_size(p),
        })
    return out


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _list_proposals(only_decided: bool = True) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not PROPOSALS_DIR.exists():
        return out
    folder = PROPOSALS_DIR / ("decided" if only_decided else "pending")
    if not folder.exists():
        return out
    for f in sorted(folder.glob("*.json"), key=lambda x: -x.stat().st_mtime):
        try:
            d = json.loads(f.read_text())
            out.append({
                "id": d.get("id"),
                "kind": d.get("kind"),
                "title": d.get("title", "")[:80],
                "status": d.get("status"),
                "ts": d.get("decided_at") or d.get("created_at") or f.stat().st_mtime,
                "path": str(f),
            })
        except Exception:
            continue
    return out


def _list_auto_branches() -> list[dict[str, Any]]:
    if not _is_git_repo():
        return []
    try:
        r = subprocess.run(
            [
                "git",
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)|%(committerdate:unix)|%(subject)",
                "refs/heads/skyn3t/auto/",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        out: list[dict[str, Any]] = []
        for line in r.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            ref, ts_s, subj = parts
            try:
                ts = float(ts_s)
            except ValueError:
                ts = 0.0
            out.append({"ref": ref, "ts": ts, "subject": subj[:80]})
        return out
    except Exception:
        return []


def preview(
    *,
    projects: bool = True,
    proposals: bool = True,
    branches: bool = True,
    older_than_days: Optional[int] = None,
    keep_last: Optional[int] = None,
) -> dict[str, Any]:
    """Return what WOULD be cleaned, never modifies."""
    cutoff = (time.time() - older_than_days * 86400) if older_than_days else None
    plan: dict[str, Any] = {"projects": [], "proposals": [], "branches": []}

    if projects:
        items = _list_projects()
        if cutoff is not None:
            items = [i for i in items if i["ts"] < cutoff]
        if keep_last is not None:
            items = items[keep_last:]   # already sorted newest-first
        plan["projects"] = items

    if proposals:
        items = _list_proposals(only_decided=True)
        if cutoff is not None:
            items = [i for i in items if i["ts"] < cutoff]
        plan["proposals"] = items

    if branches:
        items = _list_auto_branches()
        if cutoff is not None:
            items = [i for i in items if i["ts"] < cutoff]
        if keep_last is not None:
            items = items[keep_last:]
        plan["branches"] = items

    plan["total_projects"] = len(plan["projects"])
    plan["total_proposals"] = len(plan["proposals"])
    plan["total_branches"] = len(plan["branches"])
    plan["total_bytes"] = sum(p.get("size", 0) for p in plan["projects"])
    return plan


def execute(plan: dict[str, Any]) -> dict[str, Any]:
    """Apply a cleanup plan. Returns counts of what was actually removed."""
    removed: dict[str, Any] = {"projects": 0, "proposals": 0, "branches": 0, "errors": []}
    # Projects
    for item in plan.get("projects", []):
        try:
            shutil.rmtree(item["path"], ignore_errors=False)
            removed["projects"] += 1
        except Exception as e:
            removed["errors"].append(f"project {item.get('slug')}: {e}")
    # Proposals
    for item in plan.get("proposals", []):
        try:
            Path(item["path"]).unlink()
            removed["proposals"] += 1
        except Exception as e:
            removed["errors"].append(f"proposal {item.get('id')}: {e}")
    # Branches — never delete the current branch
    if plan.get("branches"):
        try:
            cur = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                timeout=5,
            ).stdout.strip()
        except Exception:
            cur = ""
        for item in plan["branches"]:
            ref = item["ref"]
            if ref == cur:
                continue
            # Defense-in-depth: never pass a ref starting with `-` to git, even
            # though the listing logic limits scope to refs/heads/skyn3t/auto/.
            # `--` separator below also blocks flag injection.
            if not ref or ref.startswith("-"):
                removed["errors"].append(f"branch {ref}: refusing flag-like ref")
                continue
            try:
                subprocess.run(
                    ["git", "branch", "-D", "--", ref],
                    capture_output=True,
                    text=True,
                    cwd=str(REPO_ROOT),
                    timeout=10,
                    check=True,
                )
                removed["branches"] += 1
            except subprocess.CalledProcessError as e:
                removed["errors"].append(f"branch {ref}: {e.stderr.strip()[:160]}")
    return removed


def delete_project(slug: str) -> dict[str, Any]:
    """Delete a single project by slug. Safe: refuses to traverse outside the projects root."""
    projects_dir = _projects_dir()
    projects_root = projects_dir.resolve()
    p = (projects_dir / slug).resolve()
    # Use relative_to instead of startswith to avoid prefix-collision attacks
    # (e.g. "/foo/projects" vs "/foo/projectsX").
    try:
        p.relative_to(projects_root)
    except ValueError:
        return {"ok": False, "error": "invalid slug"}
    if p == projects_root:
        return {"ok": False, "error": "invalid slug"}
    if not p.exists():
        return {"ok": False, "error": "not found"}
    try:
        shutil.rmtree(p)
        return {"ok": True, "removed": slug}
    except Exception as e:
        return {"ok": False, "error": str(e)}
