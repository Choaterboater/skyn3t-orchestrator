"""Tests for skyn3t.intelligence.subagent_runner.

These run a real Python subprocess against in-tree agents so we
exercise the full IPC contract (stdin JSON → child runs → stdout JSON
result line). The parent unmarshals and returns a SubagentResult.

The CodeAgent is used as the in-tree subject because it ships with a
simple `code_execution` capability that doesn't need an LLM and runs
deterministically.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from skyn3t.intelligence.subagent_runner import (
    DEFAULT_TIMEOUT_SECONDS,
    SubagentResult,
    SubagentRunner,
    _parse_last_json_line,
    _truncate,
)


# ─── Pure-helper tests ─────────────────────────────────────────────────


def test_parse_last_json_line_finds_last_object():
    text = '\n'.join([
        "noise before",
        '{"first": 1}',  # not what we want
        "more noise",
        '{"status": "ok", "output": 42}',
    ])
    obj = _parse_last_json_line(text)
    assert obj == {"status": "ok", "output": 42}


def test_parse_last_json_line_ignores_non_object_lines():
    obj = _parse_last_json_line('[1,2,3]\n"hello"\nnot-json\n')
    assert obj is None


def test_parse_last_json_line_handles_empty_string():
    assert _parse_last_json_line("") is None


def test_truncate_caps_byte_length():
    data = b"x" * 1000
    out = _truncate(data, 100)
    assert len(out) <= 120  # 100 + the "…[truncated]" marker
    assert "truncated" in out


def test_truncate_handles_undecodable_bytes():
    out = _truncate(b"\xff\xfe valid utf later", 100)
    assert "valid utf later" in out


# ─── End-to-end: real subprocess via CodeAgent ─────────────────────────


@pytest.mark.asyncio
async def test_runner_executes_a_task_in_a_real_subprocess():
    """The child should run code_execution against the in-tree CodeAgent
    and return the captured stdout in result.output."""
    runner = SubagentRunner(timeout_seconds=30)
    result = await runner.run({
        "agent_class": "skyn3t.agents.code_agent:CodeAgent",
        "task": {
            "task_type": "code_execution",
            "code": "print(2 + 2)",
        },
    })
    assert result.status == "ok", (result.error, result.stderr)
    out = result.output or {}
    # CodeAgent returns a dict; "output" inside it is the captured stdout.
    assert out.get("success") is True
    assert "4" in (out.get("output") or "")
    assert result.returncode == 0
    assert result.subagent_id.startswith("sub-")


@pytest.mark.asyncio
async def test_runner_reports_error_for_bad_agent_class():
    runner = SubagentRunner(timeout_seconds=15)
    result = await runner.run({
        "agent_class": "skyn3t.nonexistent:GhostAgent",
        "task": {},
    })
    # Child wrote a structured error to stdout — parent reports "error".
    assert result.status == "error"
    assert "could not import" in (result.error or "")


@pytest.mark.asyncio
async def test_runner_reports_error_when_agent_class_string_is_malformed():
    runner = SubagentRunner(timeout_seconds=15)
    result = await runner.run({
        "agent_class": "no-colon-here",
        "task": {},
    })
    assert result.status == "error"
    assert "module:ClassName" in (result.error or "")


@pytest.mark.asyncio
async def test_runner_times_out_a_slow_child(monkeypatch):
    """A child that hangs forever must be killed at timeout_seconds and
    surface status='timeout'.

    The child's CodeAgent runs in a restricted-builtins shim that
    forbids `import`, so we can't sleep inside the executed snippet.
    Instead we point the runner at a built-in module that blocks on
    stdin forever — the subagent_runner's main() reads sys.stdin and
    blocks there if the parent never sends a line. We exploit that by
    invoking the runner with a custom python_bin that never closes
    its stdin write end.

    Simpler approach: replace the spawn with a shell sleep via a tiny
    subprocess that wraps the python child. We do that by setting
    python_bin to /bin/sh and feeding `-c 'sleep 10'`. The runner's
    contract is that it just spawns + communicates, so it'll time out
    cleanly on any process that doesn't exit.
    """
    runner = SubagentRunner(
        python_bin="/bin/sh",
        timeout_seconds=0.5,
    )
    # Monkeypatch the cmd construction inline — we want `sh -c sleep`
    # rather than `sh -m skyn3t.intelligence.subagent_runner`.
    import skyn3t.intelligence.subagent_runner as sub_mod
    real_popen = sub_mod.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        # Replace whatever was about to spawn with a long sleep so
        # we deterministically hit the timeout path.
        return real_popen(["/bin/sh", "-c", "sleep 30"], **kwargs)

    monkeypatch.setattr(sub_mod.subprocess, "Popen", fake_popen)
    start = time.monotonic()
    result = await runner.run({
        "agent_class": "skyn3t.agents.code_agent:CodeAgent",
        "task": {"task_type": "code_execution", "code": ""},
    })
    elapsed = time.monotonic() - start
    assert result.status == "timeout"
    assert "exceeded timeout" in (result.error or "")
    assert elapsed < 5.0  # killed promptly, didn't wait for full sleep


def test_build_env_strips_keys_and_applies_overrides(monkeypatch):
    """env_strip removes inherited keys; env_overrides add/replace.

    Pure unit test of the env construction — doesn't depend on a child
    process or any agent's sandboxing semantics."""
    monkeypatch.setenv("SUBAGENT_TEST_SECRET", "leaked!")
    monkeypatch.setenv("SUBAGENT_TEST_KEEP", "kept")
    runner = SubagentRunner(
        env_strip=["SUBAGENT_TEST_SECRET"],
        env_overrides={"SUBAGENT_TEST_FLAG": "child-saw-it"},
    )
    env = runner._build_env()
    assert "SUBAGENT_TEST_SECRET" not in env
    assert env.get("SUBAGENT_TEST_KEEP") == "kept"
    assert env.get("SUBAGENT_TEST_FLAG") == "child-saw-it"


def test_build_env_overrides_replace_inherited_values(monkeypatch):
    """If a key is in the inherited env AND in overrides, override wins."""
    monkeypatch.setenv("SUBAGENT_REPLACE_ME", "old")
    runner = SubagentRunner(env_overrides={"SUBAGENT_REPLACE_ME": "new"})
    env = runner._build_env()
    assert env["SUBAGENT_REPLACE_ME"] == "new"


@pytest.mark.asyncio
async def test_runner_returns_structured_result_to_dict():
    """SubagentResult.to_dict yields a stable JSON-shape for the
    dashboard/observability path."""
    r = SubagentResult(status="ok", output={"x": 1}, returncode=0)
    d = r.to_dict()
    assert d["status"] == "ok"
    assert d["output"] == {"x": 1}
    assert d["returncode"] == 0
    assert "subagent_id" in d
