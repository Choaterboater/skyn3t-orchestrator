"""Tests for IntegrationContractVerifierAgent — frontend/backend contract check.

This agent catches the #1 bug class that slips past BuildVerifier and
BootVerifier: the frontend calls API routes the backend never implemented.
"""

from __future__ import annotations

import pytest

from skyn3t.agents.integration_verifier import (
    IntegrationContractVerifierAgent,
    ProjectProbe,
    RouteIssue,
)
from skyn3t.core.agent import TaskRequest


@pytest.mark.asyncio
async def test_skip_when_no_backend(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "App.jsx").write_text("export default function App() {}")
    (scaffold / "index.html").write_text("<html></html>")
    agent = IntegrationContractVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "skipped"
    assert "no backend" in out["summary"].lower()


@pytest.mark.asyncio
async def test_skip_when_no_frontend(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "server").mkdir()
    (scaffold / "server" / "index.js").write_text("const express = require('express');")
    (scaffold / "server" / "package.json").write_text(
        '{"name": "s", "dependencies": {"express": "^4"}}'
    )
    agent = IntegrationContractVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "skipped"
    assert "no frontend" in out["summary"].lower() or "no api calls" in out["summary"].lower()


@pytest.mark.asyncio
async def test_skip_when_no_api_calls(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "App.jsx").write_text(
        "export default function App() { return <div>hi</div> }"
    )
    (scaffold / "index.html").write_text("<html></html>")
    (scaffold / "server").mkdir()
    (scaffold / "server" / "index.js").write_text("const app = require('express')();")
    (scaffold / "server" / "package.json").write_text(
        '{"name": "s", "dependencies": {"express": "^4"}}'
    )
    agent = IntegrationContractVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "skipped"
    assert "no api calls" in out["summary"].lower()


@pytest.mark.asyncio
async def test_missing_scaffold_dir_returns_failure(tmp_path):
    agent = IntegrationContractVerifierAgent()
    await agent.initialize()
    res = await agent.execute(
        TaskRequest(input_data={"scaffold_dir": str(tmp_path / "does-not-exist")})
    )
    assert res.success is False
    assert "does not exist" in (res.error or "")


@pytest.mark.asyncio
async def test_extract_frontend_routes_finds_fetch_calls(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "api.js").write_text(
        "export async function getQueue() {\n"
        "  const res = await fetch('/api/queue');\n"
        "  return res.json();\n"
        "}\n"
        "export async function getConfig() {\n"
        "  const res = await fetch(`/api/config/${slug}`);\n"
        "  return res.json();\n"
        "}\n"
    )
    agent = IntegrationContractVerifierAgent()
    routes = agent._extract_frontend_routes(scaffold / "src")
    assert "GET /api/queue" in routes
    assert any("/api/config" in r for r in routes)


@pytest.mark.asyncio
async def test_extract_backend_routes_finds_express_routes(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "server").mkdir()
    (scaffold / "server" / "index.js").write_text(
        "const express = require('express');\n"
        "const app = express();\n"
        "app.get('/api/health', (req, res) => res.json({ok: true}));\n"
        "app.use('/api/config', configRouter);\n"
    )
    agent = IntegrationContractVerifierAgent()
    routes = agent._extract_backend_routes(scaffold / "server")
    assert any("GET /api/health" in r for r in routes)
    assert any("USE /api/config" in r for r in routes)


@pytest.mark.asyncio
async def test_match_frontend_to_backend_exact_match():
    agent = IntegrationContractVerifierAgent()
    matchers = [("GET", "/api/health", False)]
    assert agent._match_frontend_to_backend("/api/health", "GET", matchers) == "GET /api/health"


@pytest.mark.asyncio
async def test_match_frontend_to_backend_prefix_match():
    agent = IntegrationContractVerifierAgent()
    matchers = [("USE", "/api/config", True)]
    assert agent._match_frontend_to_backend("/api/config/test", "POST", matchers) == "USE /api/config"


@pytest.mark.asyncio
async def test_match_frontend_to_backend_no_match():
    agent = IntegrationContractVerifierAgent()
    matchers = [("GET", "/api/health", False)]
    assert agent._match_frontend_to_backend("/api/queue", "GET", matchers) is None


@pytest.mark.asyncio
async def test_match_frontend_to_backend_honors_http_method():
    agent = IntegrationContractVerifierAgent()
    matchers = [("GET", "/api/config/:slug", False)]
    assert agent._match_frontend_to_backend("/api/config/:*", "PUT", matchers) is None


@pytest.mark.asyncio
async def test_match_frontend_to_backend_prefers_exact_route_over_prefix():
    agent = IntegrationContractVerifierAgent()
    matchers = [
        ("USE", "/api/config", True),
        ("POST", "/api/config/:slug/test", False),
    ]
    assert (
        agent._match_frontend_to_backend("/api/config/:*/test", "POST", matchers)
        == "POST /api/config/:slug/test"
    )


@pytest.mark.asyncio
async def test_extract_frontend_routes_infers_non_get_methods(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "api.js").write_text(
        "export async function saveConfig(slug, patch) {\n"
        "  return fetch(`/api/config/${slug}`, {\n"
        "    method: 'PUT',\n"
        "    headers: { 'Content-Type': 'application/json' },\n"
        "    body: JSON.stringify(patch),\n"
        "  });\n"
        "}\n"
        "export async function testConfig(slug) {\n"
        "  return fetch(`/api/config/${slug}/test`, { method: 'POST' });\n"
        "}\n"
        "export function deleteThing(id) {\n"
        "  return axios.delete(`/api/things/${id}`);\n"
        "}\n"
    )
    agent = IntegrationContractVerifierAgent()
    routes = agent._extract_frontend_routes(scaffold / "src")
    assert "PUT /api/config/:*" in routes
    assert "POST /api/config/:*/test" in routes
    assert "DELETE /api/things/:*" in routes


@pytest.mark.asyncio
async def test_extract_frontend_routes_tracks_api_helper_calls_without_bogus_placeholder(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "useConfig.js").write_text(
        "const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\\/$/, '');\n"
        "async function requestJson(path, options = {}) {\n"
        "  return fetch(`${API_BASE}${path}`, {\n"
        "    ...options,\n"
        "    headers: { Accept: 'application/json' },\n"
        "  });\n"
        "}\n"
        "export function loadConfig() {\n"
        "  return requestJson('/api/config');\n"
        "}\n"
        "export function updateService(slug, patch) {\n"
        "  return requestJson(`/api/config/${slug}`, { method: 'PUT', body: JSON.stringify(patch) });\n"
        "}\n"
        "export function testConnection(slug) {\n"
        "  return requestJson(`/api/config/${slug}/test`, { method: 'POST' });\n"
        "}\n"
    )
    agent = IntegrationContractVerifierAgent()
    routes = agent._extract_frontend_routes(scaffold / "src")
    assert "GET /api/config" in routes
    assert "PUT /api/config/:*" in routes
    assert "POST /api/config/:*/test" in routes
    assert "GET :*:*" not in routes


@pytest.mark.asyncio
async def test_verify_routes_uses_inferred_http_methods(monkeypatch):
    agent = IntegrationContractVerifierAgent()
    seen = []

    async def mock_curl(path, port, method="GET"):
        seen.append((method, path, port))
        return 200, True, ""

    monkeypatch.setattr(agent, "_curl_route", mock_curl)

    issues = await agent._verify_routes(
        ["PUT /api/config/:*", "POST /api/config/:*/test"],
        ["USE /api/config"],
        3100,
    )

    assert issues == []
    assert seen == [
        ("PUT", "/api/config/:*", 3100),
        ("POST", "/api/config/:*/test", 3100),
    ]


@pytest.mark.asyncio
async def test_verify_routes_treats_404_on_exact_dynamic_route_as_wrong_status(monkeypatch):
    agent = IntegrationContractVerifierAgent()

    async def mock_curl(path, port, method="GET"):  # noqa: ARG001
        return 404, True, ""

    monkeypatch.setattr(agent, "_curl_route", mock_curl)

    issues = await agent._verify_routes(
        ["POST /api/config/:*/test"],
        ["USE /api/config", "POST /api/config/:slug/test"],
        3100,
    )

    assert len(issues) == 1
    assert issues[0].issue == "wrong_status"
    assert issues[0].backend_match == "POST /api/config/:slug/test"
    assert issues[0].http_status == 404


@pytest.mark.asyncio
async def test_verdict_fails_when_routes_missing(tmp_path, monkeypatch):
    """Mock out server booting so we test only the contract logic."""
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "App.jsx").write_text(
        "export default function App() {\n"
        "  useEffect(() => {\n"
        "    fetch('/api/queue');\n"
        "  }, []);\n"
        "}\n"
    )
    (scaffold / "index.html").write_text("<html></html>")
    (scaffold / "server").mkdir()
    (scaffold / "server" / "index.js").write_text(
        "const express = require('express');\n"
        "const app = express();\n"
        "app.get('/api/health', (req, res) => res.json({ok: true}));\n"
    )
    (scaffold / "server" / "package.json").write_text(
        '{"name": "s", "dependencies": {"express": "^4"}}'
    )

    agent = IntegrationContractVerifierAgent()
    await agent.initialize()

    # Mock server boot to succeed without actually starting a server.
    async def mock_boot_wait(_cmd, _cwd, _env, _port):
        return True, None, "server up", ""

    monkeypatch.setattr(agent, "_boot_and_wait", mock_boot_wait)

    # Mock curl: /api/queue returns 404 (missing route).
    async def mock_curl(path, port, method="GET"):
        if path == "/api/queue":
            return 404, False, "Not Found"
        return 200, True, ""

    monkeypatch.setattr(agent, "_curl_route", mock_curl)

    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no", out
    assert "/api/queue" in str(out.get("issues", []))
    assert out["failure_hint"]
    assert "missing" in out["failure_hint"].lower() or "does not implement" in out["failure_hint"].lower()


def test_diagnose_boot_failure_prefers_stub_named_export_hint(tmp_path):
    agent = IntegrationContractVerifierAgent()
    server = tmp_path / "server"
    routes = server / "routes"
    routes.mkdir(parents=True)
    (routes / "config.js").write_text(
        'import { get } from "../config-store.js";\n',
        encoding="utf-8",
    )
    (server / "config-store.js").write_text(
        "// TODO[skyn3t]: code generation failed for server/config-store.js\n"
        "export default null;\n",
        encoding="utf-8",
    )
    stderr = (
        f"file://{(routes / 'config.js').as_posix()}:2\n"
        'import { get } from "../config-store.js";\n'
        "         ^^^\n"
        "SyntaxError: The requested module '../config-store.js' does not provide "
        "an export named 'get'\n"
    )
    hint = agent._diagnose_boot_failure(stderr, tmp_path, ProjectProbe())
    assert "server/config-store.js is still a generated TODO stub" in hint
    assert "`get`" in hint


@pytest.mark.asyncio
async def test_verdict_passes_when_routes_match(tmp_path, monkeypatch):
    """Mock out server booting — test the happy path."""
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "App.jsx").write_text(
        "export default function App() {\n"
        "  useEffect(() => {\n"
        "    fetch('/api/health');\n"
        "  }, []);\n"
        "}\n"
    )
    (scaffold / "index.html").write_text("<html></html>")
    (scaffold / "server").mkdir()
    (scaffold / "server" / "index.js").write_text(
        "const express = require('express');\n"
        "const app = express();\n"
        "app.get('/api/health', (req, res) => res.json({ok: true}));\n"
    )
    (scaffold / "server" / "package.json").write_text(
        '{"name": "s", "dependencies": {"express": "^4"}}'
    )

    agent = IntegrationContractVerifierAgent()
    await agent.initialize()

    async def mock_boot_wait(_cmd, _cwd, _env, _port):
        return True, None, "server up", ""

    monkeypatch.setattr(agent, "_boot_and_wait", mock_boot_wait)

    async def mock_curl(_path, _port, _method="GET"):
        return 200, True, ""

    monkeypatch.setattr(agent, "_curl_route", mock_curl)

    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes", out
    assert out["failure_hint"] is None
    assert len(out.get("issues", [])) == 0


@pytest.mark.asyncio
async def test_verdict_passes_for_request_helper_routes(tmp_path, monkeypatch):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "useConfig.js").write_text(
        "const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\\/$/, '');\n"
        "async function requestJson(path, options = {}) {\n"
        "  return fetch(`${API_BASE}${path}`, { ...options });\n"
        "}\n"
        "export function loadConfig() {\n"
        "  return requestJson('/api/config');\n"
        "}\n"
        "export function updateService(slug, patch) {\n"
        "  return requestJson(`/api/config/${slug}`, { method: 'PUT', body: JSON.stringify(patch) });\n"
        "}\n"
        "export function testConnection(slug) {\n"
        "  return requestJson(`/api/config/${slug}/test`, { method: 'POST' });\n"
        "}\n"
    )
    (scaffold / "index.html").write_text("<html></html>")
    (scaffold / "server").mkdir()
    (scaffold / "server" / "index.js").write_text(
        "const express = require('express');\n"
        "const configRouter = require('./routes/config');\n"
        "const app = express();\n"
        "app.use('/api/config', configRouter);\n"
        "app.get('/api/health', (_req, res) => res.json({ ok: true }));\n"
    )
    (scaffold / "server" / "routes").mkdir()
    (scaffold / "server" / "routes" / "config.js").write_text(
        "const router = require('express').Router();\n"
        "router.get('/', (_req, res) => res.json({ config: {} }));\n"
        "router.put('/:slug', (_req, res) => res.json({ ok: true }));\n"
        "router.post('/:slug/test', (_req, res) => res.json({ ok: true }));\n"
        "module.exports = router;\n"
    )
    (scaffold / "server" / "package.json").write_text(
        '{"name": "s", "dependencies": {"express": "^4"}}'
    )

    agent = IntegrationContractVerifierAgent()
    await agent.initialize()

    async def mock_boot_wait(_cmd, _cwd, _env, _port):
        return True, None, "server up", ""

    async def mock_curl(_path, _port, _method="GET"):
        return 200, True, ""

    monkeypatch.setattr(agent, "_boot_and_wait", mock_boot_wait)
    monkeypatch.setattr(agent, "_curl_route", mock_curl)

    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes", out
    assert "GET :*:*" not in out.get("frontend_routes", [])


@pytest.mark.asyncio
async def test_verdict_warns_for_dynamic_config_routes_loaded_via_helper_mount(tmp_path, monkeypatch):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "useConfig.js").write_text(
        "const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\\/$/, '');\n"
        "async function requestJson(path, options = {}) {\n"
        "  return fetch(`${API_BASE}${path}`, { ...options });\n"
        "}\n"
        "export function loadConfig() {\n"
        "  return requestJson('/api/config');\n"
        "}\n"
        "export function updateService(slug, patch) {\n"
        "  return requestJson(`/api/config/${slug}`, { method: 'PUT', body: JSON.stringify(patch) });\n"
        "}\n"
        "export function testConnection(slug) {\n"
        "  return requestJson(`/api/config/${slug}/test`, { method: 'POST' });\n"
        "}\n"
    )
    (scaffold / "index.html").write_text("<html></html>")
    (scaffold / "server").mkdir()
    (scaffold / "server" / "index.js").write_text(
        "import express from 'express';\n"
        "const app = express();\n"
        "const configRoute = await loadRequiredRouter('./routes/config.js', {});\n"
        "app.use('/api/config', configRoute.router);\n"
    )
    (scaffold / "server" / "routes").mkdir()
    (scaffold / "server" / "routes" / "config.js").write_text(
        "import { Router } from 'express';\n"
        "const router = Router();\n"
        "router.get('/', (_req, res) => res.json({ config: {} }));\n"
        "router.put('/:slug', (_req, res) => res.status(500).json({ ok: false }));\n"
        "router.post('/:slug/test', (_req, res) => res.status(404).json({ ok: false }));\n"
        "export default router;\n"
    )
    (scaffold / "server" / "package.json").write_text(
        '{"name": "s", "dependencies": {"express": "^4"}}'
    )

    agent = IntegrationContractVerifierAgent()
    await agent.initialize()

    async def mock_boot_wait(_cmd, _cwd, _env, _port):
        return True, None, "server up", ""

    async def mock_curl(path, _port, method="GET"):
        if path == "/api/config":
            return 200, True, ""
        if path == "/api/config/:*" and method == "PUT":
            return 500, True, ""
        if path == "/api/config/:*/test" and method == "POST":
            return 404, True, ""
        return 200, True, ""

    monkeypatch.setattr(agent, "_boot_and_wait", mock_boot_wait)
    monkeypatch.setattr(agent, "_curl_route", mock_curl)

    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes", out
    assert "POST /api/config/:slug/test" in out.get("backend_routes", [])
    assert all(issue["issue"] != "missing" for issue in out.get("issues", []))


@pytest.mark.asyncio
async def test_issue_to_dict_roundtrip():
    issue = RouteIssue(
        frontend_path="/api/queue",
        method="GET",
        issue="missing",
        http_status=404,
        detail="Not Found",
    )
    agent = IntegrationContractVerifierAgent()
    d = agent._issue_to_dict(issue)
    assert d["frontend_path"] == "/api/queue"
    assert d["issue"] == "missing"
    assert d["http_status"] == 404
