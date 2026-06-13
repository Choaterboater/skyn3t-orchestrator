"""Regression tests for two web-layer bugs in skyn3t/web/app.py:

1. The chunked / no-Content-Length request-size cap was illusory: the wrapped
   ASGI receive counted bytes but still returned the message, so the route
   buffered the whole oversized body and ran its side effects BEFORE the
   middleware overrode the response with a 413. The fix aborts hard from
   inside receive so the route never executes.

2. enforce_web_access only exempted OPTIONS and /webhooks/, so the Discord
   interaction endpoint /api/discord/interactions was web-token-gated and
   Discord's remote, tokenless servers got a 401 before the endpoint's own
   Ed25519 signature check could run. The fix exempts that path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import skyn3t.web.app as web_app


@pytest.fixture(autouse=True)
def _reset_rate_buckets():
    web_app._RATE_BUCKETS.clear()
    yield
    web_app._RATE_BUCKETS.clear()


# ─── 1. Oversized chunked body must NOT run route side effects ───────────
def test_chunked_oversized_body_short_circuits_before_side_effects(monkeypatch):
    """A chunked POST over the cap must 413 *and* the route must never run.

    Before the fix the middleware let call_next() execute the route (which
    buffers the full body and persists), then overrode the response with a
    413 — so the side effect had already happened. We assert the route's
    expensive dependency (_get_rag_engine) is never invoked.
    """
    # No web token so the request isn't blocked by the auth gate first.
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True))

    called = {"engine": False}

    async def _spy_get_rag_engine(request):
        called["engine"] = True
        raise AssertionError("route side effect ran despite oversized body")

    monkeypatch.setattr(web_app, "_get_rag_engine", _spy_get_rag_engine)

    client = TestClient(web_app.app)
    oversized = b"x" * (web_app.MAX_REQUEST_BODY_BYTES + 1024)
    # TestClient streams an iterator body and omits Content-Length, exercising
    # the chunked-cap path.
    response = client.post(
        "/api/rag/add",
        content=iter([oversized]),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert "too large" in response.json()["error"]
    assert called["engine"] is False, "rag_add route executed before the 413"


# ─── 2. Normal-sized chunked body still passes the size middleware ───────
def test_normal_chunked_body_passes_size_middleware(monkeypatch):
    """The hard abort must not break legitimate chunked uploads under the cap."""
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True))

    client = TestClient(web_app.app)
    small = b'{"idea": "small chunked body"}'
    response = client.post(
        "/api/proposals/feature",
        content=iter([small]),
        headers={"Content-Type": "application/json"},
    )
    # Anything but 413 means the size middleware let it through.
    assert response.status_code != 413


# ─── 3. Discord interactions endpoint is exempt from the web-token gate ──
def test_discord_interactions_exempt_from_web_token_gate(monkeypatch):
    """With a web token configured and none supplied, every other /api route
    is 401'd by enforce_web_access. /api/discord/interactions must instead
    reach its own handler — proven here by the 503 'discord not configured'
    response (public key unset) rather than the gate's 401."""
    monkeypatch.setattr(
        web_app,
        "get_settings",
        lambda: SimpleNamespace(web_token="secret-web-token", allow_unauthenticated_loopback=True, discord_public_key=None),
    )

    client = TestClient(web_app.app)
    response = client.post(
        "/api/discord/interactions",
        json={"type": 1},
    )

    # Must NOT be the auth-gate 401; must reach the endpoint, which 503s
    # because discord_public_key is unset.
    assert response.status_code == 503
    assert "discord not configured" in response.json()["error"]


# ─── 4. Other /api routes remain gated when a web token is set ───────────
def test_other_api_routes_still_gated(monkeypatch):
    """Sanity: the Discord exemption must not loosen the gate elsewhere."""
    monkeypatch.setattr(
        web_app,
        "get_settings",
        lambda: SimpleNamespace(web_token="secret-web-token", allow_unauthenticated_loopback=True),
    )

    client = TestClient(web_app.app)
    response = client.get("/api/status")
    assert response.status_code == 401


class _FakeOrchestrator:
    agents: dict = {}


# ─── 5. 500 responses must not leak internal exception text ────────────────
def test_500_errors_use_safe_response_without_exception_text(monkeypatch):
    """H8: agent_create must route unexpected exceptions through
    _safe_error_response() rather than returning the raw exception string."""
    monkeypatch.setattr(
        web_app,
        "get_settings",
        lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("sensitive internal path /etc/secrets")

    monkeypatch.setattr(web_app, "orchestrator", _FakeOrchestrator())
    monkeypatch.setitem(web_app.__dict__, "get_custom_store", None)
    from skyn3t.config import custom_agents

    monkeypatch.setattr(custom_agents, "get_custom_store", _boom)

    client = TestClient(web_app.app)
    response = client.post("/api/agents", json={"name": "x", "base_type": "blank"})

    assert response.status_code == 500
    body = response.json()
    assert body.get("error") == "internal error"
    assert "correlation_id" in body
    assert "sensitive internal path" not in response.text


# ─── 6. Audit log query endpoint is wired ───────────────────────────────
def test_audit_endpoint_returns_entries(monkeypatch):
    monkeypatch.setattr(
        web_app,
        "get_settings",
        lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True),
    )

    from skyn3t.security.audit import get_audit_log

    audit = get_audit_log()
    audit.record("alice", "login", "dashboard", "success")
    audit.record("bob", "task_submit", "task-1", "denied")

    client = TestClient(web_app.app)
    response = client.get("/api/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 2
    assert any(e["actor"] == "alice" for e in body["entries"])

    filtered = client.get("/api/audit?actor=bob")
    assert filtered.status_code == 200
    assert all(e["actor"] == "bob" for e in filtered.json()["entries"])


# ─── 7. Encrypted secret store HTTP surface ─────────────────────────────
def test_secret_store_endpoints(monkeypatch):
    monkeypatch.setenv("SKYN3T_MASTER_KEY", "test-master-key-for-secrets-32bytes!")
    monkeypatch.setattr(
        web_app,
        "get_settings",
        lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True),
    )
    # Reset singleton so the new env key is picked up.
    from skyn3t.security import secrets as secrets_module

    secrets_module._secret_store_singleton = None

    client = TestClient(web_app.app)

    create = client.post("/api/secrets", json={"name": "api-key", "value": "super-secret"})
    assert create.status_code == 200

    listing = client.get("/api/secrets")
    assert listing.status_code == 200
    names = {s["name"] for s in listing.json()["secrets"]}
    assert "api-key" in names
    assert all(s["value"] == "***REDACTED***" for s in listing.json()["secrets"])

    single = client.get("/api/secrets/api-key")
    assert single.status_code == 200
    assert single.json()["value"] == "***REDACTED***"

    deleted = client.delete("/api/secrets/api-key")
    assert deleted.status_code == 200
    assert client.get("/api/secrets/api-key").status_code == 404


# ─── 8. Studio build cancellation endpoint ──────────────────────────────
def test_studio_cancel_endpoint_sets_cancelled_status(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        web_app,
        "get_settings",
        lambda: SimpleNamespace(
            web_token=None,
            allow_unauthenticated_loopback=True,
            projects_dir=tmp_path / "projects",
        ),
    )
    client = TestClient(web_app.app)

    runner = web_app._get_studio_runner(web_app.app)
    (tmp_path / "projects" / "cancel-me").mkdir(parents=True)
    manifest = {
        "slug": "cancel-me",
        "status": "running",
        "stages": [],
        "history": [],
        "artifacts": [],
    }
    runner._save_manifest(tmp_path / "projects" / "cancel-me", manifest)

    resp = client.post("/api/studio/projects/cancel-me/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    updated = runner.get_project("cancel-me")
    assert updated["status"] == "cancelled"
