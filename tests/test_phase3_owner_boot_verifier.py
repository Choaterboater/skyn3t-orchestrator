"""Phase 3 tests for BootVerifierAgent functional-smoke gate.

Covers the new contract surface added in Phase 3:

- ``_derive_primary_routes`` static parse of server/routes/*.js (express
  ``router.<verb>``) and frontend ``fetch('/api/...')`` / axios call sites.
- ``functional_smoke`` output key shape + verdict folding.
- Default-on guard via ``SKYN3T_VERIFY_FUNCTIONAL`` and degrade-to-skipped
  behavior when no routes are derivable / Playwright is absent.

These tests are pure-Python: the route derivation is a static parse, and the
verdict-folding tests stub the network round-trips so no server is booted and
no real data dir is touched. The existing happy path is exercised by the
end-to-end pipeline tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from skyn3t.agents.boot_verifier import (
    BootVerifierAgent,
    _functional_verify_enabled,
)


def _make_agent() -> BootVerifierAgent:
    return BootVerifierAgent(name="test-boot-verifier-phase3")


# ─── _functional_verify_enabled (env flag) ─────────────────────────────


def test_functional_verify_default_on(monkeypatch):
    monkeypatch.delenv("SKYN3T_VERIFY_FUNCTIONAL", raising=False)
    assert _functional_verify_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF", "False"])
def test_functional_verify_off_values(monkeypatch, val):
    monkeypatch.setenv("SKYN3T_VERIFY_FUNCTIONAL", val)
    assert _functional_verify_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", ""])
def test_functional_verify_on_values(monkeypatch, val):
    monkeypatch.setenv("SKYN3T_VERIFY_FUNCTIONAL", val)
    assert _functional_verify_enabled() is True


# ─── _derive_primary_routes: server/routes/*.js ────────────────────────


def test_derive_routes_from_express_router(tmp_path: Path):
    routes_dir = tmp_path / "server" / "routes"
    routes_dir.mkdir(parents=True)
    (routes_dir / "todos.js").write_text(
        "import { Router } from 'express';\n"
        "const router = Router();\n"
        "router.get('/', (req, res) => res.json([]));\n"
        "router.get('/:id', (req, res) => res.json({}));\n"
        "router.post('/', (req, res) => res.status(201).json({}));\n"
        "router.patch('/:id', (req, res) => res.json({}));\n"
        "router.delete('/:id', (req, res) => res.status(204).end());\n"
        "export default router;\n",
        encoding="utf-8",
    )
    routes = BootVerifierAgent._derive_primary_routes(tmp_path)
    paths = {(r["method"], r["path"]) for r in routes}
    assert ("GET", "/api/todos") in paths
    assert ("POST", "/api/todos") in paths
    assert ("GET", "/api/todos/:id") in paths
    assert ("PATCH", "/api/todos/:id") in paths
    assert ("DELETE", "/api/todos/:id") in paths
    # entity is derived from the router filename
    assert all(r["entity"] == "todos" for r in routes)


def test_derive_routes_absolute_api_path_kept(tmp_path: Path):
    routes_dir = tmp_path / "server" / "routes"
    routes_dir.mkdir(parents=True)
    (routes_dir / "widgets.js").write_text(
        "app.get('/api/widgets/all', (req, res) => res.json([]));\n",
        encoding="utf-8",
    )
    routes = BootVerifierAgent._derive_primary_routes(tmp_path)
    assert ("GET", "/api/widgets/all") in {(r["method"], r["path"]) for r in routes}


# ─── _derive_primary_routes: frontend fetch/axios ──────────────────────


def test_derive_routes_from_frontend_fetch(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "api.js").write_text(
        "export const load = () => fetch('/api/items');\n"
        "export const create = (b) => fetch('/api/items', "
        "{ method: 'POST', body: JSON.stringify(b) });\n",
        encoding="utf-8",
    )
    routes = BootVerifierAgent._derive_primary_routes(tmp_path)
    paths = {r["path"] for r in routes}
    assert "/api/items" in paths
    assert any(r["entity"] == "items" for r in routes)


def test_derive_routes_from_axios_verbs(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "client.ts").write_text(
        "axios.get('/api/users');\n"
        "axios.post('/api/users', payload);\n",
        encoding="utf-8",
    )
    routes = BootVerifierAgent._derive_primary_routes(tmp_path)
    pm = {(r["method"], r["path"]) for r in routes}
    assert ("GET", "/api/users") in pm
    assert ("POST", "/api/users") in pm


def test_derive_routes_empty_for_static_spa(tmp_path: Path):
    (tmp_path / "index.html").write_text("<!doctype html><div id=app></div>")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.jsx").write_text("document.getElementById('app').textContent='hi';")
    routes = BootVerifierAgent._derive_primary_routes(tmp_path)
    assert routes == []


def test_derive_routes_ignores_node_modules(tmp_path: Path):
    nm = tmp_path / "src" / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "lib.js").write_text("fetch('/api/should_not_appear');")
    routes = BootVerifierAgent._derive_primary_routes(tmp_path)
    assert all("should_not_appear" not in r["path"] for r in routes)


# ─── _functional_smoke_api: verdict folding ────────────────────────────


def test_functional_smoke_api_all_ok_yes(monkeypatch):
    agent = _make_agent()

    async def fake_req(port, method, path, body=None):
        # POST returns an id so the round-trip can PATCH/DELETE it.
        if method == "POST":
            return 201, '{"id": "abc123"}'
        return 200, "[]"

    monkeypatch.setattr(agent, "_http_request", fake_req)
    routes = [
        {"method": "GET", "path": "/api/todos", "entity": "todos"},
        {"method": "POST", "path": "/api/todos", "entity": "todos"},
        {"method": "PATCH", "path": "/api/todos/:id", "entity": "todos"},
        {"method": "DELETE", "path": "/api/todos/:id", "entity": "todos"},
    ]
    result = asyncio.run(agent._functional_smoke_api(3100, routes))
    assert result["ran"] is True
    assert result["verdict"] == "yes"
    assert all(c["ok"] for c in result["checks"])
    # PATCH/DELETE targeted the created id, never a real data dir.
    item_checks = [c for c in result["checks"] if c["route"].endswith("abc123")]
    assert {c["method"] for c in item_checks} == {"PATCH", "DELETE"}


def test_functional_smoke_api_5xx_folds_to_no(monkeypatch):
    agent = _make_agent()

    async def fake_req(port, method, path, body=None):
        # The 'boots but 502s on every /api call' false-pass class.
        return 502, "Bad Gateway"

    monkeypatch.setattr(agent, "_http_request", fake_req)
    routes = [
        {"method": "GET", "path": "/api/todos", "entity": "todos"},
        {"method": "POST", "path": "/api/todos", "entity": "todos"},
    ]
    result = asyncio.run(agent._functional_smoke_api(3100, routes))
    assert result["ran"] is True
    assert result["verdict"] == "no"
    assert any(not c["ok"] for c in result["checks"])


def test_functional_smoke_api_dead_connection_folds_to_no(monkeypatch):
    agent = _make_agent()

    async def fake_req(port, method, path, body=None):
        return 0, ""  # connection refused / timeout

    monkeypatch.setattr(agent, "_http_request", fake_req)
    routes = [{"method": "GET", "path": "/api/todos", "entity": "todos"}]
    result = asyncio.run(agent._functional_smoke_api(3100, routes))
    assert result["verdict"] == "no"


def test_functional_smoke_api_4xx_tolerated(monkeypatch):
    """A 404/422 means the server is alive and routing — beyond liveness.
    The functional gate only hard-fails on 5xx / dead connection."""
    agent = _make_agent()

    async def fake_req(port, method, path, body=None):
        return 422, '{"error":"validation"}'

    monkeypatch.setattr(agent, "_http_request", fake_req)
    routes = [
        {"method": "GET", "path": "/api/todos", "entity": "todos"},
        {"method": "POST", "path": "/api/todos", "entity": "todos"},
    ]
    result = asyncio.run(agent._functional_smoke_api(3100, routes))
    assert result["verdict"] == "yes"


def test_functional_smoke_api_no_collection_skipped(monkeypatch):
    """Only item-level (:id) routes, no collection → nothing to exercise."""
    agent = _make_agent()

    async def fake_req(port, method, path, body=None):  # pragma: no cover
        raise AssertionError("should not be called when no collection")

    monkeypatch.setattr(agent, "_http_request", fake_req)
    routes = [{"method": "GET", "path": "/api/todos/:id", "entity": "todos"}]
    result = asyncio.run(agent._functional_smoke_api(3100, routes))
    assert result["ran"] is False
    assert result["verdict"] == "skipped"


# ─── _functional_smoke_spa: Playwright degrade ─────────────────────────


def test_functional_smoke_spa_skips_without_playwright(monkeypatch):
    agent = _make_agent()
    # Force the static check to behave as if Playwright is unavailable.
    monkeypatch.setattr(
        BootVerifierAgent, "_spa_dom_mutation_check", staticmethod(lambda port: None)
    )
    result = asyncio.run(agent._functional_smoke_spa(3100))
    assert result["ran"] is False
    assert result["verdict"] == "skipped"
    assert result["spa_dom_mutated"] is None


def test_functional_smoke_spa_mutation_yes(monkeypatch):
    agent = _make_agent()
    monkeypatch.setattr(
        BootVerifierAgent,
        "_spa_dom_mutation_check",
        staticmethod(lambda port: (True, "clicked=True nodes 10->14")),
    )
    result = asyncio.run(agent._functional_smoke_spa(3100))
    assert result["ran"] is True
    assert result["verdict"] == "yes"
    assert result["spa_dom_mutated"] is True


def test_functional_smoke_spa_no_mutation_not_blocking(monkeypatch):
    """A static-render SPA that doesn't mutate is recorded but NOT failed
    (verdict 'skipped', never 'no') — never harsher than today's liveness."""
    agent = _make_agent()
    monkeypatch.setattr(
        BootVerifierAgent,
        "_spa_dom_mutation_check",
        staticmethod(lambda port: (False, "no interactive control found; nodes=12")),
    )
    result = asyncio.run(agent._functional_smoke_spa(3100))
    assert result["verdict"] != "no"
    assert result["spa_dom_mutated"] is False


# ─── execute(): additive shape preserved on skip paths ─────────────────


def test_execute_unknown_carries_functional_smoke_skipped(tmp_path: Path):
    """The existing kind=unknown skip path keeps its verdict but now also
    carries an additive functional_smoke=skipped key."""
    agent = _make_agent()
    (tmp_path / "index.html").write_text("<!doctype html>")
    from skyn3t.core.agent import TaskRequest

    task = TaskRequest(title="boot", input_data={"scaffold_dir": str(tmp_path)})
    result = asyncio.run(agent.execute(task))
    assert result.success is True
    assert result.output["verdict"] == "skipped"
    fs = result.output.get("functional_smoke")
    assert fs is not None
    assert fs["verdict"] == "skipped"
    assert fs["ran"] is False


def test_sample_body_is_generic_and_serializable():
    body = BootVerifierAgent._sample_body_for("todos")
    import json as _json

    # Must JSON-serialize cleanly for a POST and not reference a real schema.
    encoded = _json.loads(_json.dumps(body))
    assert isinstance(encoded, dict)
    assert "name" in encoded
