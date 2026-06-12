"""Tests for web-layer hardening: request size limits, security headers,
favicon stub, limit clamping, WS payload caps, rate limiting, sanitized
error responses, and GitHub webhook delivery dedup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import skyn3t.cortex as cortex_mod
import skyn3t.web.app as web_app
from skyn3t.cortex.proposals import ProposalStore
from skyn3t.integrations import github_webhook as gh_webhook


@pytest.fixture(autouse=True)
def _reset_runtime_state(monkeypatch, tmp_path):
    """Ensure each test starts from a clean slate.

    - Clear rate-limit buckets so 429 tests don't bleed across tests.
    - Clear webhook delivery dedup so dedup tests aren't poisoned by
      earlier runs.
    - Force loopback fallback (no web_token) so TestClient can reach
      every route without bootstrapping a session cookie.
    - Isolate Cortex proposal writes so endpoint tests don't pollute
      the real live proposal store under data/proposals/.
    """
    web_app._RATE_BUCKETS.clear()
    gh_webhook._seen_deliveries.clear()
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True))
    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr(cortex_mod, "get_store", lambda: store)
    monkeypatch.setattr("skyn3t.cortex.proposals._store", store)
    yield
    web_app._RATE_BUCKETS.clear()
    gh_webhook._seen_deliveries.clear()


# ─── 1. Request size middleware: oversized body → 413 ────────────────
def test_oversized_content_length_returns_413():
    client = TestClient(web_app.app)
    too_big = web_app.MAX_REQUEST_BODY_BYTES + 1
    # Send a small body but spoof Content-Length so the middleware
    # rejects before consuming the stream.
    response = client.post(
        "/api/proposals/feature",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": str(too_big)},
    )
    assert response.status_code == 413
    assert "too large" in response.json()["error"]


# ─── 2. Request size middleware: normal body passes ──────────────────
def test_normal_content_length_passes_size_check():
    client = TestClient(web_app.app)
    response = client.post(
        "/api/proposals/feature",
        json={"idea": "improve onboarding"},
    )
    # Anything other than 413 means the size middleware allowed it through.
    # Endpoint may still 400 (no orchestrator/feature suggester wiring) or
    # 200/429 — that's fine for this test.
    assert response.status_code != 413


# ─── 2b. Chunked / no-Content-Length oversized body → 413 ────────────
def test_chunked_oversized_body_returns_413():
    """Without a Content-Length header, the middleware must still cap
    the streamed body size — otherwise a malicious chunked POST can
    push unlimited bytes through."""
    client = TestClient(web_app.app)
    oversized = b"x" * (web_app.MAX_REQUEST_BODY_BYTES + 1024)
    # TestClient's stream chunks the body and omits Content-Length.
    response = client.post(
        "/api/proposals/feature",
        content=iter([oversized]),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413
    assert "too large" in response.json()["error"]


# ─── 3. Security headers on root ─────────────────────────────────────
def test_root_returns_security_headers():
    client = TestClient(web_app.app)
    response = client.get("/")
    assert response.status_code == 200
    csp = response.headers.get("Content-Security-Policy")
    assert csp is not None
    assert "default-src 'self'" in csp
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"


# ─── 4. Favicon route returns 204, not HTML ──────────────────────────
def test_favicon_returns_204():
    client = TestClient(web_app.app)
    response = client.get("/favicon.ico")
    assert response.status_code == 204
    # 204 must not have a body.
    assert response.content == b""


# ─── 5. /traces clamps oversize limit param ──────────────────────────
def test_traces_clamps_oversize_limit():
    client = TestClient(web_app.app)
    response = client.get("/traces?limit=99999")
    assert response.status_code == 200
    assert "traces" in response.json()


# ─── 6. /api/memory/insights clamps oversize limit param ─────────────
def test_memory_insights_clamps_oversize_limit():
    client = TestClient(web_app.app)
    response = client.get("/api/memory/insights?limit=999999")
    assert response.status_code == 200
    assert "insights" in response.json()


# ─── 7. WS oversized frame → error reply ─────────────────────────────
def test_ws_rejects_oversized_frame():
    client = TestClient(web_app.app)
    big_payload = "x" * (web_app.MAX_WS_FRAME_BYTES + 1024)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(big_payload)
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "frame too large" in msg["error"]


# ─── 8. WS invalid JSON → error reply ────────────────────────────────
def test_ws_rejects_invalid_json():
    client = TestClient(web_app.app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text("this is not json {")
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "invalid JSON" in msg["error"]


# ─── 9. Rate limit on /api/proposals/feature ─────────────────────────
def test_proposals_feature_rate_limit_returns_429(monkeypatch):
    # The endpoint calls FeatureSuggester, which can be slow; stub it so the
    # rate limiter is the only thing under test.
    class FastSuggester:
        def file_user_idea(self, idea, source):
            return "proposal-1"

    monkeypatch.setattr(web_app, "orchestrator", SimpleNamespace(_feature_suggester=FastSuggester()))

    client = TestClient(web_app.app)
    seen_429 = False
    for _ in range(15):
        response = client.post(
            "/api/proposals/feature",
            json={"idea": "spam idea"},
        )
        if response.status_code == 429:
            seen_429 = True
            assert "rate limit" in response.json()["error"]
            break
    assert seen_429, "expected at least one 429 from /api/proposals/feature within 15 requests"


# ─── 10. Sanitized error response carries correlation_id, no stack frames
def test_sanitized_error_response_hides_stack_frames(monkeypatch):
    async def boom_reset_cortex():
        # Internal path looks like a stack frame would expose:
        # /Users/.../skyn3t/cortex/store.py — we want to be sure it's hidden.
        raise RuntimeError("/Users/secret/path/cortex.py: kaboom internal trace")

    fake_orchestrator = SimpleNamespace(reset_cortex=boom_reset_cortex)
    monkeypatch.setattr(web_app, "orchestrator", fake_orchestrator)

    # Stub get_store to return a usable async object so we reach reset_cortex.
    class FakeStore:
        async def cancel_inflight(self):
            return {"cancelled": 0}

        async def resume_inflight(self):
            return {"requeued": 0}

    import skyn3t.cortex as cortex_mod

    monkeypatch.setattr(cortex_mod, "get_store", lambda: FakeStore())

    client = TestClient(web_app.app)
    response = client.post("/api/services/reset")

    assert response.status_code == 500
    body = response.json()
    assert "correlation_id" in body
    assert isinstance(body["correlation_id"], str) and body["correlation_id"]
    # No leaked internal text.
    serialized = json.dumps(body)
    assert "kaboom" not in serialized
    assert "/Users/secret" not in serialized
    assert "Traceback" not in serialized
    assert "RuntimeError" not in serialized


# ─── 11. GitHub webhook delivery dedup ───────────────────────────────
def test_github_webhook_dedup_returns_duplicate(monkeypatch):
    secret = "test"
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

    payload = json.dumps({"action": "opened", "number": 1}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    headers = {
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": sig,
        "X-GitHub-Delivery": "delivery-abc-123",
        "Content-Type": "application/json",
    }

    client = TestClient(web_app.app)
    first = client.post("/webhooks/github", content=payload, headers=headers)
    assert first.status_code == 200
    assert first.json().get("duplicate") is not True

    second = client.post("/webhooks/github", content=payload, headers=headers)
    assert second.status_code == 200
    assert second.json().get("duplicate") is True
