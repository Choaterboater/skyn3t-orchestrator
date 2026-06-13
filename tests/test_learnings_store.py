"""Learnings Store: compile curated learnings, retrieve relevant slices, and
ground guidance in them (model call falls back to raw context)."""

import json

from skyn3t.intelligence import learnings_store
from skyn3t.intelligence.learnings_store import LearningsStore


class _FakeSkill:
    def __init__(self, name, body, sc, fc):
        self.name = name
        self.body = body
        self.description = ""
        self.tags = ["t"]
        self.success_count = sc
        self.failure_count = fc
        self.slug = name

    @property
    def score(self):
        d = self.success_count + self.failure_count
        return 0.0 if d == 0 else (self.success_count - self.failure_count) / d


class _FakeLib:
    def __init__(self, skills):
        self._skills = skills

    def find(self, *, min_score=0.0, limit=5, tag=None):
        return [s for s in self._skills if s.score >= min_score][:limit]


def test_compile_and_guidance(tmp_path):
    prefs = tmp_path / "prefs.json"
    prefs.write_text(json.dumps({
        "fastapi": {
            "shape": ["src/main.py", "tests/test_health.py"],
            "winner_success_rate": 0.85,
            "loser_success_rate": 0.16,
            "distinguishing_files": ["tests/test_health.py"],
        }
    }))
    store = LearningsStore(root=tmp_path / "learn")
    lib = _FakeLib([
        _FakeSkill("good skill", "do X then Y", 8, 1),
        _FakeSkill("bad skill", "", 0, 5),  # score -1 -> filtered out
    ])
    # data_dir -> tmp (no model_tournament/build_success files) to isolate the test
    n = store.compile(library=lib, prefs_path=prefs, min_skill_score=0.2, data_dir=str(tmp_path))
    assert n == 2
    assert store.json_path.exists() and store.md_path.exists()

    top = store.guidance_for("fastapi tests", stack="fastapi")
    assert top and top[0]["kind"] == "build_pattern"

    ctx = store.ask("fastapi tests", use_model=False)
    assert "fastapi" in ctx.lower()


def test_storage_dir_is_configurable_for_nas(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_LEARNINGS_DIR", str(tmp_path / "nas" / "learnings"))
    assert "nas" in str(learnings_store.learnings_dir())


def test_empty_store_returns_empty(tmp_path):
    store = LearningsStore(root=tmp_path / "empty")
    assert store.guidance_for("anything") == []
    assert store.ask("anything", use_model=False) == ""
