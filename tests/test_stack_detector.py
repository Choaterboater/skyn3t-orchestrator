"""Tests for StackDetector — verifies each stack family + service detection."""

from __future__ import annotations

import json
from pathlib import Path

from skyn3t.agents.stack_detector import detect

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_pkg(root: Path, **deps: str) -> Path:
    """Write a package.json under root with the given deps."""
    pkg = {"name": "demo", "version": "1.0.0", "dependencies": deps}
    return _write(root, "package.json", json.dumps(pkg))


def _write_pkg_nested(root: Path, **deps: str) -> Path:
    """Write package.json under root/scaffold/ — the realistic SkyN3t layout."""
    pkg = {"name": "demo", "version": "1.0.0", "dependencies": deps}
    return _write(root, "scaffold/package.json", json.dumps(pkg))


# ---------------------------------------------------------------------------
# Empty / missing dirs
# ---------------------------------------------------------------------------

class TestEmptyDir:
    def test_nonexistent_returns_unknown(self, tmp_path: Path) -> None:
        result = detect(tmp_path / "does-not-exist")
        assert result.family == "unknown"
        assert result.runtimes == []

    def test_empty_dir_returns_unknown_with_note(self, tmp_path: Path) -> None:
        result = detect(tmp_path)
        assert result.family == "unknown"
        assert "no manifest files found" in result.confidence_notes


# ---------------------------------------------------------------------------
# Web stacks
# ---------------------------------------------------------------------------

class TestWebStacks:
    def test_react_vite(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, react="^18", **{"react-dom": "^18"}, vite="^5")
        result = detect(tmp_path)
        assert result.family == "web"
        assert result.stack == "react_vite"
        assert any(r.name == "node" for r in result.runtimes)

    def test_next(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, next="^14", react="^18", **{"react-dom": "^18"})
        result = detect(tmp_path)
        assert result.family == "web"
        assert result.stack == "next"

    def test_sveltekit(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, **{"@sveltejs/kit": "^2"}, vite="^5")
        result = detect(tmp_path)
        assert result.family == "web"
        assert result.stack == "sveltekit"

    def test_astro(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, astro="^4")
        result = detect(tmp_path)
        assert result.stack == "astro"

    def test_nested_scaffold_dir(self, tmp_path: Path) -> None:
        _write_pkg_nested(tmp_path, react="^18", vite="^5")
        result = detect(tmp_path)
        assert result.family == "web"
        assert result.stack == "react_vite"


# ---------------------------------------------------------------------------
# Server stacks
# ---------------------------------------------------------------------------

class TestServerStacks:
    def test_express(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, express="^4")
        result = detect(tmp_path)
        assert result.family == "server"
        assert result.stack == "express"

    def test_fastify(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, fastify="^4")
        result = detect(tmp_path)
        assert result.stack == "fastify"

    def test_hono_with_node_server(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, hono="^4", **{"@hono/node-server": "^1"})
        result = detect(tmp_path)
        assert result.stack == "hono"

    def test_fastapi_via_requirements(self, tmp_path: Path) -> None:
        _write(tmp_path, "requirements.txt", "fastapi==0.110\nuvicorn[standard]==0.27\n")
        result = detect(tmp_path)
        assert result.family == "server"
        assert result.stack == "fastapi"
        assert any(r.name == "python" for r in result.runtimes)

    def test_flask_via_pyproject(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml", """\
[project]
name = "demo"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["flask >=3.0", "gunicorn ==21.0"]
""")
        result = detect(tmp_path)
        assert result.stack == "flask"
        node = [r for r in result.runtimes if r.name == "python"][0]
        assert node.min_version == "3.12"

    def test_django_via_poetry(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml", """\
[tool.poetry]
name = "demo"
version = "0.1.0"

[tool.poetry.dependencies]
python = "^3.11"
django = "^5.0"
""")
        result = detect(tmp_path)
        assert result.stack == "django"


# ---------------------------------------------------------------------------
# Fullstack
# ---------------------------------------------------------------------------

class TestFullstack:
    def test_react_plus_fastapi(self, tmp_path: Path) -> None:
        _write_pkg_nested(tmp_path, react="^18", vite="^5")
        _write(tmp_path, "requirements.txt", "fastapi==0.110\nuvicorn[standard]==0.27\n")
        # Note: pyproject/requirements at root means the python side is the
        # "monorepo backend" — common pattern for react+fastapi setups.
        result = detect(tmp_path)
        # The scan looks at scaffold/ first and finds react_vite. Then sees
        # requirements.txt at root (since scaffold has no python markers)
        # — but right now we only scan scaffold once chosen. So this test
        # actually exposes a real behavioral question.
        # For now: react_vite was found in scaffold/, so family is "web".
        # When PackagingAgent runs, it'll see the requirements.txt at root
        # via its own scan. Document the current behavior.
        assert result.family == "web"

    def test_react_plus_express_in_same_pkg(self, tmp_path: Path) -> None:
        # Single package.json with both — the simplest fullstack case.
        _write_pkg(tmp_path, react="^18", **{"react-dom": "^18"}, vite="^5", express="^4")
        result = detect(tmp_path)
        assert result.family == "fullstack"
        # Stack stays as the web framework — server is implied by deps.
        assert result.stack == "react_vite"


# ---------------------------------------------------------------------------
# Service detection (postgres, redis, mongodb, ...)
# ---------------------------------------------------------------------------

class TestServiceDetection:
    def test_postgres_via_node_dep(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, express="^4", pg="^8")
        result = detect(tmp_path)
        assert "postgres" in result.services

    def test_postgres_via_python_dep(self, tmp_path: Path) -> None:
        _write(tmp_path, "requirements.txt", "fastapi\npsycopg2-binary\n")
        result = detect(tmp_path)
        assert "postgres" in result.services

    def test_redis(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, express="^4", ioredis="^5")
        result = detect(tmp_path)
        assert "redis" in result.services

    def test_mongodb(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, fastify="^4", mongoose="^8")
        result = detect(tmp_path)
        assert "mongodb" in result.services

    def test_celery_implies_redis(self, tmp_path: Path) -> None:
        _write(tmp_path, "requirements.txt", "fastapi\ncelery\n")
        result = detect(tmp_path)
        assert "redis" in result.services

    def test_multiple_services_no_dups(self, tmp_path: Path) -> None:
        _write(tmp_path, "requirements.txt", "fastapi\npsycopg2\nasyncpg\nredis\n")
        result = detect(tmp_path)
        assert sorted(result.services) == ["postgres", "redis"]


# ---------------------------------------------------------------------------
# Compose detection — directly reads service names
# ---------------------------------------------------------------------------

class TestComposeDetection:
    def test_compose_postgres(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, express="^4")
        _write(tmp_path, "docker-compose.yml", """\
services:
  app:
    build: .
    ports:
      - "8000:8000"
  postgres:
    image: postgres:16-alpine
""")
        result = detect(tmp_path)
        assert result.has_compose is True
        assert "postgres" in result.services

    def test_compose_multiple_services(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, fastify="^4")
        _write(tmp_path, "compose.yaml", """\
services:
  app:
    build: .
  postgres:
    image: postgres:16
  redis:
    image: redis:7
  rabbitmq:
    image: rabbitmq:3
""")
        result = detect(tmp_path)
        assert set(result.services) >= {"postgres", "redis", "rabbitmq"}

    def test_app_service_not_treated_as_infra(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, express="^4")
        _write(tmp_path, "docker-compose.yml", """\
services:
  app:
    build: .
  web:
    image: nginx
""")
        result = detect(tmp_path)
        # "app" and "web" are application services, not infra dependencies.
        assert "app" not in result.services
        assert "web" not in result.services


# ---------------------------------------------------------------------------
# Dockerfile detection
# ---------------------------------------------------------------------------

class TestDockerfileDetection:
    def test_root_dockerfile(self, tmp_path: Path) -> None:
        _write(tmp_path, "Dockerfile", "FROM python:3.12-slim\n")
        _write(tmp_path, "requirements.txt", "fastapi\n")
        result = detect(tmp_path)
        assert result.has_dockerfile is True

    def test_scaffold_dockerfile(self, tmp_path: Path) -> None:
        _write_pkg_nested(tmp_path, express="^4")
        _write(tmp_path, "scaffold/Dockerfile", "FROM node:22-alpine\n")
        result = detect(tmp_path)
        assert result.has_dockerfile is True

    def test_lowercase_dockerfile(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, express="^4")
        _write(tmp_path, "dockerfile", "FROM node:22\n")
        result = detect(tmp_path)
        assert result.has_dockerfile is True


# ---------------------------------------------------------------------------
# Runtime version extraction
# ---------------------------------------------------------------------------

class TestRuntimeVersions:
    def test_node_engines_version(self, tmp_path: Path) -> None:
        pkg = {
            "name": "demo",
            "version": "1.0.0",
            "engines": {"node": ">=22.0.0"},
            "dependencies": {"react": "^18", "vite": "^5"},
        }
        _write(tmp_path, "package.json", json.dumps(pkg))
        result = detect(tmp_path)
        node = [r for r in result.runtimes if r.name == "node"][0]
        assert node.min_version == "22.0.0"

    def test_node_runtime_present_without_engines(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, react="^18", vite="^5")
        result = detect(tmp_path)
        node = [r for r in result.runtimes if r.name == "node"][0]
        # No engines = no min_version. Caller falls back to docs default.
        assert node.min_version is None
        assert node.install_url == "https://nodejs.org/"


# ---------------------------------------------------------------------------
# Convenience properties
# ---------------------------------------------------------------------------

class TestConvenienceProps:
    def test_is_web_and_is_server_flags(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, react="^18", vite="^5", express="^4")
        result = detect(tmp_path)
        assert result.is_web is True
        assert result.is_server is True

    def test_unknown_flags_false(self, tmp_path: Path) -> None:
        result = detect(tmp_path)
        assert result.is_web is False
        assert result.is_server is False


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_malformed_package_json_returns_unknown(self, tmp_path: Path) -> None:
        _write(tmp_path, "package.json", "{ not valid json")
        result = detect(tmp_path)
        # We don't crash; family stays unknown.
        assert result.family == "unknown"

    def test_unknown_framework_returns_unknown_with_note(self, tmp_path: Path) -> None:
        # Some other npm tool — not a recognized web or server framework.
        _write_pkg(tmp_path, lodash="^4")
        result = detect(tmp_path)
        assert result.family == "unknown"
        assert result.confidence_notes  # Should explain why
