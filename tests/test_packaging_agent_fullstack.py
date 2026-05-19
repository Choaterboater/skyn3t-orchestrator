"""Tests for PackagingAgent's fullstack strategy.

Verifies that a project with both a frontend (react in scaffold/) and
a backend (fastapi at root) gets:
  - Web outputs (useConfig, Settings.jsx, .gitignore)
  - Server outputs (Dockerfile, docker-compose.yml, .env.example)
  - Frontend service added to compose
  - API_BASE_URL default seeded in useConfig
  - Unified root README explaining both layers
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skyn3t.agents.packaging_agent import PackagingAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus

# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_fullstack(tmp_path: Path, *, server_extra_deps: str = "") -> Path:
    """React frontend in scaffold/, FastAPI backend at root."""
    artifact = tmp_path / "my-fullstack-app-a6f6c0"
    scaffold = artifact / "scaffold"
    # Frontend
    _write(scaffold, "package.json", json.dumps({
        "name": "scaffold",
        "version": "0.1.0",
        "dependencies": {"react": "^18", "react-dom": "^18"},
        "devDependencies": {"vite": "^5", "@vitejs/plugin-react": "^4"},
        "scripts": {"dev": "vite", "build": "vite build"},
    }))
    _write(scaffold, "index.html", "<div id='root'></div>")
    _write(scaffold, "src/App.jsx",
           "import { useConfig } from './hooks/useConfig';\n"
           "function App() {\n"
           "  const cfg = useConfig();\n"
           "  return <h1>API at {cfg.get('API_BASE_URL')}</h1>;\n"
           "}\n"
           "export default App;\n")
    _write(scaffold, "src/main.jsx",
           "import App from './App.jsx';\nApp();\n")
    # Backend
    deps = "fastapi==0.110\nuvicorn[standard]==0.27\n" + server_extra_deps
    _write(artifact, "requirements.txt", deps)
    _write(artifact, "main.py",
           "import os\n"
           "from fastapi import FastAPI\n"
           "app = FastAPI()\n"
           "JWT_SECRET = os.environ['JWT_SECRET']\n")
    return artifact


async def _run(artifact: Path) -> dict:
    agent = PackagingAgent(event_bus=EventBus())
    await agent.initialize()
    task = TaskRequest(
        title="package",
        input_data={"artifact_dir": str(artifact), "packaging_verify": False},
    )
    result = await agent.execute(task)
    assert result.success is True
    return result.output or {}


# ---------------------------------------------------------------------------
# Strategy dispatch
# ---------------------------------------------------------------------------

class TestStrategyDispatch:
    @pytest.mark.asyncio
    async def test_react_plus_fastapi_routes_to_fullstack(self, tmp_path: Path) -> None:
        artifact = _make_fullstack(tmp_path)
        output = await _run(artifact)
        assert output["strategy"] == "fullstack"


# ---------------------------------------------------------------------------
# Both layers produced
# ---------------------------------------------------------------------------

class TestBothLayersProduced:
    @pytest.mark.asyncio
    async def test_web_outputs_present(self, tmp_path: Path) -> None:
        artifact = _make_fullstack(tmp_path)
        await _run(artifact)
        assert (artifact / "scaffold/src/hooks/useConfig.js").is_file()
        assert (artifact / "scaffold/src/Settings.jsx").is_file()

    @pytest.mark.asyncio
    async def test_server_outputs_present(self, tmp_path: Path) -> None:
        artifact = _make_fullstack(tmp_path)
        await _run(artifact)
        assert (artifact / "Dockerfile").is_file()
        assert (artifact / "docker-compose.yml").is_file()
        assert (artifact / ".env.example").is_file()


# ---------------------------------------------------------------------------
# Frontend wired into docker-compose
# ---------------------------------------------------------------------------

class TestComposeFrontendWired:
    @pytest.mark.asyncio
    async def test_frontend_service_added_to_compose(self, tmp_path: Path) -> None:
        artifact = _make_fullstack(tmp_path)
        output = await _run(artifact)
        compose = (artifact / "docker-compose.yml").read_text()
        assert "frontend:" in compose
        assert "nginx" in compose
        assert "5173" in compose
        assert "depends_on:" in compose
        # Note should mention the wiring
        notes = " ".join(output["notes"])
        assert "frontend service added" in notes

    @pytest.mark.asyncio
    async def test_idempotent_doesnt_double_add_frontend(self, tmp_path: Path) -> None:
        artifact = _make_fullstack(tmp_path)
        await _run(artifact)
        before = (artifact / "docker-compose.yml").read_text()
        await _run(artifact)
        after = (artifact / "docker-compose.yml").read_text()
        # Frontend mentioned exactly once each time
        assert before.count("frontend:") == 1
        assert after.count("frontend:") == 1


# ---------------------------------------------------------------------------
# API_BASE_URL default seeded into useConfig
# ---------------------------------------------------------------------------

class TestApiBaseUrlSeeded:
    @pytest.mark.asyncio
    async def test_useconfig_gets_default(self, tmp_path: Path) -> None:
        artifact = _make_fullstack(tmp_path)
        output = await _run(artifact)
        hook = (artifact / "scaffold/src/hooks/useConfig.js").read_text()
        assert "DEFAULTS" in hook
        assert "API_BASE_URL" in hook
        assert "http://localhost:8000" in hook  # FastAPI default
        notes = " ".join(output["notes"])
        assert "API_BASE_URL default seeded" in notes

    @pytest.mark.asyncio
    async def test_default_used_when_config_missing(self, tmp_path: Path) -> None:
        artifact = _make_fullstack(tmp_path)
        await _run(artifact)
        hook = (artifact / "scaffold/src/hooks/useConfig.js").read_text()
        # The patched `get` should chain through DEFAULTS
        assert "DEFAULTS[key]" in hook


# ---------------------------------------------------------------------------
# Unified root README
# ---------------------------------------------------------------------------

class TestFullstackReadme:
    @pytest.mark.asyncio
    async def test_readme_explains_both_layers(self, tmp_path: Path) -> None:
        artifact = _make_fullstack(tmp_path)
        await _run(artifact)
        readme = (artifact / "README.md").read_text()
        # Both layers mentioned
        assert "Frontend" in readme
        assert "Backend" in readme
        # Single quick-start command
        assert "docker compose up" in readme
        # Both ports mentioned
        assert "8000" in readme  # backend
        assert "5173" in readme  # frontend

    @pytest.mark.asyncio
    async def test_readme_lists_server_secrets_separately(self, tmp_path: Path) -> None:
        artifact = _make_fullstack(tmp_path, server_extra_deps="psycopg2-binary==2.9\n")
        await _run(artifact)
        readme = (artifact / "README.md").read_text()
        # Server-side section calls out secrets vs in-app config
        assert "Server-side" in readme
        assert "JWT_SECRET" in readme

    @pytest.mark.asyncio
    async def test_readme_lists_services_when_detected(self, tmp_path: Path) -> None:
        artifact = _make_fullstack(tmp_path, server_extra_deps="psycopg2-binary==2.9\nredis==5\n")
        await _run(artifact)
        readme = (artifact / "README.md").read_text()
        assert "postgres" in readme.lower()
        assert "redis" in readme.lower()


# ---------------------------------------------------------------------------
# Non-regression: web-only and server-only projects still work
# ---------------------------------------------------------------------------

class TestNonRegression:
    @pytest.mark.asyncio
    async def test_web_only_still_routes_to_web(self, tmp_path: Path) -> None:
        artifact = tmp_path / "web-only-a6f6c0"
        scaffold = artifact / "scaffold"
        _write(scaffold, "package.json", json.dumps({
            "name": "scaffold",
            "dependencies": {"react": "^18", "react-dom": "^18"},
            "devDependencies": {"vite": "^5"},
            "scripts": {"build": "vite build"},
        }))
        _write(scaffold, "src/App.jsx", "function App(){return null;}\nexport default App;")
        output = await _run(artifact)
        assert output["strategy"] == "web"
        assert not (artifact / "Dockerfile").is_file()

    @pytest.mark.asyncio
    async def test_server_only_still_routes_to_server(self, tmp_path: Path) -> None:
        artifact = tmp_path / "server-only-a6f6c0"
        artifact.mkdir()
        _write(artifact, "requirements.txt", "fastapi==0.110\nuvicorn[standard]==0.27\n")
        _write(artifact, "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        output = await _run(artifact)
        assert output["strategy"] == "server"
        # No scaffold/ created
        assert not (artifact / "scaffold").exists()
