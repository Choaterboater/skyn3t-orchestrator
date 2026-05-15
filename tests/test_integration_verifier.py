"""Tests for IntegrationContractVerifierAgent — frontend/backend contract check.

This agent catches the #1 bug class that slips past BuildVerifier and
BootVerifier: the frontend calls API routes the backend never implemented.
"""

from __future__ import annotations

import pytest

from skyn3t.agents.integration_verifier import (
    IntegrationContractVerifierAgent,
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
