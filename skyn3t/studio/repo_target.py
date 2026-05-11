"""Repo target helpers for Studio project launches."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

DEFAULT_REPO_TARGET: Dict[str, str] = {
    "local_path": "",
    "focus_file": "",
}
MANAGED_REPO_TARGETS_ROOT = Path("data/repo_targets")
_GITHUB_REPO_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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

    repo_root = _resolved_repo_root(target["local_path"])

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


def _resolved_repo_root(local_path: str) -> Path:
    github_spec = _parse_github_repo_target(local_path)
    if github_spec is not None:
        owner, repo, clone_url = github_spec
        repo_root = _ensure_managed_github_checkout(owner=owner, repo=repo, clone_url=clone_url)
    else:
        path = Path(local_path).expanduser()
        if not path.exists() or not path.is_dir():
            raise ValueError(
                "repo path must point to an existing directory or a supported GitHub repo URL"
            )
        local_repo_root = _git_root(path.resolve())
        if local_repo_root is None:
            raise ValueError("repo path must point to a local git repository")
        repo_root = local_repo_root
    return repo_root


def _parse_github_repo_target(value: str) -> Optional[tuple[str, str, str]]:
    raw = str(value or "").strip()
    if not raw:
        return None

    if raw.startswith("git@github.com:"):
        path = raw.split(":", 1)[1]
        clone_url = raw
    else:
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https", "ssh"}:
            return None
        host = (parsed.hostname or "").lower()
        if host not in {"github.com", "www.github.com"}:
            return None
        path = parsed.path.lstrip("/")
        clone_url = raw

    normalized_path = path.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if normalized_path.endswith(".git"):
        normalized_path = normalized_path[:-4]
    parts = [part for part in normalized_path.split("/") if part]
    if len(parts) != 2 or not all(_GITHUB_REPO_PART_RE.fullmatch(part) for part in parts):
        return None

    owner, repo = parts
    if clone_url.startswith(("http://", "https://")) and not clone_url.endswith(".git"):
        clone_url = f"https://github.com/{owner}/{repo}.git"
    return owner, repo, clone_url


def _ensure_managed_github_checkout(*, owner: str, repo: str, clone_url: str) -> Path:
    checkout_root = (MANAGED_REPO_TARGETS_ROOT / "github" / owner / repo).resolve()
    if checkout_root.exists() and _git_root(checkout_root) is None:
        shutil.rmtree(checkout_root)

    if checkout_root.exists():
        refresh_error = _refresh_managed_checkout(checkout_root)
        if refresh_error is not None:
            shutil.rmtree(checkout_root)

    if not checkout_root.exists():
        _clone_managed_checkout(clone_url=clone_url, owner=owner, repo=repo, destination=checkout_root)

    repo_root = _git_root(checkout_root)
    if repo_root is None:
        raise ValueError(f"managed checkout for {owner}/{repo} is not a valid git repository")
    return repo_root


def _refresh_managed_checkout(path: Path) -> Optional[str]:
    commands = [
        ["git", "-C", str(path), "fetch", "--depth", "1", "origin"],
        ["git", "-C", str(path), "reset", "--hard", "origin/HEAD"],
        ["git", "-C", str(path), "clean", "-fd"],
    ]
    for command in commands:
        proc = _run_command(command, timeout=120)
        if proc.returncode != 0:
            return proc.stderr.strip() or proc.stdout.strip() or "unknown git error"
    return None


def _clone_managed_checkout(
    *,
    clone_url: str,
    owner: str,
    repo: str,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    proc = _run_command(
        ["git", "clone", "--depth", "1", clone_url, str(destination)],
        timeout=180,
    )
    if proc.returncode == 0:
        return

    if destination.exists():
        shutil.rmtree(destination)

    gh_path = shutil.which("gh")
    if gh_path:
        gh_proc = _run_command(
            ["gh", "repo", "clone", f"{owner}/{repo}", str(destination), "--", "--depth", "1"],
            timeout=180,
        )
        if gh_proc.returncode == 0:
            return
        error = gh_proc.stderr.strip() or gh_proc.stdout.strip()
        if error:
            raise ValueError(f"could not clone GitHub repo: {error}")

    error = proc.stderr.strip() or proc.stdout.strip() or "unknown git error"
    raise ValueError(f"could not clone GitHub repo: {error}")


def _run_command(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
