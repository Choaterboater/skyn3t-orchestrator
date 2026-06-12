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
