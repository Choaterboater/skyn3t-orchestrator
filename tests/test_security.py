"""Tests for skyn3t/security/* — secrets, permissions, redact_text."""

from __future__ import annotations

import os

import pytest

from skyn3t.security.secrets import SecretStore, redact_text

# ---------------------------------------------------------------------------
# redact_text
# ---------------------------------------------------------------------------


def test_redact_text_hides_jwt():
    text = "header eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcDEF tail"
    out = redact_text(text)
    assert "eyJ" not in out
    assert "***REDACTED***" in out


def test_redact_text_hides_email():
    out = redact_text("contact alice@example.com for help")
    assert "alice@example.com" not in out
    assert "***REDACTED***" in out


def test_redact_text_hides_explicit_secret_values():
    secret = "abcd1234supersecret"
    out = redact_text(f"key={secret}", secret_values=[secret])
    assert secret not in out


def test_redact_text_skips_short_secrets():
    # Values shorter than 4 chars are ignored — they'd cause false positives.
    text = "abc value"
    out = redact_text(text, secret_values=["ab"])
    assert out == text


# ---------------------------------------------------------------------------
# SecretStore
# ---------------------------------------------------------------------------


def test_secret_store_refuses_empty_master_key_without_ephemeral():
    # Strip any inherited master key so we exercise the refusal path.
    saved = os.environ.pop("SKYN3T_MASTER_KEY", None)
    saved2 = os.environ.pop("SkyN3t_MASTER_KEY", None)
    saved3 = os.environ.pop("SKYN3T_ALLOW_EPHEMERAL_MASTER_KEY", None)
    try:
        with pytest.raises(RuntimeError, match="No master key"):
            SecretStore()
    finally:
        if saved is not None:
            os.environ["SKYN3T_MASTER_KEY"] = saved
        if saved2 is not None:
            os.environ["SkyN3t_MASTER_KEY"] = saved2
        if saved3 is not None:
            os.environ["SKYN3T_ALLOW_EPHEMERAL_MASTER_KEY"] = saved3


def test_secret_store_accepts_ephemeral_when_allowed():
    store = SecretStore(allow_ephemeral=True)
    assert store is not None


def test_secret_store_loads_canonical_env_prefix(monkeypatch):
    monkeypatch.setenv("SKYN3T_SECRET_DEMO", "supersecret")
    store = SecretStore(master_key="deadbeef-master")
    # Use the public API rather than reaching into _secrets so we test what
    # callers actually depend on.
    assert store.get_secret("DEMO") == "supersecret"


def test_secret_store_loads_legacy_mixed_case_prefix(monkeypatch):
    monkeypatch.setenv("SkyN3t_SECRET_LEGACY", "old-style")
    store = SecretStore(master_key="deadbeef-master")
    assert store.get_secret("LEGACY") == "old-style"


# ---------------------------------------------------------------------------
# Permissions — capability gate
# ---------------------------------------------------------------------------


def test_permissions_module_imports():
    """Smoke test: permissions module must at least import cleanly."""
    from skyn3t.security import permissions  # noqa: F401
