"""Repo target helpers for Studio project launches."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_REPO_TARGET: Dict[str, str] = {
    "local_path": "",
    "focus_file": "",
}


def _repo_target_fields(value: Any) -> tuple[str, str]:
    data = value if isinstance(value, dict) else {}
    local_path = str(data.get("local_path") or "").strip()
    focus_file = str(data.get("focus_file") or "").strip().replace("\\", "/")
    while focus_file.startswith("./"):
        focus_file = focus_file[2:]
    return local_path, focus_file


def normalize_repo_target(value: Any) -> Dict[str, str]:
    """Coerce repo target data to the small supported schema."""
    local_path, focus_file = _repo_target_fields(value)
    if not local_path:
        focus_file = ""
    return {
        "local_path": local_path,
        "focus_file": focus_file,
    }


def resolve_repo_target(value: Any) -> Dict[str, str]:
    """Validate and canonicalize a repo target for new launches."""
    local_path, focus_file = _repo_target_fields(value)
    if focus_file and not local_path:
        raise ValueError("focus file requires a repo path")

    target = normalize_repo_target(
        {"local_path": local_path, "focus_file": focus_file}
    )
    if not target["local_path"]:
        return dict(DEFAULT_REPO_TARGET)

    path = Path(target["local_path"]).expanduser()
    if not path.exists() or not path.is_dir():
        raise ValueError("repo path must point to an existing directory")

    repo_root = _git_root(path.resolve())
    if repo_root is None:
        raise ValueError("repo path must point to a local git repository")

    resolved = {
        "local_path": repo_root.as_posix(),
        "focus_file": "",
    }
    focus_file = target["focus_file"]
    if not focus_file:
        return resolved

    focus_path = Path(focus_file)
    if focus_path.is_absolute():
        raise ValueError("focus file must be relative to the repo root")
    if any(part == ".." for part in focus_path.parts):
        raise ValueError("focus file must stay inside the repo")

    candidate = (repo_root / focus_path).resolve()
    try:
        rel = candidate.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("focus file must stay inside the repo") from exc
    if not candidate.exists() or not candidate.is_file():
        raise ValueError("focus file must point to an existing file in the repo")

    resolved["focus_file"] = rel.as_posix()
    return resolved


def augment_brief_with_repo_target(brief: str, value: Any) -> str:
    """Append repo target context when a launch points at another codebase."""
    target = normalize_repo_target(value)
    if not target["local_path"]:
        return brief

    lines = [
        f"- Local git repo: {target['local_path']}",
        "- Apply code changes inside this repo when the brief calls for edits.",
    ]
    if target["focus_file"]:
        lines.append(f"- Focus file: {target['focus_file']}")
        lines.append(f"- target_file: {target['focus_file']}")

    clean_brief = str(brief or "").rstrip()
    if clean_brief:
        return clean_brief + "\n\n## Codebase target\n" + "\n".join(lines)
    return "## Codebase target\n" + "\n".join(lines)


def repo_target_stage_hints(value: Any) -> Dict[str, Any]:
    """Translate repo target data into concrete stage input hints."""
    target = normalize_repo_target(value)
    if not target["local_path"]:
        return {"repo_target": dict(DEFAULT_REPO_TARGET)}

    hints: Dict[str, Any] = {
        "repo_target": target,
        "repo_root": target["local_path"],
        "repo_label": Path(target["local_path"]).name,
    }
    if target["focus_file"]:
        hints["target_file"] = target["focus_file"]
    return hints


def _git_root(path: Path) -> Optional[Path]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    if not root:
        return None
    try:
        resolved = Path(root).expanduser().resolve()
    except Exception:
        return None
    return resolved if resolved.exists() and resolved.is_dir() else None
