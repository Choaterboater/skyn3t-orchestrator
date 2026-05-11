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
async def test_static_html_well_formed_passes(tmp_path):
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
async def test_node_project_with_no_lockfile_skipped(tmp_path):
    """package.json present but no package-lock.json and no index.js — verifier
    should skip rather than run a potentially-slow install."""
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0", "scripts": {"build": "echo ok"}})
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "skipped"
    assert out["stack"] == "node"


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
