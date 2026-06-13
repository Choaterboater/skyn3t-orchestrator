"""Secret management with encryption at rest.

Secrets are stored encrypted using Fernet symmetric encryption.
They are loaded from environment variables or an encrypted file,
and are never logged or exposed in task output.
"""

import base64
import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _env_truthy(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}

# Regex to detect common secret patterns in output
_SECRET_PATTERNS = [
    re.compile(r"[A-Za-z0-9_]{20,}[_-][A-Za-z0-9]{20,}"),  # API key style
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                     # GitHub PAT
    re.compile(r"sk-[a-zA-Z0-9]{48}"),                      # OpenAI key
    re.compile(r"sk-ant-[a-zA-Z0-9_-]{40,}"),               # Anthropic key
    re.compile(r"Bearer\s+[A-Za-z0-9\-_]{20,}"),           # Bearer token
    re.compile(r"Basic\s+[A-Za-z0-9+/]{20,}={0,2}"),       # Basic auth
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),  # JWT
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # email
]


def redact_text(text: str, *, secret_values: Optional[List[str]] = None) -> str:
    """Redact sensitive values and common credential patterns from text."""
    redacted = text
    for value in secret_values or []:
        if len(value) < 4:
            continue
        redacted = redacted.replace(value, "***REDACTED***")
        if len(value) > 16:
            redacted = redacted.replace(value[:8], "***")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("***REDACTED***", redacted)
    return redacted


@dataclass
class SecretEntry:
    """A single secret entry with metadata."""

    name: str
    value: str
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    expires_at: Optional[datetime] = None
    rotated_from: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return _utcnow() >= _ensure_utc(self.expires_at)

    def to_dict(self, mask_value: bool = True) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": "***REDACTED***" if mask_value else self.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "rotated_from": self.rotated_from,
            "metadata": self.metadata,
        }


class SecretStore:
    """Encrypted secret storage with rotation support.

    Secrets are kept in memory after decryption and are never
    written to logs or returned in task output.
    """

    # Standardize on the all-uppercase prefix. The mixed-case
    # ``SkyN3t_SECRET_`` form is read for back-compat but new code (and the
    # documented .env entries) should use ``SKYN3T_SECRET_``. On case-sensitive
    # filesystems / shells the two are different env vars.
    LEGACY_ENV_PREFIX = "SkyN3t_SECRET_"

    def __init__(
        self,
        master_key: Optional[str] = None,
        storage_path: Optional[Path] = None,
        env_prefix: str = "SKYN3T_SECRET_",
        allow_ephemeral: bool = False,
    ):
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError(
                "cryptography package is required for SecretStore. "
                "Install it with: pip install cryptography"
            )

        self._env_prefix = env_prefix
        self._storage_path = storage_path
        self._secrets: Dict[str, SecretEntry] = {}
        self._lock = threading.RLock()
        self._fernet: Optional[Fernet] = None
        self._password: Optional[str] = None
        self._redacted_names: Set[str] = set()

        # Derive Fernet key from master key or env
        key_material = (
            master_key
            or os.environ.get("SKYN3T_MASTER_KEY")
            or os.environ.get("SkyN3t_MASTER_KEY")
        )
        allow_ephemeral = allow_ephemeral or _env_truthy("SKYN3T_ALLOW_EPHEMERAL_MASTER_KEY")
        if key_material:
            # Per-secret random salts are used at encrypt/decrypt time;
            # store password to allow re-derivation. Note: changing the salt
            # scheme means existing on-disk encrypted secrets created before
            # this migration are unreadable.
            self._password = key_material
        else:
            if not allow_ephemeral:
                raise RuntimeError(
                    "No master key provided. Set SKYN3T_MASTER_KEY or pass "
                    "allow_ephemeral=True to use a non-persistent key."
                )
            # Generate a new random key (secrets won't persist across restarts)
            logger.warning(
                "No master key provided; using an ephemeral key because "
                "SKYN3T_ALLOW_EPHEMERAL_MASTER_KEY/allow_ephemeral enabled. "
                "Secrets will not persist across restarts."
            )
            key = Fernet.generate_key()  # bytes
            self._fernet = Fernet(key)

        self._load_from_environment()
        if self._storage_path and self._storage_path.exists():
            self._load_from_file()

    @staticmethod
    def _derive_fernet(password: str, salt: Optional[bytes] = None) -> Fernet:
        """Derive a Fernet key from a password string.

        Salt is required for security; if not provided, a random 16-byte
        salt is generated. Callers must persist the salt alongside the
        ciphertext to enable decryption.
        """
        if salt is None:
            salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        return Fernet(key)

    def _encrypt(self, plaintext: str) -> bytes:
        """Encrypt plaintext, prepending the 16-byte salt to ciphertext."""
        if self._password is not None:
            salt = os.urandom(16)
            fernet = self._derive_fernet(self._password, salt=salt)
            return salt + fernet.encrypt(plaintext.encode())
        if self._fernet is None:
            raise RuntimeError("SecretStore has no key material")
        # Ephemeral mode: prepend 16-byte zero-salt placeholder for format consistency
        return b"\x00" * 16 + self._fernet.encrypt(plaintext.encode())

    def _decrypt(self, blob: bytes) -> str:
        """Decrypt blob whose first 16 bytes are the salt."""
        if len(blob) < 16:
            raise InvalidToken("ciphertext too short to contain salt")
        salt = blob[:16]
        ciphertext = blob[16:]
        if self._password is not None:
            fernet = self._derive_fernet(self._password, salt=salt)
            return fernet.decrypt(ciphertext).decode()
        if self._fernet is None:
            raise RuntimeError("SecretStore has no key material")
        return self._fernet.decrypt(ciphertext).decode()

    def _load_from_environment(self) -> None:
        """Load secrets from environment variables.

        Accepts the canonical SKYN3T_SECRET_* prefix and the legacy
        SkyN3t_SECRET_* form so existing deployments don't break on upgrade.
        """
        prefixes = (self._env_prefix, self.LEGACY_ENV_PREFIX)
        for key, value in os.environ.items():
            matched = next((p for p in prefixes if key.startswith(p)), None)
            if matched is None:
                continue
            name = key[len(matched):]
            with self._lock:
                self._secrets[name] = SecretEntry(
                    name=name,
                    value=value,
                    metadata={"source": "environment"},
                )
                self._redacted_names.add(name)

    def _load_from_file(self) -> None:
        """Load encrypted secrets from storage file."""
        if not self._storage_path or (self._fernet is None and self._password is None):
            return
        try:
            data = json.loads(self._storage_path.read_text())
            for entry in data.get("secrets", []):
                try:
                    blob = base64.b64decode(entry["value"])
                    plaintext = self._decrypt(blob)
                    self._secrets[entry["name"]] = SecretEntry(
                        name=entry["name"],
                        value=plaintext,
                        created_at=_ensure_utc(datetime.fromisoformat(entry["created_at"])),
                        updated_at=_ensure_utc(datetime.fromisoformat(entry["updated_at"])),
                        expires_at=(
                            _ensure_utc(datetime.fromisoformat(entry["expires_at"]))
                            if entry.get("expires_at")
                            else None
                        ),
                        rotated_from=entry.get("rotated_from"),
                        metadata=entry.get("metadata", {}),
                    )
                    self._redacted_names.add(entry["name"])
                except InvalidToken:
                    logger.error(
                        "Failed to decrypt secret '%s' — wrong master key?",
                        entry.get("name", "?"),
                    )
        except Exception as e:
            logger.error("Failed to load secrets from %s: %s", self._storage_path, e)

    def _save_to_file(self) -> None:
        """Persist encrypted secrets to storage file."""
        if not self._storage_path or (self._fernet is None and self._password is None):
            return
        with self._lock:
            data: Dict[str, Any] = {
                "version": 2,
                "saved_at": _utcnow().isoformat(),
                "secrets": [],
            }
            for entry in self._secrets.values():
                blob = self._encrypt(entry.value)
                ciphertext = base64.b64encode(blob).decode()
                data["secrets"].append({
                    "name": entry.name,
                    "value": ciphertext,
                    "created_at": entry.created_at.isoformat(),
                    "updated_at": entry.updated_at.isoformat(),
                    "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
                    "rotated_from": entry.rotated_from,
                    "metadata": entry.metadata,
                })
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._storage_path.write_text(json.dumps(data, indent=2))

    def set_secret(
        self,
        name: str,
        value: str,
        expires_in_days: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Store a secret.

        Args:
            name: Secret identifier.
            value: The secret value.
            expires_in_days: Optional expiration.
            metadata: Optional dict of metadata.
        """
        with self._lock:
            old = self._secrets.get(name)
            rotated_from = old.value[:8] + "..." if old else None
            expires_at = None
            if expires_in_days:
                expires_at = _utcnow() + timedelta(days=expires_in_days)
            self._secrets[name] = SecretEntry(
                name=name,
                value=value,
                updated_at=_utcnow(),
                expires_at=expires_at,
                rotated_from=rotated_from,
                metadata=metadata or {},
            )
            self._redacted_names.add(name)
        self._save_to_file()
        logger.info("Secret '%s' stored (expires: %s)", name, expires_at)

    def get_secret(self, name: str) -> Optional[str]:
        """Retrieve a secret value.

        Returns None if not found or expired.
        """
        entry = self.get_secret_entry(name)
        if entry is None or entry.is_expired():
            return None
        return entry.value

    def get_secret_entry(self, name: str) -> Optional[SecretEntry]:
        """Retrieve the full secret entry (metadata + value), or None."""
        with self._lock:
            return self._secrets.get(name)

    def delete_secret(self, name: str) -> bool:
        """Delete a secret."""
        with self._lock:
            if name in self._secrets:
                del self._secrets[name]
                self._redacted_names.discard(name)
        self._save_to_file()
        return True

    def rotate_secret(
        self,
        name: str,
        new_value: str,
        expires_in_days: Optional[int] = None,
    ) -> bool:
        """Rotate a secret to a new value.

        The old value is preserved in rotated_from for audit.
        """
        with self._lock:
            old = self._secrets.get(name)
            if old is None:
                return False
            old_hash = hashlib.sha256(old.value.encode()).hexdigest()[:16]
            self.set_secret(
                name,
                new_value,
                expires_in_days=expires_in_days,
                metadata={**old.metadata, "rotated_at": _utcnow().isoformat(), "previous_hash": old_hash},
            )
        logger.info("Secret '%s' rotated", name)
        return True

    def list_secrets(self) -> List[Dict[str, Any]]:
        """List all secret names and metadata (values redacted)."""
        with self._lock:
            return [entry.to_dict(mask_value=True) for entry in self._secrets.values()]

    def redact(self, text: str) -> str:
        """Redact secret values from text output.

        Scans for known secret names and common API key patterns.
        """
        secret_values: List[str] = []
        with self._lock:
            for name in self._redacted_names:
                entry = self._secrets.get(name)
                if not entry:
                    continue
                secret_values.append(entry.value)
        return redact_text(text, secret_values=secret_values)

    def sanitize_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively redact secrets from a dict."""
        result: Dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                result[key] = self.sanitize_dict(value)
            elif isinstance(value, list):
                result[key] = self.sanitize_list(value)
            elif isinstance(value, str):
                result[key] = self.redact(value)
            else:
                result[key] = value
        return result

    def sanitize_list(self, data: List[Any]) -> List[Any]:
        """Recursively redact secrets from a list."""
        result: List[Any] = []
        for item in data:
            if isinstance(item, dict):
                result.append(self.sanitize_dict(item))
            elif isinstance(item, list):
                result.append(self.sanitize_list(item))
            elif isinstance(item, str):
                result.append(self.redact(item))
            else:
                result.append(item)
        return result

    def get_env_dict(self) -> Dict[str, str]:
        """Return secrets as an env dict for injection into subprocesses."""
        with self._lock:
            return {
                f"{self._env_prefix}{name}": entry.value
                for name, entry in self._secrets.items()
                if not entry.is_expired()
            }


# Singleton accessor so the web layer shares one encrypted store.
_secret_store_singleton: Optional[SecretStore] = None
_secret_store_lock = threading.Lock()


def get_secret_store() -> Optional[SecretStore]:
    """Return the process-wide SecretStore, or None if no master key is set.

    The store is intentionally optional: deployments that only use env vars
    don't need it, and we don't want a missing master key to crash startup.
    """
    global _secret_store_singleton
    if _secret_store_singleton is None:
        with _secret_store_lock:
            if _secret_store_singleton is None:
                from skyn3t.config.settings import get_settings

                settings = get_settings()
                storage_path = getattr(settings, "secret_storage_path", None)
                try:
                    _secret_store_singleton = SecretStore(
                        storage_path=Path(storage_path) if storage_path else None,
                        allow_ephemeral=True,
                    )
                except RuntimeError:
                    # No master key configured and ephemeral not allowed.
                    return None
    return _secret_store_singleton
