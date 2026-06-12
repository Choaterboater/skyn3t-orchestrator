"""Phase 3 critical security regressions for the web control plane.

Covers:
  C4 — Control plane defaults: remote access requires SKYN3T_WEB_TOKEN unless
       the operator explicitly opts in to unauthenticated loopback mode.
  C10 — Scheduled-delivery bridge is registered at application startup.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import skyn3t.web.app as web_app
from skyn3t.config.settings import get_settings


def test_remote_access_requires_token_by_default(monkeypatch):
    """With no web token and no loopback opt-in, /api/status must 401."""
    monkeypatch.setenv("SKYN3T_ALLOW_UNAUTHENTICATED_LOOPBACK", "0")
    get_settings.cache_clear()

    with TestClient(web_app.app) as client:
        response = client.get("/api/status")

    assert response.status_code == 401
    detail = response.json().get("error", response.text)
    assert "SKYN3T_WEB_TOKEN" in detail


def test_loopback_opt_in_restores_unauthenticated_access(monkeypatch):
    """When explicitly opted in, loopback requests are allowed without token."""
    monkeypatch.setenv("SKYN3T_ALLOW_UNAUTHENTICATED_LOOPBACK", "1")
    get_settings.cache_clear()

    with TestClient(web_app.app) as client:
        response = client.get("/api/status")

    assert response.status_code != 401


def test_scheduled_delivery_bridge_registered_at_boot():
    """C10: The lifespan must subscribe the scheduled-delivery bridge."""
    with TestClient(web_app.app) as client:
        bridge = getattr(client.app.state, "scheduled_delivery_bridge", None)

    assert bridge is not None
    assert callable(bridge)
