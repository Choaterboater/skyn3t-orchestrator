from __future__ import annotations

import json

import pytest

from skyn3t.config.settings import get_settings
from skyn3t.core.llm_readiness import assess_llm_readiness


def _write_playbook(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "playbook.json").write_text(
        json.dumps(
            [
                {
                    "kind": "model_winner",
                    "title": "Best model for code tasks",
                    "content": "Prefer a proven model for code work.",
                    "score": 1.0,
                    "tags": ["code", "routing"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (root / "playbook.md").write_text("# SkyN3t Learnings Playbook\n", encoding="utf-8")


def test_readiness_accepts_configured_openrouter_and_playbook(monkeypatch, tmp_path):
    playbook_root = tmp_path / "skynetllm"
    _write_playbook(playbook_root)
    monkeypatch.setenv("SKYN3T_LEARNINGS_DIR", str(playbook_root))
    monkeypatch.setenv("SKYN3T_LLM_BACKEND", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("SKYN3T_NO_CLAUDE", "1")
    monkeypatch.setenv("SKYN3T_FREE_ONLY", "0")
    get_settings.cache_clear()

    result = assess_llm_readiness()

    assert result["real_project_ready"] is True
    assert "openrouter" in result["real_available_backends"]
    assert result["fallback_policy"]["deterministic"] == "blocked_for_real_projects"
    assert result["learnings"]["json_exists"] is True
    assert result["learnings"]["md_exists"] is True
    assert result["learnings"]["entry_count"] == 1
    assert not any(item["code"] == "no_real_backend" for item in result["blockers"])


def test_readiness_blocks_deterministic_only_real_projects(monkeypatch, tmp_path):
    playbook_root = tmp_path / "skynetllm"
    _write_playbook(playbook_root)
    monkeypatch.setenv("SKYN3T_LEARNINGS_DIR", str(playbook_root))
    monkeypatch.setenv("SKYN3T_LLM_BACKEND", "auto")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("SKYN3T_ALLOW_DETERMINISTIC_REAL_PROJECTS", raising=False)
    monkeypatch.setattr("skyn3t.core.llm_readiness.shutil.which", lambda _name: None)
    get_settings.cache_clear()

    result = assess_llm_readiness()

    assert result["real_project_ready"] is False
    assert any(item["code"] == "no_real_backend" for item in result["blockers"])
    assert result["fallback_policy"]["deterministic"] == "blocked_for_real_projects"


def test_no_claude_policy_requires_openrouter_for_real_readiness(monkeypatch, tmp_path):
    playbook_root = tmp_path / "skynetllm"
    _write_playbook(playbook_root)
    monkeypatch.setenv("SKYN3T_LEARNINGS_DIR", str(playbook_root))
    monkeypatch.setenv("SKYN3T_LLM_BACKEND", "anthropic")
    monkeypatch.setenv("SKYN3T_NO_CLAUDE", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("skyn3t.core.llm_readiness.shutil.which", lambda _name: None)
    get_settings.cache_clear()

    result = assess_llm_readiness()

    assert result["real_project_ready"] is False
    assert result["real_available_backends"] == []
    assert result["availability"]["anthropic"]["available"] is False
    assert result["availability"]["anthropic"]["disabled_by_policy"] == "SKYN3T_NO_CLAUDE"
    assert any(item["code"] == "no_real_backend" for item in result["blockers"])


def test_auto_readiness_ignores_clis_not_in_auto_order(monkeypatch, tmp_path):
    playbook_root = tmp_path / "skynetllm"
    _write_playbook(playbook_root)
    monkeypatch.setenv("SKYN3T_LEARNINGS_DIR", str(playbook_root))
    monkeypatch.setenv("SKYN3T_LLM_BACKEND", "auto")
    monkeypatch.delenv("SKYN3T_NO_CLAUDE", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr(
        "skyn3t.core.llm_readiness.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "copilot" else None,
    )
    get_settings.cache_clear()

    result = assess_llm_readiness()

    assert result["availability"]["copilot_cli"]["available"] is True
    assert result["real_available_backends"] == []
    assert result["real_project_ready"] is False
    assert any(item["code"] == "no_real_backend" for item in result["blockers"])


@pytest.mark.asyncio
async def test_llm_readiness_endpoint_uses_core_assessment(monkeypatch):
    import skyn3t.web.app as web_app

    monkeypatch.setattr(
        "skyn3t.core.llm_readiness.assess_llm_readiness",
        lambda: {"status": "ready", "real_project_ready": True},
    )

    result = await web_app.llm_readiness()

    assert result == {"status": "ready", "real_project_ready": True}
