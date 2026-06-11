"""Domain benchmark harness for comparing networking tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from skyn3t.intelligence.networking_quality import evaluate_networking_quality

_SKIP_DIRS = {
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
_TEXT_EXTS = {".md", ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".toml", ".yaml", ".yml"}


@dataclass
class BenchmarkResult:
    project_path: str
    brief: str
    score: int
    rubric_score: int
    proof_score: int
    packaging_score: int
    passed: bool
    gaps: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_path": self.project_path,
            "brief": self.brief,
            "score": self.score,
            "rubric_score": self.rubric_score,
            "proof_score": self.proof_score,
            "packaging_score": self.packaging_score,
            "passed": self.passed,
            "gaps": list(self.gaps),
            "details": dict(self.details),
        }


@dataclass
class ComparisonResult:
    baseline: BenchmarkResult
    candidate: BenchmarkResult
    improved: bool
    delta: int
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "improved": self.improved,
            "delta": self.delta,
            "reason": self.reason,
        }


def _project_root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    if (root / "scaffold").is_dir():
        return root / "scaffold"
    return root


def _read_contents(path: str | Path, *, max_files: int = 80, max_chars: int = 12000) -> Dict[str, str]:
    root = _project_root(path)
    contents: Dict[str, str] = {}
    if not root.is_dir():
        return contents
    for file_path in sorted(root.rglob("*")):
        if any(part in _SKIP_DIRS for part in file_path.parts):
            continue
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in _TEXT_EXTS and file_path.name.lower() not in {
            "dockerfile",
            ".env.example",
        }:
            continue
        try:
            rel = file_path.relative_to(root).as_posix()
        except ValueError:
            rel = file_path.name
        try:
            contents[rel] = file_path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
        except Exception:
            continue
        if len(contents) >= max_files:
            break
    return contents


def _packaging_score(path: str | Path) -> int:
    root = Path(path).expanduser().resolve()
    score = 0
    if (root / "README.md").is_file() or (_project_root(path) / "README.md").is_file():
        score += 25
    if (root / ".env.example").is_file() or (_project_root(path) / ".env.example").is_file():
        score += 25
    if (root / "Dockerfile").is_file() or (root / "docker-compose.yml").is_file():
        score += 25
    if (root / "package.json").is_file() or (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        score += 25
    return min(score, 100)


def _proof_score(proof: Optional[Dict[str, Any]]) -> int:
    if proof is None:
        return 50
    verdict = str(proof.get("verdict") or "").strip().lower()
    if proof.get("ok") is True or verdict in {"yes", "passed", "pass"}:
        return 100
    if verdict in {"skipped", "partial"}:
        return 65
    return 0


def score_project(
    project_path: str | Path,
    *,
    brief: str,
    proof: Optional[Dict[str, Any]] = None,
) -> BenchmarkResult:
    contents = _read_contents(project_path)
    rubric = evaluate_networking_quality(
        brief=brief,
        contents=contents,
        artifact_dir=Path(project_path),
    )
    rubric_score = int(rubric.score if rubric.applicable else 70)
    proof_points = _proof_score(proof)
    packaging_points = _packaging_score(project_path)
    score = int(round(rubric_score * 0.55 + proof_points * 0.30 + packaging_points * 0.15))
    gaps = list(rubric.gaps)
    if proof_points == 0:
        gaps.append("Benchmark: proof/build failed.")
    if packaging_points < 50:
        gaps.append("Benchmark: packaging/setup artifacts are weak.")
    return BenchmarkResult(
        project_path=str(Path(project_path).expanduser()),
        brief=brief,
        score=max(0, min(100, score)),
        rubric_score=rubric_score,
        proof_score=proof_points,
        packaging_score=packaging_points,
        passed=score >= 80 and proof_points >= 65 and not gaps,
        gaps=gaps,
        details={"networking_rubric": rubric.to_dict(), "proof": proof or {}},
    )


def compare_projects(
    *,
    baseline_path: str | Path,
    candidate_path: str | Path,
    brief: str,
    baseline_proof: Optional[Dict[str, Any]] = None,
    candidate_proof: Optional[Dict[str, Any]] = None,
    min_delta: int = 5,
) -> ComparisonResult:
    baseline = score_project(baseline_path, brief=brief, proof=baseline_proof)
    candidate = score_project(candidate_path, brief=brief, proof=candidate_proof)
    delta = candidate.score - baseline.score
    improved = candidate.passed and delta >= min_delta
    if improved:
        reason = f"candidate wins by {delta} points and passes domain gates"
    elif not candidate.passed:
        reason = "candidate did not pass benchmark gates"
    else:
        reason = f"candidate delta {delta} below required {min_delta}"
    return ComparisonResult(
        baseline=baseline,
        candidate=candidate,
        improved=improved,
        delta=delta,
        reason=reason,
    )


def export_benchmark_json(results: Iterable[BenchmarkResult]) -> str:
    return json.dumps([result.to_dict() for result in results], indent=2)
