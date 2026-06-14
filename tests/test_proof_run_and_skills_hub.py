"""Tests for post-build proof runs and Skills Hub."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyn3t.config.settings import get_settings
from skyn3t.cortex.autonomous_loop import AutonomousCoordinator
from skyn3t.intelligence.skills_hub import hub_roots, install_from_hub, list_hub_entries
from skyn3t.studio.proof_run import resolve_scaffold_dir, run_scaffold_proof


@pytest.fixture(autouse=True)
def _clear_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "skyn3t.cortex.autonomous_loop.STATE_PATH",
        tmp_path / "data" / "autonomous_loop_state.json",
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_run_scaffold_proof_python_ok(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "main.py").write_text("print('ok')\n", encoding="utf-8")

    proof = await run_scaffold_proof(scaffold, strict=True)
    assert proof["ok"] is True
    assert proof["verdict"] == "yes"
    assert proof["stack"] == "python"


@pytest.mark.asyncio
async def test_run_scaffold_proof_python_syntax_fail(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "main.py").write_text("def broken(:\n", encoding="utf-8")

    proof = await run_scaffold_proof(scaffold, strict=True)
    assert proof["ok"] is False
    assert proof["verdict"] == "no"


@pytest.mark.asyncio
async def test_run_scaffold_proof_missing_dir(tmp_path):
    proof = await run_scaffold_proof(tmp_path / "missing", strict=True)
    assert proof["ok"] is False
    assert "not found" in proof["summary"].lower()


def test_resolve_scaffold_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "projects"))
    get_settings.cache_clear()
    path = resolve_scaffold_dir("my-app")
    assert path == (tmp_path / "projects" / "my-app" / "scaffold").resolve()


def test_list_hub_entries_includes_seed():
    catalog = list_hub_entries()
    repo_root = Path(__file__).resolve().parents[1]
    assert str((repo_root / "examples" / "skills_seed").resolve()) in (
        catalog.get("roots") or []
    )
    assert (catalog.get("total") or 0) >= 1


def test_hub_roots_are_repo_relative_when_cwd_changes(monkeypatch, tmp_path):
    monkeypatch.delenv("SKYN3T_SKILLS_HUB_PATHS", raising=False)
    monkeypatch.chdir(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]

    assert hub_roots() == [
        (repo_root / "examples" / "skills_seed").resolve(),
        (repo_root / "skills").resolve(),
    ]


def test_hub_roots_resolve_relative_env_from_repo_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_SKILLS_HUB_PATHS", "custom_hub,examples/skills_seed")
    monkeypatch.chdir(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]

    assert hub_roots() == [
        (repo_root / "custom_hub").resolve(),
        (repo_root / "examples" / "skills_seed").resolve(),
    ]


def test_install_from_hub_imports_seed_skills(tmp_path, monkeypatch):
    from skyn3t.intelligence.skill_library import get_default_library
    lib = get_default_library()
    for skill in lib.all():
        lib.delete(skill.slug)
    monkeypatch.setenv("SKYN3T_SKILLS_HUB_PATHS", str(tmp_path / "hub"))
    get_settings.cache_clear()
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "demo-skill.md").write_text(
        "---\nname: demo-skill\ntags: [test]\n---\n\nAlways include /health.\n",
        encoding="utf-8",
    )

    result = install_from_hub(only_missing=True)
    assert "demo-skill" in (result.get("installed") or [])

    from skyn3t.intelligence.skill_library import get_default_library

    assert any(s.slug == "demo-skill" for s in get_default_library().all())


@pytest.mark.asyncio
async def test_autonomous_proof_failed_enqueues_brief(monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_PROOF_RUN", "1")
    get_settings.cache_clear()

    orch = SimpleNamespace(
        memory_store=MagicMock(),
        get_studio_runner=lambda: None,
        _repo_scout=None,
    )
    bus = MagicMock()
    coord = AutonomousCoordinator(orch, bus)
    coord._offer_brief = AsyncMock(return_value=True)  # type: ignore[method-assign]

    fake_proof = {
        "ok": False,
        "summary": "npm run build failed",
        "stack": "node",
        "stderr": "Module not found",
        "failure_hint": "missing dependency",
    }
    with patch(
        "skyn3t.studio.proof_run.run_proof_for_slug",
        new=AsyncMock(return_value=fake_proof),
    ):
        await coord._on_project_completed(
            {
                "slug": "auto-test-123",
                "autonomous": True,
                "status": "done",
                "verdict": "go",
                "reviewer_score": 92,
                "stack": "node",
            }
        )

    assert coord.state.last_proof_ok is False
    assert coord.state.last_proof_slug == "auto-test-123"
    coord._offer_brief.assert_awaited()


@pytest.mark.asyncio
async def test_autonomous_proof_passed_skips_enqueue(monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    get_settings.cache_clear()

    orch = SimpleNamespace(
        memory_store=MagicMock(),
        get_studio_runner=lambda: None,
        _repo_scout=None,
    )
    bus = MagicMock()
    coord = AutonomousCoordinator(orch, bus)
    coord._offer_brief = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with patch(
        "skyn3t.studio.proof_run.run_proof_for_slug",
        new=AsyncMock(
            return_value={
                "ok": True,
                "summary": "node scaffold OK",
                "stack": "node",
            }
        ),
    ):
        await coord._on_project_completed(
            {
                "slug": "auto-ok",
                "autonomous": True,
                "status": "done",
                "verdict": "go",
                "reviewer_score": 92,
            }
        )

    assert coord.state.last_proof_ok is True
    coord._offer_brief.assert_not_awaited()
