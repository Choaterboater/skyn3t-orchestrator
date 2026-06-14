from __future__ import annotations

from skyn3t.core.events import EventBus
from skyn3t.studio.runner import StudioRunner


def test_real_project_scorecard_passes_with_core_contract(tmp_path):
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    manifest = {
        "status": "done",
        "quality_summary": {"source": "reviewer", "verdict": "go", "score": 92},
        "build_verification": {"verdict": "yes"},
        "boot_verification": {"verdict": "yes"},
        "integration_verification": {"verdict": "yes"},
        "injected_skills": ["node-winning-shape"],
        "injected_learnings": ["node winning shape"],
        "lessons_count": 1,
    }

    scorecard = runner._build_real_project_scorecard(manifest)

    assert scorecard["passed"] is True
    assert scorecard["score"] == 100
    assert scorecard["penalties"] == {}


def test_real_project_scorecard_explains_build_failure(tmp_path):
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    manifest = {
        "status": "failed",
        "quality_summary": {"source": "reviewer", "verdict": "no-go", "score": 49},
        "build_verification": {"verdict": "no"},
        "injected_learnings": [],
        "lessons_count": 0,
    }

    scorecard = runner._build_real_project_scorecard(manifest)

    assert scorecard["passed"] is False
    assert scorecard["penalties"]["build_integrity"] == 30
    assert scorecard["penalties"]["reviewer"] > 0
    assert "learnings" in scorecard["penalties"]
    assert any("build verifier" in reason.lower() for reason in scorecard["reasons"])


def test_real_project_scorecard_flags_deterministic_fallback(tmp_path):
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    manifest = {
        "status": "done",
        "quality_summary": {"source": "reviewer", "verdict": "go", "score": 95},
        "build_verification": {"verdict": "yes"},
        "llm_trace": {"backend": "deterministic"},
        "injected_learnings": ["node winning shape"],
        "lessons_count": 1,
    }

    scorecard = runner._build_real_project_scorecard(manifest)

    assert scorecard["passed"] is False
    assert scorecard["penalties"]["llm_fallback"] == 25


def test_real_project_scorecard_recomputes_after_late_failure(tmp_path):
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    manifest = {
        "status": "done",
        "quality_summary": {"source": "reviewer", "verdict": "go", "score": 95},
        "build_verification": {"verdict": "yes"},
        "injected_learnings": ["node winning shape"],
        "lessons_count": 1,
    }
    manifest["real_project_scorecard"] = runner._build_real_project_scorecard(manifest)
    assert manifest["real_project_scorecard"]["passed"] is True

    manifest["status"] = "failed"
    manifest["build_verification"] = {"verdict": "no"}
    manifest["real_project_scorecard"] = runner._build_real_project_scorecard(manifest)

    assert manifest["real_project_scorecard"]["passed"] is False
    assert manifest["real_project_scorecard"]["penalties"]["build_integrity"] == 30
