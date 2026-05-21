"""Tests for sandboxed execution backends."""

from __future__ import annotations

import pytest

from skyn3t.security.sandbox import (
    DockerBackend,
    DockerPoolBackend,
    ExecutionResult,
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
        backend._available = True
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
    async def test_inline_name(self) -> None:
        backend = await get_backend("inline")
        assert isinstance(backend, InlineBackend)

    @pytest.mark.asyncio
    async def test_auto_falls_back_when_docker_missing(self, monkeypatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
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
        import skyn3t.web.app as web_app
        from fastapi.testclient import TestClient

        monkeypatch.setenv("SKYN3T_EXECUTION_BACKEND", "inline")
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

    def test_exec_rejects_missing_code(self) -> None:
        import skyn3t.web.app as web_app
        from fastapi.testclient import TestClient

        client = TestClient(web_app.app)
        response = client.post("/api/exec", json={})
        assert response.status_code == 400
        assert "code is required" in response.json()["error"]

    def test_exec_rejects_bad_json(self) -> None:
        import skyn3t.web.app as web_app
        from fastapi.testclient import TestClient

        client = TestClient(web_app.app)
        response = client.post(
            "/api/exec",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
