"""Regression tests for the Phase 3 must-fix follow-ups (applied after the
adversarial review):

- boot_verifier: proxy-to-external (frontend-derived) routes are soft (a 5xx
  from an unreachable sandbox upstream must NOT fold the verdict to 'no');
  owns-its-data (server-sourced) routes stay hard-gated.
- stack_templates: _filter_plan de-dupes case-insensitively (macOS/Docker
  collapse Button.jsx/button.jsx) keeping the later, richer spec.
- consistency_engine: orphan-classname scanner skips scaffolds that import an
  external CSS framework (Bootstrap/Bulma/…) instead of false-flagging them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.agents import consistency_engine as ce
from skyn3t.agents.boot_verifier import BootVerifierAgent
from skyn3t.agents.stack_templates import _filter_plan


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── boot_verifier: proxy soft-gate vs owns-data hard-gate ──────────────────

def test_frontend_proxy_route_5xx_does_not_fold_to_no(monkeypatch):
    agent = BootVerifierAgent(name="test-mustfix-boot")

    async def fake_req(port, method, path, body=None):
        return 502, ""  # unreachable proxy upstream

    monkeypatch.setattr(agent, "_http_request", fake_req)
    routes = [{"method": "GET", "path": "/api/sonos", "entity": "sonos",
               "source": "frontend"}]
    result = asyncio.run(agent._functional_smoke_api(3100, routes))
    assert result["ran"] is True
    assert result["verdict"] == "yes"  # soft — proxy upstream not our concern


def test_server_owns_data_route_5xx_folds_to_no(monkeypatch):
    agent = BootVerifierAgent(name="test-mustfix-boot2")

    async def fake_req(port, method, path, body=None):
        return 500, ""

    monkeypatch.setattr(agent, "_http_request", fake_req)
    routes = [{"method": "GET", "path": "/api/todos", "entity": "todos",
               "source": "server"}]
    result = asyncio.run(agent._functional_smoke_api(3100, routes))
    assert result["verdict"] == "no"  # owns-its-data failure is real


def test_functional_smoke_respects_deadline(monkeypatch):
    import time as _t
    agent = BootVerifierAgent(name="test-mustfix-boot3")

    async def fake_req(port, method, path, body=None):
        return 200, "{}"

    monkeypatch.setattr(agent, "_http_request", fake_req)
    routes = [{"method": "GET", "path": "/api/a", "entity": "a", "source": "server"},
              {"method": "GET", "path": "/api/b", "entity": "b", "source": "server"}]
    # An already-passed deadline truncates before any entity runs.
    result = asyncio.run(
        agent._functional_smoke_api(3100, routes, deadline=_t.monotonic() - 1)
    )
    assert result["truncated"] is True


# ── stack_templates: case-insensitive de-dup keeps the richer primitive ────

def test_filter_plan_dedupes_case_insensitively_keeping_last():
    plan = [
        ("src/components/ui/button.jsx", "base shadcn"),
        ("src/components/ui/Button.jsx", "token-driven primitive"),
        ("src/components/ui/badge.jsx", "base shadcn badge"),
    ]
    out = _filter_plan(plan)
    paths = [p for p, _ in out]
    # button.jsx / Button.jsx collapse to ONE physical file...
    assert paths.count("src/components/ui/Button.jsx") + paths.count("src/components/ui/button.jsx") == 1
    # ...and the richer (later) primitive content wins.
    chosen = [entry for entry in out if entry[0].lower() == "src/components/ui/button.jsx"][0]
    assert chosen[0] == "src/components/ui/Button.jsx"
    assert chosen[1] == "token-driven primitive"
    # the non-colliding base file survives.
    assert "src/components/ui/badge.jsx" in paths


# ── consistency_engine: external CSS framework skip ────────────────────────

def _orphan_scaffold(tmp_path: Path, *, with_bootstrap: bool) -> Path:
    root = tmp_path / "scaffold"
    _write(root / "src" / "App.jsx",
           'export default function App(){return <div className="dashboard-grid card-fancy">x</div>;}')
    _write(root / "src" / "index.css", "body { margin: 0; }")  # no backing rules
    deps = '{"bootstrap": "^5.3.0"}' if with_bootstrap else "{}"
    _write(root / "package.json", '{"name":"x","dependencies":' + deps + '}')
    return root


def test_orphan_class_scanner_flags_without_framework(tmp_path):
    root = _orphan_scaffold(tmp_path, with_bootstrap=False)
    issues = ce._scan_css_coverage_orphan_classes(root)
    assert any(i.category == "orphan_classname" for i in issues)


def test_orphan_class_scanner_skips_with_external_framework(tmp_path):
    root = _orphan_scaffold(tmp_path, with_bootstrap=True)
    issues = ce._scan_css_coverage_orphan_classes(root)
    assert issues == []
