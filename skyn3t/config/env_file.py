"""Read/write operator settings in the local ``.env`` file."""

from __future__ import annotations

from pathlib import Path


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
