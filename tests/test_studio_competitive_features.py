"""Studio token budget and pipeline checkpoint behavior."""

import json

import pytest

from skyn3t.studio.runner import StudioRunner


@pytest.fixture
def runner(tmp_path, monkeypatch):
    projects_root = tmp_path / "projects"
    projects_root.mkdir(parents=True)
    monkeypatch.setenv("PROJECTS_DIR", str(projects_root))
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()
    return StudioRunner(event_bus=None, projects_root=projects_root)


def test_resume_start_index_uses_checkpoint(runner):
    manifest = {
        "stages": [
            {"name": "research", "status": "done"},
            {"name": "architect", "status": "running"},
            {"name": "code", "status": "queued"},
        ],
        "pipeline_checkpoint": {
            "last_completed_index": 0,
            "resume_index": 1,
        },
    }
    assert runner._resume_start_index(manifest) == 1


def test_resume_start_index_never_skips_incomplete(runner):
    manifest = {
        "stages": [
            {"name": "research", "status": "done"},
            {"name": "architect", "status": "running"},
        ],
        "pipeline_checkpoint": {
            "last_completed_index": 2,
            "resume_index": 3,
        },
    }
    assert runner._resume_start_index(manifest) == 1


def test_record_pipeline_checkpoint(runner):
    manifest: dict = {}
    runner._record_pipeline_checkpoint(
        manifest,
        stage_index=2,
        stage_name="code",
    )
    assert manifest["pipeline_checkpoint"]["last_completed_stage"] == "code"
    assert manifest["pipeline_checkpoint"]["resume_index"] == 3


def test_studio_token_budget_exceeded(monkeypatch, runner):
    monkeypatch.setenv("SKYN3T_STUDIO_TOKEN_BUDGET", "1000")
    from types import SimpleNamespace

    from skyn3t.config.settings import get_settings
    from skyn3t.observability.token_tracker import get_default_tracker

    get_settings.cache_clear()
    tracker = get_default_tracker()
    tracker._on_exchange(
        SimpleNamespace(
            payload={
                "agent_name": "code_agent",
                "project_slug": "demo",
                "stage": "code",
                "prompt_chars": 4000,
                "response_chars": 4000,
                "backend": "openrouter",
                "model": "test",
            }
        )
    )
    assert runner._studio_token_budget_exceeded("demo") is True
    assert runner._studio_token_budget_exceeded("other") is False


def test_resume_interrupted_uses_checkpoint(tmp_path, runner, monkeypatch):
    slug = "chk-demo"
    artifact_dir = runner.projects_root / slug
    artifact_dir.mkdir(parents=True)
    manifest = {
        "slug": slug,
        "template": "app_saas",
        "brief": "Build a todo app",
        "status": "interrupted",
        "stages": [
            {"name": "research", "status": "done"},
            {"name": "architect", "status": "running"},
        ],
        "pipeline_checkpoint": {
            "last_completed_index": 0,
            "resume_index": 1,
        },
        "history": [],
        "artifacts": [],
    }
    (artifact_dir / "project.json").write_text(json.dumps(manifest), encoding="utf-8")

    captured: dict = {}

    async def fake_resume_pipeline_from(slug_arg, start_index):
        captured["start_index"] = start_index
        return manifest

    monkeypatch.setattr(runner, "_resume_pipeline_from", fake_resume_pipeline_from)

    import asyncio

    asyncio.run(runner.resume_interrupted(slug))
    assert captured["start_index"] == 1
