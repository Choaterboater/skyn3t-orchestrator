"""Tests for skyn3t.security.keychain migration helper."""

from __future__ import annotations

import pytest

from skyn3t.security import keychain


@pytest.fixture
def fake_keyring(monkeypatch, tmp_path):
    """Replace keyring with an in-memory fake and point .env at tmp_path."""
    store: dict[str, str] = {}

    class FakeKeyring:
        @staticmethod
        def get_password(service, name):
            return store.get(f"{service}:{name}")

        @staticmethod
        def set_password(service, name, value):
            store[f"{service}:{name}"] = value

    monkeypatch.setattr(keychain, "_get_keyring", lambda: FakeKeyring())
    return store


def test_migrate_dotenv_secrets_moves_secret_values(tmp_path, fake_keyring):
    env = tmp_path / ".env"
    env.write_text(
        "OPENAI_API_KEY=sk-openai\n"
        "PUBLIC_VAR=not-a-secret\n"
        "# a comment\n"
        "EMPTY_SECRET=\n",
        encoding="utf-8",
    )

    migrated, skipped = keychain.migrate_dotenv_secrets(env)

    assert "OPENAI_API_KEY" in migrated
    assert "OPENAI_API_KEY=sk-openai" not in env.read_text()
    assert "# OPENAI_API_KEY migrated to keychain" in env.read_text()
    assert "PUBLIC_VAR=not-a-secret" in env.read_text()
    assert "EMPTY_SECRET" in skipped
    assert fake_keyring["skyn3t:OPENAI_API_KEY"] == "sk-openai"
    assert (tmp_path / ".env.keychain-migration-backup").exists()


def test_migrate_dry_run_does_not_modify_files(tmp_path, fake_keyring):
    env = tmp_path / ".env"
    original = "OPENAI_API_KEY=sk-test\n"
    env.write_text(original, encoding="utf-8")

    migrated, _ = keychain.migrate_dotenv_secrets(env, dry_run=True)

    assert "OPENAI_API_KEY" in migrated
    assert env.read_text() == original
    assert not (tmp_path / ".env.keychain-migration-backup").exists()


def test_migrate_without_keyring_raises_runtime_error(tmp_path, monkeypatch):
    monkeypatch.setattr(keychain, "_get_keyring", lambda: None)
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        keychain.migrate_dotenv_secrets(env)


def test_rotate_keychain_secret_updates_value(fake_keyring):
    keychain.set_keychain_secret("FOO", "old")
    assert keychain.get_keychain_secret("FOO") == "old"
    keychain.rotate_keychain_secret("FOO", "new")
    assert keychain.get_keychain_secret("FOO") == "new"
