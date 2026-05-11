"""Tests for BuildVerifierAgent — the "does it actually run?" gate.

The whole point: a scaffold-shaped project shouldn't be allowed to report
"done" if it doesn't compile/parse. These tests pin down the three live
stack detectors (python, node, static) plus the unknown-stack skip path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skyn3t.agents.build_verifier import BuildVerifierAgent
from skyn3t.core.agent import TaskRequest


@pytest.mark.asyncio
async def test_python_scaffold_that_compiles_passes(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "app.py").write_text("def hello():\n    return 'world'\n")
    (scaffold / "README.md").write_text("just a readme")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes", out
    assert out["stack"] == "python"
    assert out["failure_hint"] is None


@pytest.mark.asyncio
async def test_python_scaffold_with_syntax_error_fails(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "app.py").write_text("def broken(:\n  pass\n")  # syntax error
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no", out
    assert out["stack"] == "python"
    assert out["failure_hint"]  # non-empty hint for retry
    assert "SyntaxError" in out["stderr"] or "invalid syntax" in out["stderr"]


@pytest.mark.asyncio
async def test_static_html_well_formed_passes(tmp_path, monkeypatch):
    """Stub the render gate so this test doesn't spin up Chromium on every
    run — that adds 3-5s to the suite. The render gate has dedicated tests
    below that exercise both the skip and fail paths."""
    monkeypatch.setattr(
        BuildVerifierAgent, "_render_smoke_test",
        staticmethod(lambda *_a, **_kw: None),
    )
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "index.html").write_text(
        "<!doctype html><html><head><title>x</title></head>"
        "<body><h1>hi</h1></body></html>"
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes", out
    assert out["stack"] == "static"


@pytest.mark.asyncio
async def test_unknown_stack_is_skipped(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "notes.txt").write_text("nothing here")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "skipped", out
    assert out["stack"] == "unknown"


@pytest.mark.asyncio
async def test_missing_scaffold_dir_returns_failure(tmp_path):
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(
        TaskRequest(input_data={"scaffold_dir": str(tmp_path / "does-not-exist")})
    )
    assert res.success is False
    assert "does not exist" in (res.error or "")


@pytest.mark.asyncio
async def test_failure_hint_carries_tail_of_stderr(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "app.py").write_text(
        "def a():\n    pass\n"
        + "\n".join([f"# filler line {i}" for i in range(50)])
        + "\ndef b(:\n  pass\n"  # syntax error at the end
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no"
    hint = out["failure_hint"] or ""
    assert "python verification" in hint.lower() or "build attempt failed" in hint.lower()


@pytest.mark.asyncio
async def test_node_project_passes_when_shape_ok_and_no_syntax_errors(tmp_path, monkeypatch):
    """A well-shaped package.json + no .js files = pass (shape gate succeeds,
    syntax gate has nothing to check, install gate is opt-in off)."""
    monkeypatch.delenv("SKYN3T_VERIFY_NPM_INSTALL", raising=False)
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0", "scripts": {"build": "echo ok"}})
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes", out
    assert out["stack"] == "node"
    assert "install skipped" in out["command"]


@pytest.mark.asyncio
async def test_node_project_fails_when_package_json_scripts_is_a_string(tmp_path):
    """The most common LLM-shape mistake: writing `\"scripts\": \"build\"`
    instead of an object. Shape gate must catch it."""
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    # `scripts` written as a string — invalid.
    scaffold.joinpath("package.json").write_text(
        '{"name": "demo", "version": "0.0.0", "scripts": "build"}'
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no", out
    assert out["stack"] == "node"
    assert "scripts" in (out["stderr"] or "")
    assert out["failure_hint"]


@pytest.mark.asyncio
async def test_node_project_fails_when_package_json_is_garbage(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    scaffold.joinpath("package.json").write_text("{ this is not valid json")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no", out
    assert "parsed" in (out["stderr"] or "").lower() or "json" in (out["stderr"] or "").lower()


@pytest.mark.asyncio
async def test_static_render_gate_returns_skipped_when_playwright_unavailable(tmp_path, monkeypatch):
    """When Playwright isn't installed, the render gate gracefully reports
    skipped and the static check passes on parse alone — never penalizes a
    well-formed HTML scaffold just because the optional browser dep is missing."""
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "index.html").write_text(
        "<!doctype html><html><head><title>x</title></head>"
        "<body><h1>hi</h1></body></html>"
    )
    # Force the "playwright not installed" branch by stubbing the helper.
    monkeypatch.setattr(
        BuildVerifierAgent, "_render_smoke_test",
        staticmethod(lambda *_a, **_kw: None),
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes"
    assert out["stack"] == "static"
    assert "render gate skipped" in (out["command"] or "")


@pytest.mark.asyncio
async def test_static_render_gate_fails_when_smoke_returns_errors(tmp_path, monkeypatch):
    """If Playwright is available AND reports a console.error, the static
    verifier must flip verdict to 'no' so the in-place fix loop fires."""
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "index.html").write_text(
        "<!doctype html><html><head><title>x</title></head>"
        "<body><h1>hi</h1></body></html>"
    )
    monkeypatch.setattr(
        BuildVerifierAgent, "_render_smoke_test",
        staticmethod(lambda *_a, **_kw: (False, ["pageerror: ReferenceError: foo is not defined"], "")),
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no"
    assert out["stack"] == "static"
    assert "pageerror" in (out["stderr"] or "")
    assert out["failure_hint"]


@pytest.mark.asyncio
async def test_artifact_dir_alias_works(tmp_path):
    """The runner passes artifact_dir; verifier looks under artifact_dir/scaffold."""
    artifact_dir = tmp_path / "proj"
    scaffold = artifact_dir / "scaffold"
    scaffold.mkdir(parents=True)
    (scaffold / "app.py").write_text("x = 1\n")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"artifact_dir": str(artifact_dir)}))
    out = res.output
    assert out["verdict"] == "yes"
    assert out["stack"] == "python"
