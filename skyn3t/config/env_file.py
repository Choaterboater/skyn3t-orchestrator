"""Read/write operator settings in the local ``.env`` file."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def env_file_path() -> Path:
    return Path(".env").resolve()


def upsert_env_setting(path: Path, key: str, value: str) -> None:
    """Insert or replace a single ``KEY=value`` line in ``.env``."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        existing_key, _existing_value = line.split("=", 1)
        if existing_key.strip() != key:
            out.append(line)
            continue
        out.append(f"{key}={value}")
        replaced = True
    if not replaced:
        if out and out[-1] != "":
            out.append("")
        out.append(f"{key}={value}")
    text = "\n".join(out).rstrip("\n") + "\n"
    path.write_text(text, encoding="utf-8")


def warn_env_file_permissions() -> None:
    """Log a security warning if ``.env`` is readable by group or others."""
    path = env_file_path()
    if not path.is_file():
        return
    try:
        mode = path.stat().st_mode
        perms = stat.S_IMODE(mode)
        if perms & 0o044:
            logger.warning(
                ".env is readable by group or others (mode %04o). "
                "Run `chmod 600 .env` and rotate any exposed credentials.",
                perms,
            )
    except Exception:
        logger.debug("could not check .env permissions", exc_info=True)
