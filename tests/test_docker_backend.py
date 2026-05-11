"""Tests for skyn3t.intelligence.docker_backend.

Most of the behavior we care about — command shape, env scrub, timeout
fallback — is verifiable without actually invoking docker (CI machines
usually don't have it). The full end-to-end smoke is left to operator
verification per the README.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skyn3t.intelligence.docker_backend import (
    CONTAINER_SOURCE_MOUNT,
    DEFAULT_DOCKER_IMAGE,
    DockerSubagentRunner,
    docker_available,
)


# ─── Command construction ──────────────────────────────────────────────


def test_build_cmd_includes_required_pieces(tmp_path):
    runner = DockerSubagentRunner(project_root=tmp_path)
    cmd = runner._build_cmd("sub-test")
    assert cmd[0] == "docker"
    assert cmd[1] == "run"
    assert "--rm" in cmd
    assert "-i" in cmd
    assert "--name" in cmd
    name_idx = cmd.index("--name")
    assert cmd[name_idx + 1] == "skyn3t-sub-test"
    # Source mount: read-only bind onto /skyn3t.
    mount_arg = f"{tmp_path.resolve()}:{CONTAINER_SOURCE_MOUNT}:ro"
    assert mount_arg in cmd
    # Workdir
    w_idx = cmd.index("-w")
    assert cmd[w_idx + 1] == CONTAINER_SOURCE_MOUNT
    # Image + entrypoint
    assert DEFAULT_DOCKER_IMAGE in cmd
    py_idx = cmd.index("python")
    assert cmd[py_idx : py_idx + 4] == [
        "python", "-m", "skyn3t.intelligence.subagent_runner",
    ]


def test_build_cmd_includes_memory_and_cpu_caps(tmp_path):
    runner = DockerSubagentRunner(
        project_root=tmp_path,
        memory_limit_mb=512,
        cpu_limit=1.5,
    )
    cmd = runner._build_cmd("sub-x")
    assert "--memory" in cmd
    mem_idx = cmd.index("--memory")
    assert cmd[mem_idx + 1] == "512m"
    cpu_idx = cmd.index("--cpus")
    assert cmd[cpu_idx + 1] == "1.5"


def test_build_cmd_passes_through_explicit_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_TEST_FLAG", "child-saw-it")
    monkeypatch.setenv("SHOULD_BE_HIDDEN", "secret-do-not-leak")
    runner = DockerSubagentRunner(
        project_root=tmp_path,
        env_passthrough=["SKYN3T_TEST_FLAG"],  # ONLY this — secret stays out
    )
    cmd = runner._build_cmd("sub-x")
    env_args = [c for c in cmd if "=" in c and c.split("=")[0]]
    # The -e flag is the previous element; collect those properly.
    passes = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-e" and i + 1 < len(cmd)]
    assert "SKYN3T_TEST_FLAG=child-saw-it" in passes
    assert not any("SHOULD_BE_HIDDEN" in p for p in passes)


def test_build_cmd_applies_env_overrides(tmp_path):
    runner = DockerSubagentRunner(
        project_root=tmp_path,
        env_overrides={"SKYN3T_LLM_BACKEND": "kimi_cli"},
    )
    cmd = runner._build_cmd("sub-x")
    passes = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-e" and i + 1 < len(cmd)]
    assert "SKYN3T_LLM_BACKEND=kimi_cli" in passes


def test_build_cmd_appends_extra_run_args(tmp_path):
    """Operators can inject any docker run flag we don't surface
    explicitly (e.g. --gpus, --device, custom labels)."""
    runner = DockerSubagentRunner(
        project_root=tmp_path,
        extra_run_args=["--label", "team=skyn3t"],
    )
    cmd = runner._build_cmd("sub-x")
    # Must appear BEFORE the image but AFTER the standard flags.
    image_idx = cmd.index(DEFAULT_DOCKER_IMAGE)
    assert cmd[image_idx - 2 : image_idx] == ["--label", "team=skyn3t"]


def test_build_cmd_uses_custom_image_when_provided(tmp_path):
    runner = DockerSubagentRunner(
        project_root=tmp_path,
        image="ghcr.io/example/skyn3t-runtime:latest",
    )
    cmd = runner._build_cmd("sub-x")
    assert "ghcr.io/example/skyn3t-runtime:latest" in cmd
    assert DEFAULT_DOCKER_IMAGE not in cmd


def test_build_cmd_honors_network_setting(tmp_path):
    runner = DockerSubagentRunner(project_root=tmp_path, network="none")
    cmd = runner._build_cmd("sub-x")
    net_idx = cmd.index("--network")
    assert cmd[net_idx + 1] == "none"


# ─── docker_available ──────────────────────────────────────────────────


def test_docker_available_returns_bool():
    """The probe never raises — even when docker isn't installed."""
    val = docker_available()
    assert isinstance(val, bool)


# ─── No-docker fallback ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_returns_crashed_when_docker_missing(monkeypatch, tmp_path):
    """When the docker CLI isn't on PATH, run() must return a structured
    crashed result rather than blowing up the caller."""
    import skyn3t.intelligence.docker_backend as db
    monkeypatch.setattr(db, "docker_available", lambda: False)
    runner = DockerSubagentRunner(project_root=tmp_path)
    result = await runner.run({"agent_class": "x:Y", "task": {}})
    assert result.status == "crashed"
    assert "docker" in (result.error or "")
    assert result.image == DEFAULT_DOCKER_IMAGE


# ─── Result.to_dict carries image + container_id ───────────────────────


def test_docker_result_to_dict_includes_image_and_container_id():
    from skyn3t.intelligence.docker_backend import DockerRunResult
    r = DockerRunResult(
        status="ok", output={"x": 1}, returncode=0,
        image="python:3.11-slim", container_id="sub-abc",
    )
    d = r.to_dict()
    assert d["image"] == "python:3.11-slim"
    assert d["container_id"] == "sub-abc"
    assert d["status"] == "ok"
