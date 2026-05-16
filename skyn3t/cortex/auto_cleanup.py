"""Auto-cleanup scheduler — drops stale state on a timer.

What it cleans:
- failed projects older than ``failed_project_age_days`` (default 7d)
- decided proposals (status=applied | rejected) older than
  ``decided_proposal_age_days`` (default 30d)
- skyn3t/auto/* git branches with no commits in ``stale_branch_age_days``
  (default 14d) that aren't currently checked out

The cleaner runs once at startup (after a warm-up grace period) then
every ``interval_seconds`` (default 24h). Each pass publishes a
``SYSTEM_ALERT`` with ``kind="AUTO_CLEANUP_RESULT"`` summarizing what
it removed, so the dashboard can show what happened.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("skyn3t.cortex.auto_cleanup")

# Repo root: /.../jarvis (this file lives at jarvis/skyn3t/cortex/auto_cleanup.py)
REPO_ROOT = Path(__file__).resolve().parents[2]


class AutoCleanup:
    """Periodic janitor for failed projects, old proposals, and dead branches.

    Construction is cheap (no I/O). Call ``start()`` from the orchestrator's
    boot path to begin the schedule. ``stop()`` cancels the loop.
    """

    def __init__(
        self,
        event_bus: Any = None,
        *,
        projects_root: Optional[Path] = None,
        proposals_root: Optional[Path] = None,
        interval_seconds: float = 86400.0,
        warmup_seconds: float = 60.0,
        failed_project_age_days: float = 7.0,
        decided_proposal_age_days: float = 30.0,
        stale_branch_age_days: float = 14.0,
        repo_root: Optional[Path] = None,
    ):
        self.event_bus = event_bus
        self.projects_root = Path(projects_root or REPO_ROOT / "projects")
        self.proposals_root = Path(proposals_root or REPO_ROOT / "data" / "proposals")
        self.repo_root = Path(repo_root or REPO_ROOT)
        self.interval_seconds = float(interval_seconds)
        self.warmup_seconds = float(warmup_seconds)
        self.failed_project_age_days = float(failed_project_age_days)
        self.decided_proposal_age_days = float(decided_proposal_age_days)
        self.stale_branch_age_days = float(stale_branch_age_days)
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Begin the periodic cleanup loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the scheduled cleanup loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        # Warmup grace period: don't run on boot before the system has
        # finished registering agents / loading state.
        try:
            await asyncio.sleep(self.warmup_seconds)
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                result = await asyncio.to_thread(self.run_once)
                self._publish(result)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("auto-cleanup pass failed")
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Public: synchronous one-shot pass (useful for tests + manual runs)
    # ------------------------------------------------------------------

    def run_once(self) -> Dict[str, Any]:
        """Run one cleanup pass. Returns a summary of what was removed."""
        summary: Dict[str, Any] = {
            "projects_removed": 0,
            "proposals_removed": 0,
            "branches_removed": 0,
            "skills_archived": 0,
            "errors": [],
        }
        try:
            summary["projects_removed"] = self._sweep_failed_projects()
        except Exception as e:
            summary["errors"].append(f"projects: {e}")
        try:
            summary["proposals_removed"] = self._sweep_old_proposals()
        except Exception as e:
            summary["errors"].append(f"proposals: {e}")
        try:
            summary["branches_removed"] = self._sweep_stale_branches()
        except Exception as e:
            summary["errors"].append(f"branches: {e}")
        try:
            summary["skills_archived"] = self._sweep_skills()
        except Exception as e:
            summary["errors"].append(f"skills: {e}")
        logger.info("auto-cleanup pass: %s", summary)
        return summary

    def _sweep_skills(self) -> int:
        """Curator pass on the skill library — Hermes-equivalent. Drops
        stale or hurtful skills, preserves pinned ones. Returns the count
        of skills archived."""
        try:
            from skyn3t.intelligence.skill_library import get_default_library
        except Exception:
            return 0
        result = get_default_library().curate()
        return len(result.get("archived") or [])

    # ------------------------------------------------------------------
    # Sweeps
    # ------------------------------------------------------------------

    def _sweep_failed_projects(self) -> int:
        """Delete project directories whose manifest.json status='failed' and
        completed_at is older than failed_project_age_days."""
        if not self.projects_root.exists():
            return 0
        cutoff = time.time() - (self.failed_project_age_days * 86400.0)
        removed = 0
        for child in self.projects_root.iterdir():
            if not child.is_dir():
                continue
            manifest_path = child / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
            except Exception:
                continue
            if manifest.get("status") != "failed":
                continue
            completed_at = manifest.get("completed_at") or manifest.get("created_at") or 0
            try:
                completed_at = float(completed_at)
            except Exception:
                continue
            if completed_at and completed_at < cutoff:
                try:
                    # Path-safety: only remove dirs strictly under projects_root.
                    resolved = child.resolve()
                    resolved.relative_to(self.projects_root.resolve())
                except Exception:
                    continue
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed

    def _sweep_old_proposals(self) -> int:
        """Delete decided/{applied,rejected}/*.json older than threshold."""
        decided = self.proposals_root / "decided"
        if not decided.exists():
            return 0
        cutoff = time.time() - (self.decided_proposal_age_days * 86400.0)
        removed = 0
        for path in decided.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except Exception:
                continue
            ts = data.get("decided_at") or data.get("applied_at") or data.get("created_at") or 0
            try:
                ts = float(ts)
            except Exception:
                continue
            if ts and ts < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def _sweep_stale_branches(self) -> int:
        """Delete local skyn3t/auto/* branches whose tip commit is older
        than threshold AND that aren't currently checked out."""
        try:
            current = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, cwd=str(self.repo_root), timeout=5,
            ).stdout.strip()
        except Exception:
            return 0
        try:
            listing = subprocess.run(
                ["git", "for-each-ref",
                 "--format=%(refname:short) %(committerdate:unix)",
                 "refs/heads/skyn3t/auto/"],
                capture_output=True, text=True, cwd=str(self.repo_root), timeout=10,
            )
        except Exception:
            return 0
        if listing.returncode != 0:
            return 0
        cutoff = time.time() - (self.stale_branch_age_days * 86400.0)
        removed = 0
        for line in listing.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            ref, ts_raw = parts[0], parts[-1]
            if ref == current:
                continue
            if not ref or ref.startswith("-"):
                continue
            try:
                ts = float(ts_raw)
            except Exception:
                continue
            if ts > cutoff:
                continue
            try:
                proc = subprocess.run(
                    ["git", "branch", "-D", "--", ref],
                    capture_output=True, text=True,
                    cwd=str(self.repo_root), timeout=10,
                )
                if proc.returncode == 0:
                    removed += 1
            except Exception:
                continue
        return removed

    # ------------------------------------------------------------------
    # Event publishing
    # ------------------------------------------------------------------

    def _publish(self, summary: Dict[str, Any]) -> None:
        if self.event_bus is None:
            return
        try:
            from skyn3t.core.events import Event, EventType
            self.event_bus.publish(
                Event(
                    event_type=EventType.SYSTEM_ALERT,
                    source="auto_cleanup",
                    payload={"kind": "AUTO_CLEANUP_RESULT", **summary},
                )
            )
        except Exception:
            logger.debug("auto-cleanup publish failed", exc_info=True)
