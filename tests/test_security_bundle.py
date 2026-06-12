from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import WebSocketException
from fastapi.testclient import TestClient
from starlette.datastructures import URL

import skyn3t.web.app as web_app
from skyn3t.core.events import EventBus
from skyn3t.integrations import github_webhook
from skyn3t.integrations.email_agent import EmailAgent
from skyn3t.security.secrets import SecretStore


def test_http_auth_requires_token_when_configured(monkeypatch):
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token="secret-token", allow_unauthenticated_loopback=True))
    client = TestClient(web_app.app)

    response = client.get("/api/status")

    assert response.status_code == 401
    assert "Provide ?token" in response.json()["error"]


def test_root_token_bootstrap_sets_cookie_and_redirects(monkeypatch):
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token="secret-token", allow_unauthenticated_loopback=True))
    client = TestClient(web_app.app)

    response = client.get("/?token=secret-token", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/")
    assert "skyn3t_session=secret-token" in response.headers["set-cookie"]

    authed = client.get("/api/status")
    assert authed.status_code == 200


def test_header_auth_allows_api_access(monkeypatch):
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token="secret-token", allow_unauthenticated_loopback=True))
    client = TestClient(web_app.app)

    response = client.get("/api/status", headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200


def test_localhost_fallback_allows_testclient_without_token(monkeypatch):
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True))
    client = TestClient(web_app.app)

    response = client.get("/api/status")

    assert response.status_code == 200


def test_remote_requests_require_token_when_no_token_configured(monkeypatch):
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True))
    monkeypatch.setattr(web_app, "_is_loopback_host", lambda _host: False)
    client = TestClient(web_app.app)

    response = client.get("/api/status")

    assert response.status_code == 401
    assert "localhost" in response.text


def test_websocket_auth_rejects_missing_token(monkeypatch):
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token="secret-token", allow_unauthenticated_loopback=True))

    fake_ws = SimpleNamespace(
        headers={},
        query_params={},
        cookies={},
        client=SimpleNamespace(host="testclient"),
        url=URL("ws://testserver/ws"),
    )

    with pytest.raises(WebSocketException):
        web_app._authorize_websocket(fake_ws)


def test_websocket_auth_accepts_query_token(monkeypatch):
    monkeypatch.setattr(web_app, "get_settings", lambda: SimpleNamespace(web_token="secret-token", allow_unauthenticated_loopback=True))

    fake_ws = SimpleNamespace(
        headers={"origin": "http://testserver"},
        query_params={"token": "secret-token"},
        cookies={},
        client=SimpleNamespace(host="198.51.100.10"),
        url=URL("ws://testserver/ws"),
    )

    web_app._authorize_websocket(fake_ws)


@pytest.mark.asyncio
async def test_studio_project_file_rejects_prefix_escape(monkeypatch, tmp_path):
    projects_root = tmp_path / "projects"
    base = projects_root / "proj"
    other = projects_root / "proj-evil"
    base.mkdir(parents=True)
    other.mkdir(parents=True)
    (other / "secret.txt").write_text("nope", encoding="utf-8")

    runner = SimpleNamespace(
        projects_root=projects_root,
        get_project=lambda slug: {"slug": slug} if slug == "proj" else None,
    )
    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: runner)

    response = await web_app.studio_project_file("proj", "../proj-evil/secret.txt")

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_studio_project_file_accepts_legacy_same_project_prefix(monkeypatch, tmp_path):
    projects_root = tmp_path / "projects"
    base = projects_root / "proj"
    base.mkdir(parents=True)
    (base / "_clarifications.json").write_text('{"ok":true}', encoding="utf-8")

    runner = SimpleNamespace(
        projects_root=projects_root,
        get_project=lambda slug: {"slug": slug} if slug == "proj" else None,
    )
    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: runner)

    response = await web_app.studio_project_file("proj", "projects/proj/_clarifications.json")

    assert response.status_code == 200
    assert response.body.decode("utf-8") == '{"ok":true}'


@pytest.mark.asyncio
async def test_studio_project_file_rejects_symlink(monkeypatch, tmp_path):
    projects_root = tmp_path / "projects"
    base = projects_root / "proj"
    outside = tmp_path / "outside.txt"
    base.mkdir(parents=True)
    outside.write_text("hidden", encoding="utf-8")
    try:
        (base / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not supported in this test environment")

    runner = SimpleNamespace(
        projects_root=projects_root,
        get_project=lambda slug: {"slug": slug} if slug == "proj" else None,
    )
    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: runner)

    response = await web_app.studio_project_file("proj", "link.txt")

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_studio_project_preview_rejects_prefix_escape(monkeypatch, tmp_path):
    projects_root = tmp_path / "projects"
    base = projects_root / "proj"
    other = projects_root / "proj-evil"
    base.mkdir(parents=True)
    other.mkdir(parents=True)
    (other / "secret.html").write_text("<h1>nope</h1>", encoding="utf-8")

    runner = SimpleNamespace(
        projects_root=projects_root,
        get_project=lambda slug: {"slug": slug} if slug == "proj" else None,
    )
    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: runner)

    response = await web_app.studio_project_preview("proj", "../proj-evil/secret.html")

    assert response.status_code == 400


def test_studio_project_preview_sets_iframe_safe_csp(monkeypatch, tmp_path):
    projects_root = tmp_path / "projects"
    base = projects_root / "proj"
    base.mkdir(parents=True)
    (base / "index.html").write_text("<html><body><h1>demo</h1></body></html>", encoding="utf-8")

    runner = SimpleNamespace(
        projects_root=projects_root,
        get_project=lambda slug: {"slug": slug} if slug == "proj" else None,
    )
    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: runner)
    monkeypatch.setattr(
        web_app,
        "get_settings",
        lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True),
    )

    client = TestClient(web_app.app)
    response = client.get("/api/studio/projects/proj/preview/index.html")

    assert response.status_code == 200
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert "<h1>demo</h1>" in response.text


def test_webhook_route_rejects_unsigned_payload_when_secret_missing(monkeypatch):
    monkeypatch.setattr(github_webhook, "get_webhook_secret", lambda: "")
    client = TestClient(web_app.app)

    response = client.post("/webhooks/github", headers={"X-GitHub-Event": "push"}, json={"ok": True})

    assert response.status_code == 401


def test_secret_store_requires_master_key_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("SKYN3T_MASTER_KEY", raising=False)
    monkeypatch.delenv("SkyN3t_MASTER_KEY", raising=False)
    monkeypatch.delenv("SKYN3T_ALLOW_EPHEMERAL_MASTER_KEY", raising=False)

    with pytest.raises(RuntimeError, match="No master key provided"):
        SecretStore(storage_path=tmp_path / "secrets.json")


def test_secret_store_allows_ephemeral_with_explicit_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("SKYN3T_MASTER_KEY", raising=False)
    monkeypatch.delenv("SkyN3t_MASTER_KEY", raising=False)
    monkeypatch.setenv("SKYN3T_ALLOW_EPHEMERAL_MASTER_KEY", "true")

    store = SecretStore(storage_path=tmp_path / "secrets.json")
    store.set_secret("demo", "value")

    assert store.get_secret("demo") == "value"
    assert Path(tmp_path / "secrets.json").exists()


@pytest.mark.asyncio
async def test_email_agent_refuses_plaintext_smtp_login():
    agent = EmailAgent(
        event_bus=EventBus(),
        config={
            "imap_host": "imap.example.com",
            "smtp_host": "smtp.example.com",
            "email_address": "bot@example.com",
            "email_password": "pw",
            "smtp_tls": False,
            "smtp_ssl": False,
        },
    )

    with pytest.raises(RuntimeError, match="refuses SMTP login without TLS or SSL"):
        await agent.initialize()


@pytest.mark.asyncio
async def test_email_agent_send_email_rejects_plaintext_smtp_login():
    agent = EmailAgent(
        event_bus=EventBus(),
        config={
            "imap_host": "imap.example.com",
            "smtp_host": "smtp.example.com",
            "email_address": "bot@example.com",
            "email_password": "pw",
            "smtp_tls": False,
            "smtp_ssl": False,
        },
    )

    with pytest.raises(RuntimeError, match="SMTP login requires TLS or SSL"):
        await agent._send_email("user@example.com", "hello", "body")
