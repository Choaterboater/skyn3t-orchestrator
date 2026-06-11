"""Safe project evolution helpers.

Original local projects and original GitHub repositories are treated as
read-only. Improvements happen in local candidate workspaces, then become
approval-gated proposals only if benchmarks prove they beat the baseline.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from skyn3t.intelligence.domain_benchmark import ComparisonResult, compare_projects
from skyn3t.intelligence.domain_corpus import (
    corpus_id_for_source,
    parse_github_repo,
    source_from_uri,
)

_SKIP_COPY_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
}


@dataclass
class CandidateWorkspace:
    source_uri: str
    source_type: str
    candidate_dir: Path
    metadata_path: Path
    read_only_original: bool = True
    git_push_allowed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_uri": self.source_uri,
            "source_type": self.source_type,
            "candidate_dir": str(self.candidate_dir),
            "metadata_path": str(self.metadata_path),
            "read_only_original": self.read_only_original,
            "git_push_allowed": self.git_push_allowed,
        }


def _candidate_root() -> Path:
    try:
        from skyn3t.config.settings import get_settings

        return Path(get_settings().data_dir) / "project_candidates"
    except Exception:
        return Path("data/project_candidates")


def _ignore_copy(dir_path: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _SKIP_COPY_DIRS}


def prepare_candidate_workspace(
    source_uri: str,
    *,
    candidate_root: Optional[Path] = None,
    label: str = "",
) -> CandidateWorkspace:
    """Create a local candidate workspace without modifying the original."""

    source = source_from_uri(source_uri)
    root = candidate_root or _candidate_root()
    root.mkdir(parents=True, exist_ok=True)
    suffix = label.strip().lower().replace(" ", "-")[:32] if label else str(int(time.time()))
    candidate_dir = root / f"{corpus_id_for_source(source)}-{suffix}"
    if candidate_dir.exists():
        shutil.rmtree(candidate_dir)
    candidate_dir.mkdir(parents=True, exist_ok=True)

    local_source = Path(source.uri).expanduser()
    if source.source_type == "local" and local_source.is_dir():
        shutil.copytree(
            local_source,
            candidate_dir,
            dirs_exist_ok=True,
            ignore=_ignore_copy,
            symlinks=False,
        )
    elif source.source_type == "github":
        owner_repo = parse_github_repo(source.uri)
        metadata = {
            "github_repo": "/".join(owner_repo) if owner_repo else source.uri,
            "clone_required": True,
            "safety": "Do not push directly to the original GitHub repository.",
        }
        (candidate_dir / "GITHUB_SOURCE.json").write_text(
            json.dumps(metadata, indent=2),
            encoding="utf-8",
        )

    metadata_path = candidate_dir / ".skyn3t_candidate.json"
    metadata_path.write_text(
        json.dumps(
            {
                "source": source.to_dict(),
                "candidate_dir": str(candidate_dir),
                "read_only_original": True,
                "git_push_allowed": False,
                "created_at": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return CandidateWorkspace(
        source_uri=source.uri,
        source_type=source.source_type,
        candidate_dir=candidate_dir,
        metadata_path=metadata_path,
    )


def compare_candidate_to_baseline(
    *,
    baseline_path: str | Path,
    candidate_path: str | Path,
    brief: str,
    baseline_proof: Optional[Dict[str, Any]] = None,
    candidate_proof: Optional[Dict[str, Any]] = None,
    min_delta: int = 5,
) -> ComparisonResult:
    return compare_projects(
        baseline_path=baseline_path,
        candidate_path=candidate_path,
        brief=brief,
        baseline_proof=baseline_proof,
        candidate_proof=candidate_proof,
        min_delta=min_delta,
    )


def propose_verified_improvement(
    *,
    comparison: ComparisonResult,
    source_uri: str,
    candidate_dir: str | Path,
    proposal_store: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Create an approval-gated proposal for a verified winner."""

    if not comparison.improved:
        return None
    if proposal_store is None:
        from skyn3t.cortex import get_store

        proposal_store = get_store()
    detail = (
        "Verified project improvement candidate.\n\n"
        f"- Source/original: `{source_uri}`\n"
        f"- Candidate workspace: `{candidate_dir}`\n"
        f"- Baseline score: {comparison.baseline.score}/100\n"
        f"- Candidate score: {comparison.candidate.score}/100\n"
        f"- Delta: +{comparison.delta}\n\n"
        "Safety: original local projects and GitHub repositories were not modified. "
        "Apply only after reviewing the candidate diff/workspace."
    )
    proposal = proposal_store.create(
        kind="project_improvement",
        title=f"Promote verified improvement for {Path(str(source_uri)).name or source_uri}",
        summary=comparison.reason[:200],
        detail=detail,
        payload={
            "source_uri": source_uri,
            "candidate_dir": str(candidate_dir),
            "comparison": comparison.to_dict(),
            "read_only_original": True,
            "git_push_allowed": False,
        },
        source="project_evolution",
        requires_approval=True,
        force_requires_approval=True,
    )
    return proposal.to_public() if hasattr(proposal, "to_public") else dict(proposal)
