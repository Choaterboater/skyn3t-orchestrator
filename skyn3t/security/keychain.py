"""Keychain-backed secret migration and rotation helper (C3 residual).

Provides an optional bridge between `.env` files and the system keychain
(via the ``keyring`` package when installed). Secrets migrated this way are
removed from the env file and can be rotated without leaving plaintext
values on disk.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Keys that look like live tokens / credentials and should not live in .env.
_SECRET_PATTERNS = (
    r"API_KEY",
    r"API_TOKEN",
    r"TOKEN",
    r"SECRET_KEY",
    r"SECRET",
    r"MASTER_KEY",
    r"PASSWORD",
    r"PASSWD",
    r"PASS",
    r"PRIVATE_KEY",
    r"CREDENTIAL",
)
_SECRET_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$",
    re.DOTALL,
)


def _get_keyring() -> Optional[Any]:
    try:
        import keyring  # type: ignore[import-untyped]
        return keyring
    except Exception:  # pragma: no cover
        return None


def _looks_like_secret(name: str) -> bool:
    upper = name.upper()
    return any(pat in upper for pat in _SECRET_PATTERNS)


def _service_name() -> str:
    return os.environ.get("SKYN3T_KEYRING_SERVICE", "skyn3t")


def list_keychain_secrets(service: Optional[str] = None) -> List[str]:
    """Return the names of secrets stored in the keychain for ``service``."""
    keyring = _get_keyring()
    if keyring is None:
        return []
    svc = service or _service_name()
    try:
        return keyring.get_credential_names(svc)  # type: ignore[attr-defined, no-any-return]
    except Exception:
        # Older keyring versions do not expose credential enumeration.
        return []


def get_keychain_secret(name: str, service: Optional[str] = None) -> Optional[str]:
    """Read a single secret from the system keychain."""
    keyring = _get_keyring()
    if keyring is None:
        return None
    try:
        return keyring.get_password(service or _service_name(), name)  # type: ignore[no-any-return]
    except Exception:
        logger.debug("keychain read failed for %s", name, exc_info=True)
        return None


def set_keychain_secret(
    name: str,
    value: str,
    service: Optional[str] = None,
) -> bool:
    """Write a single secret to the system keychain."""
    keyring = _get_keyring()
    if keyring is None:
        logger.warning("keyring not installed; cannot write %s", name)
        return False
    try:
        keyring.set_password(service or _service_name(), name, value)
        return True
    except Exception:
        logger.exception("keychain write failed for %s", name)
        return False


def rotate_keychain_secret(
    name: str,
    new_value: str,
    service: Optional[str] = None,
) -> bool:
    """Rotate a keychain secret in place.

    The old value is intentionally *not* retained in the keychain; callers
    should audit the rotation via SecretStore or the audit log if they need
    history.
    """
    return set_keychain_secret(name, new_value, service=service)


def migrate_dotenv_secrets(
    dotenv_path: Optional[Path] = None,
    *,
    service: Optional[str] = None,
    dry_run: bool = False,
    backup: bool = True,
) -> Tuple[Dict[str, str], List[str]]:
    """Move secret-looking values from ``.env`` into the system keychain.

    Returns a tuple of ``(migrated, skipped)``. ``migrated`` maps key names
    to the action taken ("stored" or "updated"). ``skipped`` lists keys that
    were either not secret-looking or failed to persist.

    If ``dry_run`` is True, no file or keychain changes are made.
    """
    keyring = _get_keyring()
    if keyring is None:
        raise RuntimeError(
            "keyring package is not installed. Install it with: pip install keyring"
        )

    path = dotenv_path or Path(".env")
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")

    original_lines = path.read_text(encoding="utf-8").splitlines()
    new_lines: List[str] = []
    migrated: Dict[str, str] = {}
    skipped: List[str] = []
    svc = service or _service_name()

    for line in original_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue

        m = _SECRET_RE.match(line)
        if not m:
            new_lines.append(line)
            continue

        key = m.group("key")
        value = m.group("value").strip()
        # Drop optional surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        if not _looks_like_secret(key) or not value:
            new_lines.append(line)
            if _looks_like_secret(key) and not value:
                skipped.append(key)
            continue

        existing = get_keychain_secret(key, service=svc)
        if dry_run:
            migrated[key] = "would_store" if existing is None else "would_update"
            new_lines.append(f"# {key} would be migrated to keychain (dry-run)")
            continue

        if set_keychain_secret(key, value, service=svc):
            migrated[key] = "updated" if existing is not None else "stored"
            new_lines.append(f"# {key} migrated to keychain")
        else:
            skipped.append(key)
            new_lines.append(line)

    if not dry_run and (migrated or skipped):
        if backup:
            backup_path = path.with_suffix(path.suffix + ".keychain-migration-backup")
            backup_path.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return migrated, skipped


