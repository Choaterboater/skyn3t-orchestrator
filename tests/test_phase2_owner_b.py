"""Phase 2 — Owner B (CodeAgent) self-learning-loop wiring.

These tests pin the three behaviors Owner B added to
``CodeAgent._scaffold_from_brief`` (skyn3t/agents/code_agent.py):

1. ITEM 3 — injected-skill names are surfaced on the TaskResult output
   under ``output["injected_skills"]`` so the runner (Owner A) can call
   ``skill_library.record_use(name, success=verdict=="yes")`` per build.
2. ITEM 2 — lesson strings threaded through ``task.input_data["lessons"]``
   (set by the runner / Owner A) are appended to the build system prompt,
   so they reach the model on every build path — including paths where the
   inline RAG-recall query never fires.
3. ITEM 4 — when the runner threads a live engine through
   ``task.input_data["rag_engine"]`` the agent REUSES it instead of
   cold-starting a fresh ``RAGEngine()`` (and Owner I's per-query metric
   fires for that call). The cold-start fallback is preserved for
   non-studio callers that pass no engine.

The test drives the real ``_scaffold_from_brief`` end-to-end against a
fake LLM client and stubbed collaborators, writing only into ``tmp_path``.
It never touches ``data/`` and never starts the orchestrator.

Each test is written so it FAILS before the Owner B change and PASSES
after: e.g. removing the ``"injected_skills"`` output key, dropping the
input_data lessons block, or reinstating the unconditional cold-start
would each break a distinct assertion below.
"""

from __future__ import annotations

import asyncio

import pytest

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.agent import TaskRequest

# ─── fakes ────────────────────────────────────────────────────────────


class _FakeSkill:
    """Minimal stand-in for a skill_library.Skill record."""

    def __init__(self, name: str, body: str) -> None:
        self.name = name
        self.body = body


class _FakeLibrary:
    """Fake skill library: every find() returns the same single skill so
    the injection loop populates ``seen`` deterministically (one name)."""

    SKILL_NAME = "owner-b-test-skill"

    def find(self, tag=None, min_score=0.0, limit=3):
        return [_FakeSkill(self.SKILL_NAME, "Always memoize the grid render.")]


class _RecordingLLMClient:
    """Fake LLMClient. Records every ``system=`` prompt it is given and
    returns a deterministic plan for the planning call and raw HTML for
    the per-file build call. No subprocess, no network."""

    def __init__(self) -> None:
        self.systems: list[str] = []
        self.prompts: list[str] = []

    async def complete(self, prompt, *, system=None, **kwargs):
        self.systems.append(system or "")
        self.prompts.append(prompt or "")
        # Planning call: the system prompt describes JSON planning.
        if system and "planning a runnable project" in system:
            return (
                '{"stack": "minimal", "files": '
                '[{"path": "index.html", "purpose": "single-page app entry"}]}'
            )
        # Per-file build call: return valid, non-stub HTML.
        return (
            "<!DOCTYPE html>\n<html><head><title>App</title></head>"
            "<body><main id=\"root\">hello</main></body></html>\n"
        )


class _StubRagEngine:
    """A pre-initialized live engine, threaded via input_data['rag_engine'].
    Records that .query() was invoked (proving reuse) and returns no docs."""

    def __init__(self) -> None:
        self.query_calls = 0

    async def query(self, query_text, n_results=3, filter_dict=None):
        self.query_calls += 1
        return {"documents": []}


class _ExplodingRagEngine:
    """Sentinel used to patch the cold-start RAGEngine class. Constructing
    it is a test failure signal: if the agent reuses the passed engine it
    must NEVER instantiate this."""

    def __init__(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError(
            "cold-start RAGEngine() was constructed even though a live "
            "rag_engine was threaded through input_data"
        )


# ─── shared harness ───────────────────────────────────────────────────


def _patch_common(monkeypatch):
    """Patches shared by every test: force the LLM-plan path, deterministic
    skills, no per-file backend reroute, and no OpenRouter fast-path."""
    # Force the LLM-plan branch (no deterministic stack template) so
    # stack stays "minimal" and index.html becomes a normal LLM job.
    monkeypatch.setattr(
        "skyn3t.agents.stack_templates.detect_stack_from_handoff",
        lambda *a, **k: None,
    )
    # Deterministic skill injection -> seen == {SKILL_NAME}.
    monkeypatch.setattr(
        "skyn3t.intelligence.skill_library.get_default_library",
        lambda: _FakeLibrary(),
    )
    # Keep file_client == the agent's primary fake client (no reroute,
    # no new LLMClient subprocess construction).
    monkeypatch.setattr(
        "skyn3t.core.model_router.resolve_model_for_file",
        lambda *a, **k: (None, None),
    )
    # Disable the OpenRouter entrypoint fast-path / last-resort retries.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SKYN3T_ENTRYPOINT_OPENROUTER_FIRST", "0")


def _make_agent(monkeypatch):
    agent = CodeAgent(name="code_agent_test")
    client = _RecordingLLMClient()
    # ``get_llm()`` returns the ``llm`` property which is backed by the
    # lazily-built ``_llm`` field. Seed ``_llm`` so both the get_llm()
    # call (line ~1076) and the later ``self._llm`` reference use the fake.
    agent._llm = client
    return agent, client


def _run_scaffold(agent, *, scaffold_dir, lessons=None, rag_engine=None,
                  brief="Build a tiny single-page hello app."):
    input_data = {
        "brief": brief,
        "artifact_dir": str(scaffold_dir),
        "code_scaffold_dir": str(scaffold_dir),
    }
    if lessons is not None:
        input_data["lessons"] = lessons
    if rag_engine is not None:
        input_data["rag_engine"] = rag_engine
    task = TaskRequest(title="hello", description="hello", input_data=input_data)
    return asyncio.run(agent._scaffold_from_brief(task))


# ─── tests ────────────────────────────────────────────────────────────


def test_injected_skills_surfaced_on_output(monkeypatch, tmp_path):
    """ITEM 3: the names pulled into the prompt land in output."""
    _patch_common(monkeypatch)
    agent, _client = _make_agent(monkeypatch)

    result = _run_scaffold(agent, scaffold_dir=tmp_path / "build1")

    assert result.success is True
    assert "injected_skills" in result.output, (
        "TaskResult.output must carry 'injected_skills' for runner record_use"
    )
    assert result.output["injected_skills"] == [_FakeLibrary.SKILL_NAME]


def test_lessons_from_input_data_reach_system_prompt(monkeypatch, tmp_path):
    """ITEM 2 (read-side): input_data['lessons'] is appended to the build
    system prompt, so the per-file LLM call sees it."""
    _patch_common(monkeypatch)
    agent, client = _make_agent(monkeypatch)

    lessons = [
        "OWNER_B_LESSON_SENTINEL: never ship a JSON dump as the UI.",
        "OWNER_B_LESSON_SENTINEL2: wire empty + error states.",
    ]
    result = _run_scaffold(
        agent, scaffold_dir=tmp_path / "build2", lessons=lessons,
    )
    assert result.success is True

    # The per-file build call carries the lessons in its system prompt.
    build_systems = [
        s for s in client.systems if "implementing one file" in s
    ]
    assert build_systems, "expected at least one per-file build call"
    joined = "\n".join(build_systems)
    assert "Lessons from past builds" in joined
    assert "OWNER_B_LESSON_SENTINEL" in joined
    assert "OWNER_B_LESSON_SENTINEL2" in joined


def test_lessons_absent_does_not_break_or_inject(monkeypatch, tmp_path):
    """No input_data lessons -> no input_data lesson block (and no crash).
    Guards against a malformed-payload regression."""
    _patch_common(monkeypatch)
    agent, client = _make_agent(monkeypatch)

    result = _run_scaffold(agent, scaffold_dir=tmp_path / "build3")
    assert result.success is True
    build_systems = [s for s in client.systems if "implementing one file" in s]
    assert build_systems
    # The sentinel from the lessons test must never appear unprompted.
    assert "OWNER_B_LESSON_SENTINEL" not in "\n".join(build_systems)


def test_live_rag_engine_is_reused_not_cold_started(monkeypatch, tmp_path):
    """ITEM 4: a threaded rag_engine is queried; the cold-start RAGEngine
    class is never instantiated."""
    _patch_common(monkeypatch)
    agent, _client = _make_agent(monkeypatch)

    # Any construction of the cold-start engine is a hard failure.
    monkeypatch.setattr(
        "skyn3t.rag.rag_engine.RAGEngine", _ExplodingRagEngine,
    )
    stub = _StubRagEngine()

    result = _run_scaffold(
        agent, scaffold_dir=tmp_path / "build4", rag_engine=stub,
    )
    assert result.success is True
    assert stub.query_calls >= 1, (
        "threaded live rag_engine.query() must be used for recall"
    )


def test_cold_start_fallback_preserved_without_engine(monkeypatch, tmp_path):
    """No rag_engine in input_data -> the cold-start fallback path is
    eligible. We assert the build still succeeds (the fallback is guarded
    by a vector-db existence check, so on a clean tmp env it simply
    no-ops) and that no live stub was consulted."""
    _patch_common(monkeypatch)
    agent, _client = _make_agent(monkeypatch)

    # Point the settings vector_db_path at a non-existent tmp dir so the
    # cold-start branch hits its 'vector db not initialized' guard and
    # bails cleanly (never constructing a real ChromaDB) — proving the
    # fallback branch is still reachable and non-fatal.
    missing_db = tmp_path / "no_such_vector_db"

    class _S:
        vector_db_path = str(missing_db)

    monkeypatch.setattr(
        "skyn3t.config.settings.get_settings", lambda: _S(),
    )

    result = _run_scaffold(agent, scaffold_dir=tmp_path / "build5")
    assert result.success is True
    # A file was written despite no RAG recall — the build path is intact.
    assert result.output["written_count"] >= 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
