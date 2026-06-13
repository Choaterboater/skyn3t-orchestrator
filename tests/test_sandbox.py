"""Tests for sandboxed execution backends."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import skyn3t.web.app as web_app
from skyn3t.security.sandbox import (
    DockerBackend,
    DockerPoolBackend,
    InlineBackend,
    get_backend,
)


class TestInlineBackend:
    @pytest.mark.asyncio
    async def test_executes_python_code(self) -> None:
        backend = InlineBackend()
        result = await backend.execute("print('hello')")
        assert result.success is True
        assert "hello" in result.stdout

    @pytest.mark.asyncio
    async def test_captures_print(self) -> None:
        backend = InlineBackend()
        result = await backend.execute("print('err', flush=True)")
        assert result.success is True
        assert "err" in result.stdout

    @pytest.mark.asyncio
    async def test_restricts_dangerous_builtins(self) -> None:
        backend = InlineBackend()
        result = await backend.execute("import os")
        assert result.success is False
        assert "No module named" in result.error or "__import__" in result.error

    @pytest.mark.asyncio
    async def test_empty_code(self) -> None:
        backend = InlineBackend()
        result = await backend.execute("")
        assert result.success is False
        assert "No code" in result.error

    @pytest.mark.asyncio
    async def test_non_python_rejected(self) -> None:
        backend = InlineBackend()
        result = await backend.execute("console.log(1)", language="javascript")
        assert result.success is False
        assert "only supports python" in result.error


class TestDockerBackend:
    @pytest.mark.asyncio
    async def test_available_false_when_docker_missing(self, monkeypatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        backend = DockerBackend()
        assert await backend.available() is False

    @pytest.mark.asyncio
    async def test_execute_returns_error_when_unavailable(self) -> None:
        backend = DockerBackend()
        backend._available = False
        result = await backend.execute("print(1)")
        assert result.success is False
        assert "Docker is not available" in result.error

    @pytest.mark.asyncio
    async def test_unsupported_language(self) -> None:
        backend = DockerBackend()
        backend._available = True
        result = await backend.execute("code", language="fortran")
        assert result.success is False
        assert "does not support language" in result.error

    @pytest.mark.asyncio
    async def test_image_mapping_includes_new_languages(self) -> None:
        assert "go" in DockerBackend._IMAGES
        assert "rust" in DockerBackend._IMAGES
        assert "typescript" in DockerBackend._IMAGES
        assert "php" in DockerBackend._IMAGES
        assert "ruby" in DockerBackend._IMAGES


class TestDockerPoolBackend:
    @pytest.mark.asyncio
    async def test_available_delegates_to_docker(self, monkeypatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        backend = DockerPoolBackend()
        assert await backend.available() is False

    @pytest.mark.asyncio
    async def test_execute_returns_error_when_docker_missing(self, monkeypatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        backend = DockerPoolBackend()
        result = await backend.execute("print(1)")
        assert result.success is False
        assert "Docker is not available" in result.error

    @pytest.mark.asyncio
    async def test_unsupported_language(self) -> None:
        backend = DockerPoolBackend()
        backend.available = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = await backend.execute("code", language="fortran")
        assert result.success is False
        assert "does not support language" in result.error

    @pytest.mark.asyncio
    async def test_shutdown_is_safe_when_no_pool(self) -> None:
        backend = DockerPoolBackend()
        await backend.shutdown()
        assert backend._shutdown is True


class TestGetBackend:
    @pytest.mark.asyncio
    async def test_inline_name(self, monkeypatch) -> None:
        monkeypatch.setenv("SKYN3T_ALLOW_INLINE_EXEC", "1")
        backend = await get_backend("inline")
        assert isinstance(backend, InlineBackend)

    @pytest.mark.asyncio
    async def test_inline_disabled_without_optin(self, monkeypatch) -> None:
        monkeypatch.delenv("SKYN3T_ALLOW_INLINE_EXEC", raising=False)
        with pytest.raises(RuntimeError, match="InlineBackend is disabled"):
            await get_backend("inline")

    @pytest.mark.asyncio
    async def test_auto_raises_when_docker_missing_and_inline_not_allowed(self, monkeypatch) -> None:
        # SECURITY (C1): "auto" must NOT silently fall back to in-process exec.
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.delenv("SKYN3T_ALLOW_INLINE_EXEC", raising=False)
        with pytest.raises(RuntimeError, match="in-process execution is disabled"):
            await get_backend("auto")

    @pytest.mark.asyncio
    async def test_auto_allows_inline_only_with_explicit_optin(self, monkeypatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setenv("SKYN3T_ALLOW_INLINE_EXEC", "1")
        backend = await get_backend("auto")
        assert isinstance(backend, InlineBackend)

    @pytest.mark.asyncio
    async def test_docker_raises_when_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(RuntimeError, match="Docker is not available"):
            await get_backend("docker")

    @pytest.mark.asyncio
    async def test_docker_pool_raises_when_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(RuntimeError, match="Docker is not available"):
            await get_backend("docker-pool")

    @pytest.mark.asyncio
    async def test_unknown_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown execution backend"):
            await get_backend("qemu")


class TestExecEndpoint:
    def test_exec_python_inline(self, monkeypatch) -> None:
        monkeypatch.setenv("SKYN3T_ALLOW_EXEC_API", "1")
        monkeypatch.setenv("SKYN3T_ALLOW_UNAUTHENTICATED_LOOPBACK", "1")
        monkeypatch.setenv("SKYN3T_EXECUTION_BACKEND", "inline")
        monkeypatch.setenv("SKYN3T_ALLOW_INLINE_EXEC", "1")
        from skyn3t.config.settings import get_settings

        get_settings.cache_clear()

        client = TestClient(web_app.app)
        response = client.post(
            "/api/exec",
            json={"code": "print(2+2)", "language": "python", "timeout": 10},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "4" in data["stdout"]
        assert data["backend"] == "InlineBackend"
        get_settings.cache_clear()

    def test_exec_rejects_missing_code(self, monkeypatch) -> None:
        monkeypatch.setenv("SKYN3T_ALLOW_EXEC_API", "1")
        monkeypatch.setenv("SKYN3T_ALLOW_UNAUTHENTICATED_LOOPBACK", "1")
        from skyn3t.config.settings import get_settings

        get_settings.cache_clear()
        client = TestClient(web_app.app)
        response = client.post("/api/exec", json={})
        assert response.status_code == 400
        assert "code is required" in response.json()["error"]
        get_settings.cache_clear()

    def test_exec_rejects_bad_json(self, monkeypatch) -> None:
        monkeypatch.setenv("SKYN3T_ALLOW_EXEC_API", "1")
        monkeypatch.setenv("SKYN3T_ALLOW_UNAUTHENTICATED_LOOPBACK", "1")
        from skyn3t.config.settings import get_settings

        get_settings.cache_clear()
        client = TestClient(web_app.app)
        response = client.post(
            "/api/exec",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        get_settings.cache_clear()


class TestDockerHardening:
    @pytest.mark.asyncio
    async def test_backend_adds_hardening_flags(self, monkeypatch) -> None:
        captured: list[list[str]] = []

        async def _fake_create(*cmd, **kwargs):
            captured.append(list(cmd))
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"ok", b""))
            proc.returncode = 0
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
        monkeypatch.setattr("os.chmod", lambda _path, _mode: None)
        monkeypatch.setenv("SKYN3T_DOCKER_HARDENING", "1")
        from skyn3t.config.settings import get_settings

        get_settings.cache_clear()

        backend = DockerBackend()
        backend._available = True
        result = await backend.execute("print(1)")
        assert result.success is True
        cmd = captured[-1]
        assert "--user" in cmd
        assert "65534:65534" in cmd
        assert "--cap-drop" in cmd
        assert "ALL" in cmd
        assert "--security-opt" in cmd
        assert "no-new-privileges=true" in cmd
        assert "--cpus" in cmd
        assert "--pids-limit" in cmd
        assert "/tmp:noexec,nosuid,size=50m,mode=1777" in cmd

    @pytest.mark.asyncio
    async def test_backend_hardening_can_be_disabled(self, monkeypatch) -> None:
        captured: list[list[str]] = []

        async def _fake_create(*cmd, **kwargs):
            captured.append(list(cmd))
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"ok", b""))
            proc.returncode = 0
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
        monkeypatch.setattr("os.chmod", lambda _path, _mode: None)
        monkeypatch.setenv("SKYN3T_DOCKER_HARDENING", "0")
        from skyn3t.config.settings import get_settings

        get_settings.cache_clear()

        backend = DockerBackend()
        backend._available = True
        result = await backend.execute("print(1)")
        assert result.success is True
        cmd = captured[-1]
        assert "--user" not in cmd
        assert "--cap-drop" not in cmd
        assert "--security-opt" not in cmd

    @pytest.mark.asyncio
    async def test_pool_applies_hardening_flags(self, monkeypatch) -> None:
        captured: list[list[str]] = []

        async def _fake_create(*cmd, **kwargs):
            captured.append(list(cmd))
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock(return_value=0)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
        monkeypatch.setenv("SKYN3T_DOCKER_HARDENING", "1")
        from skyn3t.config.settings import get_settings

        get_settings.cache_clear()

        backend = DockerPoolBackend(pool_size=1)
        backend.available = AsyncMock(return_value=True)  # type: ignore[method-assign]
        await backend.execute("print(1)")
        # _ensure_pool is called inside execute; we only inspect the run cmd.
        run_cmd = next(
            (c for c in captured if len(c) > 3 and c[1] == "run"),
            [],
        )
        assert "--user" in run_cmd
        assert "--cap-drop" in run_cmd
