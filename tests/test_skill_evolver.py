"""Reflective skill evolution: pick the worst skills, rewrite via LLM, file a
draft + proposal (live skill untouched until approved)."""

import asyncio

from skyn3t.cortex import skill_evolver


class _FakeSkill:
    def __init__(self, name, body, sc, fc):
        self.name = name
        self.body = body
        self.description = ""
        self.author = ""
        self.tags = ["x"]
        self.triggers = []
        self.success_count = sc
        self.failure_count = fc
        self.source = "test"
        self.slug = name

    @property
    def score(self):
        d = self.success_count + self.failure_count
        return 0.0 if d == 0 else (self.success_count - self.failure_count) / d


class _FakeLib:
    def __init__(self, skills):
        self._skills = skills
        self.drafts = []

    def all(self):
        return self._skills

    def upsert_draft(self, skill):
        self.drafts.append(skill)


class _FakeLLM:
    def __init__(self, out="NEW BODY content"):
        self.out = out
        self.calls = 0

    async def complete(self, prompt, **kw):
        self.calls += 1
        return self.out


class _FakeStore:
    def __init__(self):
        self.created = []

    def create(self, **kw):
        self.created.append(kw)
        return type("P", (), kw)


def test_evolve_candidates_picks_worst_with_enough_signal():
    good = _FakeSkill("good", "b", 9, 1)   # score +0.8 -> filtered
    bad = _FakeSkill("bad", "b", 1, 9)     # score -0.8 -> candidate
    thin = _FakeSkill("thin", "b", 0, 1)   # only 1 sample -> filtered
    cands = skill_evolver.evolve_candidates(
        _FakeLib([good, bad, thin]), max_score=-0.2, min_samples=3, limit=3
    )
    assert [c.name for c in cands] == ["bad"]


def test_run_once_writes_draft_and_proposal():
    bad = _FakeSkill("bad", "old body", 1, 9)
    lib = _FakeLib([bad])
    store = _FakeStore()
    llm = _FakeLLM("rewritten, better body")
    names = asyncio.run(
        skill_evolver.run_once(
            library=lib, llm=llm, proposal_store=store, min_samples=3, max_score=-0.2
        )
    )
    assert names == ["bad"]
    assert len(lib.drafts) == 1
    assert lib.drafts[0].body == "rewritten, better body"
    assert lib.drafts[0].success_count == 0  # counts reset to re-earn grade
    assert len(store.created) == 1
    assert store.created[0]["kind"] == "code_patch"


def test_no_rewrite_when_body_unchanged():
    bad = _FakeSkill("bad", "same", 1, 9)
    lib = _FakeLib([bad])
    store = _FakeStore()
    llm = _FakeLLM("same")  # identical -> no-op
    names = asyncio.run(
        skill_evolver.run_once(library=lib, llm=llm, proposal_store=store)
    )
    assert names == []
    assert lib.drafts == []
    assert store.created == []
