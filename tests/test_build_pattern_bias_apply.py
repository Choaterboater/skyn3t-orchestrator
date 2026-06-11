"""Build-pattern bias proposals apply via skill persistence, not CodeImprover."""

from __future__ import annotations

import pytest

from skyn3t.cortex.build_pattern_bias import apply_build_pattern_bias
from skyn3t.cortex.proposals import ProposalStore


@pytest.mark.asyncio
async def test_apply_build_pattern_bias_without_code_improver(tmp_path, monkeypatch):
    import skyn3t.intelligence.skill_library as sl

    skill_root = tmp_path / "skills"
    monkeypatch.setattr(sl, "_default_library", None)
    monkeypatch.setattr(
        sl,
        "get_default_library",
        lambda: sl.SkillLibrary(root=skill_root),
    )

    prefs_path = tmp_path / "prefs.json"
    monkeypatch.setattr(
        "skyn3t.cortex.build_pattern_bias.PREFS_PATH",
        prefs_path,
    )

    payload = {
        "kind": "build_pattern_bias",
        "stack": "node",
        "winner_shape": ["src/index.js", "package.json"],
        "winner_success_rate": 0.85,
        "winner_samples": 7,
        "loser_success_rate": 0.2,
        "distinguishing_files": ["src/index.js"],
    }
    result = await apply_build_pattern_bias(payload)

    assert result["ok"] is True
    assert result["status"] == "applied"
    assert result["skill"] == "node-winning-shape"
    assert prefs_path.exists()
    prefs = __import__("json").loads(prefs_path.read_text())
    assert prefs["node"]["shape"] == payload["winner_shape"]

    lib = sl.get_default_library()
    skills = lib.find(tag="node", min_score=-1.0, limit=5)
    assert any(s.name == "node-winning-shape" for s in skills)


@pytest.mark.asyncio
async def test_feature_handler_routes_build_pattern_bias(tmp_path, monkeypatch):
    from skyn3t.cortex.handlers import install_handlers

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)

    applied: list[dict] = []

    async def fake_apply(payload):
        applied.append(payload)
        return {"ok": True, "status": "applied", "stack": payload["stack"]}

    monkeypatch.setattr(
        "skyn3t.cortex.build_pattern_bias.apply_build_pattern_bias",
        fake_apply,
    )

    orchestrator = type("O", (), {"agents": {}})()
    install_handlers(orchestrator)

    handler = store._handlers["feature"]
    result = await handler(
        {
            "kind": "build_pattern_bias",
            "stack": "node",
            "winner_shape": ["package.json"],
        }
    )
    assert result["ok"] is True
    assert applied[0]["stack"] == "node"
