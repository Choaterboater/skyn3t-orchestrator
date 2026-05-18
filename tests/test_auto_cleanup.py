"""Tests for skyn3t.cortex.auto_cleanup.AutoCleanup.

Covers the three sweeps (failed projects, decided proposals, stale branches)
plus the no-op case where nothing has aged out.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from skyn3t.cortex.auto_cleanup import AutoCleanup


def _write_manifest(slug_dir: Path, *, status: str, completed_at: float) -> None:
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / "manifest.json").write_text(
        json.dumps({"slug": slug_dir.name, "status": status, "completed_at": completed_at})
    )


def test_failed_projects_older_than_threshold_are_removed(tmp_path):
    projects = tmp_path / "projects"
    old_completed = time.time() - (10 * 86400)  # 10 days ago
    fresh = time.time() - (1 * 86400)            # 1 day ago

    _write_manifest(projects / "old-failed", status="failed", completed_at=old_completed)
    _write_manifest(projects / "fresh-failed", status="failed", completed_at=fresh)
    _write_manifest(projects / "old-done", status="done", completed_at=old_completed)

    ac = AutoCleanup(
        event_bus=None,
        projects_root=projects,
        proposals_root=tmp_path / "proposals_unused",
        repo_root=tmp_path / "no-git-here",
        failed_project_age_days=7.0,
    )
    summary = ac.run_once()
    assert summary["projects_removed"] == 1
    assert not (projects / "old-failed").exists()
    assert (projects / "fresh-failed").exists()
    assert (projects / "old-done").exists()


def test_decided_proposals_older_than_threshold_are_removed(tmp_path):
    proposals = tmp_path / "proposals"
    decided = proposals / "decided"
    decided.mkdir(parents=True)
    cutoff_old = time.time() - (40 * 86400)
    cutoff_new = time.time() - (5 * 86400)

    (decided / "old.json").write_text(json.dumps({"id": "old", "applied_at": cutoff_old}))
    (decided / "new.json").write_text(json.dumps({"id": "new", "applied_at": cutoff_new}))

    ac = AutoCleanup(
        event_bus=None,
        projects_root=tmp_path / "projects_unused",
        proposals_root=proposals,
        repo_root=tmp_path / "no-git-here",
        decided_proposal_age_days=30.0,
    )
    summary = ac.run_once()
    assert summary["proposals_removed"] == 1
    assert not (decided / "old.json").exists()
    assert (decided / "new.json").exists()


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, cwd=str(cwd), timeout=10)


@pytest.fixture
def git_repo(tmp_path):
    """A minimal git repo with one initial commit on a non-auto branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["git", "init", "-q", "-b", "main"], repo)
    _git(["git", "config", "user.email", "t@t"], repo)
    _git(["git", "config", "user.name", "t"], repo)
    (repo / "README").write_text("x")
    _git(["git", "add", "README"], repo)
    _git(["git", "commit", "-q", "-m", "init"], repo)
    return repo


def test_stale_auto_branches_removed_current_preserved(git_repo):
    # Create a skyn3t/auto/stale branch with a single commit, then a fresh one
    # we'll keep current (checked out).
    _git(["git", "checkout", "-q", "-b", "skyn3t/auto/stale"], git_repo)
    (git_repo / "f").write_text("x")
    _git(["git", "add", "f"], git_repo)
    _git(["git", "commit", "-q", "-m", "stale-commit"], git_repo)
    _git(["git", "checkout", "-q", "main"], git_repo)
    _git(["git", "checkout", "-q", "-b", "skyn3t/auto/current"], git_repo)
    (git_repo / "g").write_text("x")
    _git(["git", "add", "g"], git_repo)
    _git(["git", "commit", "-q", "-m", "current-commit"], git_repo)
    # Currently on skyn3t/auto/current. stale_branch_age_days=0 means everything
    # qualifies as stale — but the current branch must be preserved.

    ac = AutoCleanup(
        event_bus=None,
        projects_root=git_repo / "projects_unused",
        proposals_root=git_repo / "proposals_unused",
        repo_root=git_repo,
        stale_branch_age_days=0.0,
    )
    summary = ac.run_once()
    assert summary["branches_removed"] == 1
    # Verify stale is gone, current is still there.
    branches = _git(["git", "branch", "--list"], git_repo).stdout
    assert "skyn3t/auto/stale" not in branches
    assert "skyn3t/auto/current" in branches


def test_run_once_publishes_event_when_event_bus_provided(tmp_path):
    """Smoke: passing an event_bus should not raise and should publish."""
    from skyn3t.core.events import EventBus, EventType

    captured = []
    bus = EventBus()
    bus.subscribe(captured.append, EventType.SYSTEM_ALERT)

    ac = AutoCleanup(
        event_bus=bus,
        projects_root=tmp_path / "projects",
        proposals_root=tmp_path / "proposals",
        repo_root=tmp_path / "no-git",
    )
    # Manually invoke the publish path (run_once doesn't publish — only the
    # background loop does).
    ac._publish({"projects_removed": 0, "proposals_removed": 0, "branches_removed": 0, "errors": []})
    assert any(
        e.source == "auto_cleanup" and e.payload.get("kind") == "AUTO_CLEANUP_RESULT"
        for e in captured
    )


def test_skip_failed_project_without_completed_at(tmp_path):
    """A failed project with no completed_at shouldn't be deleted (we can't
    age it). Better to leak one entry than to delete something we can't date."""
    projects = tmp_path / "projects"
    slug_dir = projects / "no-ts"
    slug_dir.mkdir(parents=True)
    (slug_dir / "manifest.json").write_text(json.dumps({"status": "failed"}))

    ac = AutoCleanup(
        event_bus=None,
        projects_root=projects,
        proposals_root=tmp_path / "proposals_unused",
        repo_root=tmp_path / "no-git-here",
        failed_project_age_days=0.0,  # everything else would age out
    )
    summary = ac.run_once()
    assert summary["projects_removed"] == 0
    assert slug_dir.exists()
