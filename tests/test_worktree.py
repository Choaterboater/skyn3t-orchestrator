"""Tests for git worktree helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skyn3t.worktree import (
    ensure_worktree,
    is_git_repo,
    remove_worktree,
    worktree_enabled_for_extra,
)


@pytest.fixture
def scaffold_dir(tmp_path: Path) -> Path:
    return tmp_path / "project" / "scaffold"


def test_worktree_enabled_for_fleet_slot(monkeypatch):
    monkeypatch.setenv("SKYN3T_STUDIO_WORKTREE", "1")
    assert worktree_enabled_for_extra({"fleet_slot": 3}) is True
    monkeypatch.setenv("SKYN3T_STUDIO_WORKTREE", "0")
    assert worktree_enabled_for_extra({"fleet_slot": 3}) is False


def test_worktree_enabled_explicit_extra():
    assert worktree_enabled_for_extra({"worktree": True}) is True
    assert worktree_enabled_for_extra({}) is False


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not installed",
)
def test_ensure_worktree_creates_isolated_path(scaffold_dir: Path):
    info = ensure_worktree(scaffold_dir, track_id="slot-1")
    assert info.worktree_path.exists()
    assert is_git_repo(info.worktree_path)
    assert is_git_repo(scaffold_dir)
    assert info.branch == "skyn3t/slot-1"

    info2 = ensure_worktree(scaffold_dir, track_id="slot-1")
    assert info2.worktree_path == info.worktree_path
    assert info2.created is False

    assert remove_worktree(scaffold_dir, info.worktree_path, force=True) is True
