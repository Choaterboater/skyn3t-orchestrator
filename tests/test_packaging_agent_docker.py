"""Tests for PackagingAgent's server (Docker) strategy.

Covers FastAPI / Flask / Django / Express fixtures + service detection
(postgres / redis / mongodb) being threaded into compose stanzas
correctly. README + .env.example + .gitignore all tested.

Verification is skipped by design for server projects — the downstream
BuildVerifier owns `docker compose build`. Tests just assert that the
agent reports verifier_skipped=True with no error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skyn3t.agents.packaging_agent import PackagingAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_fastapi(tmp_path: Path, *, extra_deps: str = "") -> Path:
    """Minimal FastAPI project at the artifact root."""
    artifact = tmp_path / "my-api-a6f6c0"
    artifact.mkdir(parents=True)
    deps = "fastapi==0.110\nuvicorn[standard]==0.27\n" + extra_deps
    _write(artifact, "requirements.txt", deps)
    _write(artifact, "main.py", (
        "import os\n"
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "JWT_SECRET = os.environ['JWT_SECRET']\n"
    ))
    return artifact


def _make_flask(tmp_path: Path) -> Path:
    artifact = tmp_path / "my-flask-app-a6f6c0"
    artifact.mkdir(parents=True)
    _write(artifact, "requirements.txt", "flask==3.0\ngunicorn==21.2\n")
    _write(artifact, "app.py", "from flask import Flask\napp = Flask(__name__)\n")
    return artifact


def _make_express(tmp_path: Path) -> Path:
    artifact = tmp_path / "my-express-api-a6f6c0"
    artifact.mkdir(parents=True)
    _write(artifact, "package.json", json.dumps({
        "name": "demo",
        "version": "1.0.0",
        "dependencies": {"express": "^4"},
        "scripts": {"dev": "node server.js"},
    }))
    _write(artifact, "server.js",
           "const express = require('express');\n"
           "const app = express();\n"
           "app.listen(process.env.PORT || 3000);\n")
    return artifact


async def _run(artifact: Path) -> dict:
    agent = PackagingAgent(event_bus=EventBus())
    await agent.initialize()
    task = TaskRequest(title="package", input_data={"artifact_dir": str(artifact)})
    result = await agent.execute(task)
    assert result.success is True
    return result.output or {}


# ---------------------------------------------------------------------------
# Strategy dispatch
# ---------------------------------------------------------------------------

class TestStrategyDispatch:
    @pytest.mark.asyncio
    async def test_fastapi_routes_to_server(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        output = await _run(artifact)
        assert output["strategy"] == "server"
        assert output["verifier_skipped"] is True

    @pytest.mark.asyncio
    async def test_flask_routes_to_server(self, tmp_path: Path) -> None:
        artifact = _make_flask(tmp_path)
        output = await _run(artifact)
        assert output["strategy"] == "server"

    @pytest.mark.asyncio
    async def test_express_routes_to_server(self, tmp_path: Path) -> None:
        artifact = _make_express(tmp_path)
        output = await _run(artifact)
        assert output["strategy"] == "server"


# ---------------------------------------------------------------------------
# Dockerfile generation
# ---------------------------------------------------------------------------

class TestDockerfile:
    @pytest.mark.asyncio
    async def test_fastapi_dockerfile_uses_uvicorn(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        await _run(artifact)
        dockerfile = (artifact / "Dockerfile").read_text()
        assert "FROM python:3.12-slim" in dockerfile
        assert "uvicorn" in dockerfile
        assert "0.0.0.0" in dockerfile
        assert "EXPOSE 8000" in dockerfile

    @pytest.mark.asyncio
    async def test_flask_dockerfile_uses_gunicorn(self, tmp_path: Path) -> None:
        artifact = _make_flask(tmp_path)
        await _run(artifact)
        dockerfile = (artifact / "Dockerfile").read_text()
        assert "gunicorn" in dockerfile
        assert "EXPOSE 5000" in dockerfile

    @pytest.mark.asyncio
    async def test_express_dockerfile_uses_node(self, tmp_path: Path) -> None:
        artifact = _make_express(tmp_path)
        await _run(artifact)
        dockerfile = (artifact / "Dockerfile").read_text()
        assert "FROM node:22-alpine" in dockerfile
        assert "npm ci --omit=dev" in dockerfile
        assert "EXPOSE 3000" in dockerfile

    @pytest.mark.asyncio
    async def test_existing_dockerfile_left_alone(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        existing = "FROM python:3.10\n# my custom dockerfile\n"
        (artifact / "Dockerfile").write_text(existing)
        output = await _run(artifact)
        assert (artifact / "Dockerfile").read_text() == existing
        notes = " ".join(output["notes"])
        assert "already exists" in notes


# ---------------------------------------------------------------------------
# docker-compose.yml generation
# ---------------------------------------------------------------------------

class TestCompose:
    @pytest.mark.asyncio
    async def test_compose_basic_no_services(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        await _run(artifact)
        compose = (artifact / "docker-compose.yml").read_text()
        assert "services:" in compose
        assert "app:" in compose
        assert 'build: .' in compose
        assert '"8000:8000"' in compose
        # No infra service when none detected
        assert "postgres:" not in compose
        assert "volumes:" not in compose

    @pytest.mark.asyncio
    async def test_compose_with_postgres(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path, extra_deps="psycopg2-binary==2.9\n")
        await _run(artifact)
        compose = (artifact / "docker-compose.yml").read_text()
        assert "postgres:" in compose
        assert "postgres:16-alpine" in compose
        assert "depends_on:" in compose
        assert "service_healthy" in compose  # postgres uses healthcheck
        assert "postgres-data" in compose
        assert "volumes:" in compose

    @pytest.mark.asyncio
    async def test_compose_with_redis_and_mongo(self, tmp_path: Path) -> None:
        # FastAPI + redis + mongo via deps
        artifact = _make_fastapi(tmp_path, extra_deps="redis==5\npymongo==4\n")
        await _run(artifact)
        compose = (artifact / "docker-compose.yml").read_text()
        assert "redis:" in compose
        assert "redis:7-alpine" in compose
        assert "mongodb:" in compose
        assert "mongo:7" in compose

    @pytest.mark.asyncio
    async def test_existing_compose_left_alone(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        existing = "services:\n  app:\n    image: my-custom\n"
        (artifact / "docker-compose.yml").write_text(existing)
        output = await _run(artifact)
        assert (artifact / "docker-compose.yml").read_text() == existing
        assert any("already exists" in n for n in output["notes"])


# ---------------------------------------------------------------------------
# .env.example generation
# ---------------------------------------------------------------------------

class TestEnvExample:
    @pytest.mark.asyncio
    async def test_env_example_has_app_vars(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        await _run(artifact)
        env = (artifact / ".env.example").read_text()
        # JWT_SECRET is in main.py via os.environ['JWT_SECRET']
        assert "JWT_SECRET" in env
        assert "secret" in env.lower()  # the comment

    @pytest.mark.asyncio
    async def test_env_example_has_postgres_creds_when_pg_detected(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path, extra_deps="psycopg2-binary==2.9\n")
        await _run(artifact)
        env = (artifact / ".env.example").read_text()
        assert "POSTGRES_USER" in env
        assert "POSTGRES_PASSWORD" in env
        assert "DATABASE_URL" in env

    @pytest.mark.asyncio
    async def test_env_example_skips_vite_vars(self, tmp_path: Path) -> None:
        # FastAPI scaffold with a stray VITE_ var reference shouldn't end
        # up in the server-side .env.example. (Edge case: a fullstack
        # repo before we get the combo strategy.)
        artifact = _make_fastapi(tmp_path)
        _write(artifact, "frontend/main.jsx",
               "const u = import.meta.env.VITE_API_URL;\n")
        await _run(artifact)
        env = (artifact / ".env.example").read_text()
        assert "VITE_API_URL" not in env

    @pytest.mark.asyncio
    async def test_existing_env_example_left_alone(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        existing = "# my custom .env.example\nFOO=bar\n"
        (artifact / ".env.example").write_text(existing)
        output = await _run(artifact)
        assert (artifact / ".env.example").read_text() == existing
        assert any("already exists" in n for n in output["notes"])


# ---------------------------------------------------------------------------
# README generation
# ---------------------------------------------------------------------------

class TestServerReadme:
    @pytest.mark.asyncio
    async def test_readme_has_docker_quickstart(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        await _run(artifact)
        readme = (artifact / "README.md").read_text()
        assert "docker compose up" in readme
        assert "cp .env.example .env" in readme

    @pytest.mark.asyncio
    async def test_readme_lists_required_vars(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        await _run(artifact)
        readme = (artifact / "README.md").read_text()
        assert "JWT_SECRET" in readme
        assert "Required environment variables" in readme

    @pytest.mark.asyncio
    async def test_readme_lists_services_when_detected(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path, extra_deps="psycopg2-binary==2.9\nredis==5\n")
        await _run(artifact)
        readme = (artifact / "README.md").read_text()
        assert "postgres" in readme.lower()
        assert "redis" in readme.lower()

    @pytest.mark.asyncio
    async def test_readme_has_native_dev_command(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        await _run(artifact)
        readme = (artifact / "README.md").read_text()
        assert "uvicorn" in readme
        assert "--reload" in readme


# ---------------------------------------------------------------------------
# .gitignore
# ---------------------------------------------------------------------------

class TestServerGitignore:
    @pytest.mark.asyncio
    async def test_gitignore_includes_python_and_env(self, tmp_path: Path) -> None:
        artifact = _make_fastapi(tmp_path)
        await _run(artifact)
        gi = (artifact / ".gitignore").read_text()
        assert "__pycache__/" in gi
        assert ".venv/" in gi
        assert ".env" in gi

    @pytest.mark.asyncio
    async def test_gitignore_includes_node_for_node_servers(self, tmp_path: Path) -> None:
        artifact = _make_express(tmp_path)
        await _run(artifact)
        gi = (artifact / ".gitignore").read_text()
        assert "node_modules/" in gi
        assert ".env" in gi


# ---------------------------------------------------------------------------
# Web strategy regression — adding docker logic shouldn't break web
# ---------------------------------------------------------------------------

class TestWebStillWorks:
    @pytest.mark.asyncio
    async def test_web_project_still_routes_to_web_strategy(self, tmp_path: Path) -> None:
        # Recreate a minimal react_vite scaffold
        artifact = tmp_path / "my-web-app-a6f6c0"
        scaffold = artifact / "scaffold"
        scaffold.mkdir(parents=True)
        (scaffold / "package.json").write_text(json.dumps({
            "name": "demo",
            "dependencies": {"react": "^18", "react-dom": "^18"},
            "devDependencies": {"vite": "^5"},
        }))
        (scaffold / "src").mkdir()
        (scaffold / "src/App.jsx").write_text(
            "function App() { return null; }\nexport default App;\n"
        )
        agent = PackagingAgent(event_bus=EventBus())
        await agent.initialize()
        result = await agent.execute(TaskRequest(
            title="package",
            input_data={"artifact_dir": str(artifact), "packaging_verify": False},
        ))
        assert result.output["strategy"] == "web"
        # No docker artifacts for a web project
        assert not (artifact / "Dockerfile").is_file()
        assert not (artifact / "docker-compose.yml").is_file()
