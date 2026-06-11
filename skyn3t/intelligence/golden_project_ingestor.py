"""Golden project ingestion for domain-specific learning.

This module turns approved local/GitHub networking projects into redacted
knowledge documents and skills. It never mutates source projects. GitHub
sources are treated as read-only metadata unless a later candidate workspace
explicitly clones/forks them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from skyn3t.intelligence.domain_corpus import (
    NETWORKING_DOMAINS,
    NETWORKING_VENDORS,
    GoldenProjectRecord,
    build_github_search_queries,
    make_golden_project_record,
    redact_sensitive_text,
)
from skyn3t.intelligence.skill_library import Skill, SkillLibrary, get_default_library

logger = logging.getLogger("skyn3t.intelligence.golden_project_ingestor")

_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
_TEXT_EXTS = {
    ".md",
    ".txt",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".sh",
    ".env",
    ".example",
}
_PRIORITY_FILES = {
    "README.md",
    "readme.md",
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
}


@dataclass
class LocalProjectSnapshot:
    """Redacted local project sample used for corpus ingestion."""

    root: Path
    file_shape: List[str]
    commands: List[str]
    snippets: Dict[str, str] = field(default_factory=dict)
    redaction: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GoldenIngestResult:
    """Result of ingesting one golden project."""

    record: GoldenProjectRecord
    knowledge_id: Optional[str] = None
    skill_names: List[str] = field(default_factory=list)
    content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "knowledge_id": self.knowledge_id,
            "skill_names": list(self.skill_names),
            "content": self.content,
        }


def _safe_rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _is_text_candidate(path: Path) -> bool:
    if path.name in _PRIORITY_FILES:
        return True
    if path.suffix.lower() in _TEXT_EXTS:
        return True
    if path.name.endswith(".env.example"):
        return True
    return False


def _iter_project_files(root: Path, *, max_files: int = 80) -> List[Path]:
    files: List[Path] = []
    if not root.is_dir():
        return files
    for path in sorted(root.rglob("*")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        files.append(path)
        if len(files) >= max_files:
            break
    return files


def _extract_commands(root: Path) -> List[str]:
    commands: List[str] = []
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts") if isinstance(data, dict) else {}
            if isinstance(scripts, dict):
                for name in ("test", "lint", "build", "dev", "start"):
                    if name in scripts:
                        commands.append(f"npm run {name}")
        except Exception:
            logger.debug("package.json command extraction failed", exc_info=True)
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        commands.extend(["pytest -q", "ruff check ."])
    requirements = root / "requirements.txt"
    if requirements.is_file() and "pytest -q" not in commands:
        commands.append("pytest -q")
    return list(dict.fromkeys(commands))


def snapshot_local_project(
    root: str | Path,
    *,
    max_files: int = 80,
    max_snippets: int = 12,
    max_chars_per_file: int = 2400,
) -> LocalProjectSnapshot:
    project_root = Path(root).expanduser().resolve()
    files = _iter_project_files(project_root, max_files=max_files)
    file_shape = [_safe_rel(path, project_root) for path in files]
    snippets: Dict[str, str] = {}
    finding_counts: Dict[str, int] = {}

    priority = sorted(
        [path for path in files if _is_text_candidate(path)],
        key=lambda path: (
            0 if path.name in _PRIORITY_FILES else 1,
            _safe_rel(path, project_root),
        ),
    )
    for path in priority[:max_snippets]:
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")[:max_chars_per_file]
        except Exception:
            continue
        redacted = redact_sensitive_text(raw)
        snippets[_safe_rel(path, project_root)] = redacted.text
        for finding in redacted.findings:
            finding_counts[finding.kind] = finding_counts.get(finding.kind, 0) + finding.count

    return LocalProjectSnapshot(
        root=project_root,
        file_shape=file_shape,
        commands=_extract_commands(project_root),
        snippets=snippets,
        redaction={
            "redacted": bool(finding_counts),
            "findings": [
                {"kind": kind, "count": count}
                for kind, count in sorted(finding_counts.items())
            ],
        },
    )


def _format_record_content(record: GoldenProjectRecord, snippets: Dict[str, str]) -> str:
    lines = [
        f"Golden Networking Project: {record.title}",
        f"Source Type: {record.source.source_type}",
        f"Source URI: {record.source.uri}",
        f"Read-only original: {record.source.read_only_original}",
        f"Clone Strategy: {record.source.clone_strategy}",
        f"Vendors: {', '.join(record.vendor_tags) or 'unknown'}",
        f"Domains: {', '.join(record.domain_tags) or 'unknown'}",
        f"Stack: {record.stack or 'unknown'}",
    ]
    if record.commands:
        lines.append("Commands:")
        lines.extend(f"- {command}" for command in record.commands[:10])
    if record.file_shape:
        lines.append("File Shape:")
        lines.extend(f"- {path}" for path in record.file_shape[:40])
    if record.quality_notes:
        lines.extend(["Quality Notes:", record.quality_notes[:1600]])
    if record.reusable_patterns:
        lines.append("Reusable Patterns:")
        lines.extend(f"- {pattern[:300]}" for pattern in record.reusable_patterns[:12])
    if snippets:
        lines.append("Redacted Snippets:")
        for rel, body in snippets.items():
            lines.append(f"## {rel}")
            lines.append(body[:1600])
    return "\n".join(lines)


def _skill_for_record(
    record: GoldenProjectRecord,
    *,
    knowledge_id: Optional[str],
) -> Skill:
    tags = [
        "golden-corpus",
        "networking",
        *record.vendor_tags,
        *record.domain_tags,
    ]
    if record.stack:
        tags.append(record.stack)
    body = "\n".join(
        [
            f"# Golden corpus pattern: {record.title}",
            "",
            "Use this as an approved networking-project reference. Do not copy secrets, "
            "customer data, device addresses, or repository code verbatim; apply the "
            "architecture and quality patterns.",
            "",
            f"- Source type: `{record.source.source_type}`",
            f"- Original read-only: `{record.source.read_only_original}`",
            f"- Clone strategy: `{record.source.clone_strategy}`",
            f"- Vendors: {', '.join(record.vendor_tags) or 'unknown'}",
            f"- Domains: {', '.join(record.domain_tags) or 'unknown'}",
            f"- Stack: {record.stack or 'unknown'}",
            "",
            "## Commands",
            "\n".join(f"- `{command}`" for command in record.commands[:8]) or "- Unknown",
            "",
            "## Reusable patterns",
            "\n".join(f"- {pattern}" for pattern in record.reusable_patterns[:10]) or "- See corpus memory.",
            "",
            "## Safety",
            "Original local projects and GitHub repositories stay read-only. Generate local "
            "candidate improvements and propose them only after verification beats baseline.",
        ]
    )
    return Skill(
        name=f"golden-networking-{record.title}",
        description="Approved Aruba/Juniper/networking project pattern.",
        tags=tags,
        triggers=[
            "aruba",
            "juniper",
            "network automation",
            "field troubleshooting",
            "inventory validation",
        ],
        success_count=1,
        failure_count=0,
        source="golden_project_ingestor",
        memory_doc_id=knowledge_id or "",
        body=body,
    )


async def ingest_golden_project(
    *,
    source_uri: str,
    title: str = "",
    source_ref: str = "",
    vendor_tags: Iterable[str] = (),
    domain_tags: Iterable[str] = (),
    stack: str = "",
    commands: Iterable[str] = (),
    reusable_patterns: Iterable[str] = (),
    quality_notes: str = "",
    rag_engine: Optional[Any] = None,
    skill_library: Optional[SkillLibrary] = None,
    github_metadata: Optional[Dict[str, Any]] = None,
) -> GoldenIngestResult:
    """Ingest an approved golden project without mutating the original source."""

    snippets: Dict[str, str] = {}
    file_shape: List[str] = []
    merged_commands = list(commands)
    merged_patterns = list(reusable_patterns)
    merged_notes = quality_notes

    root = Path(source_uri).expanduser()
    if root.exists() and root.is_dir():
        snapshot = snapshot_local_project(root)
        snippets = snapshot.snippets
        file_shape = snapshot.file_shape
        merged_commands.extend(snapshot.commands)
        if snapshot.redaction.get("redacted"):
            merged_notes = (
                f"{merged_notes}\n\nRedaction applied: {snapshot.redaction}"
            ).strip()

    if github_metadata:
        description = str(github_metadata.get("description") or "").strip()
        language = str(github_metadata.get("language") or "").strip()
        topics = github_metadata.get("topics") or []
        if description:
            merged_notes = f"{merged_notes}\n\nGitHub description: {description}".strip()
        if language and not stack:
            stack = language
        if isinstance(topics, list):
            merged_patterns.extend(str(topic) for topic in topics if str(topic).strip())

    record = make_golden_project_record(
        title=title or Path(source_uri).name or str(source_uri),
        source_uri=source_uri,
        source_ref=source_ref,
        vendor_tags=vendor_tags,
        domain_tags=domain_tags,
        stack=stack,
        commands=merged_commands,
        file_shape=file_shape,
        reusable_patterns=merged_patterns,
        quality_notes=merged_notes,
    )
    content = _format_record_content(record, snippets)
    knowledge_id: Optional[str] = None
    if rag_engine is not None:
        knowledge_id = await rag_engine.add_knowledge_one(
            content=content,
            title=f"Golden corpus: {record.title}",
            source=record.source.uri,
            doc_type="golden_project",
            metadata={
                "corpus_id": record.corpus_id,
                "source_type": record.source.source_type,
                "vendor_tags": ", ".join(record.vendor_tags),
                "domain_tags": ", ".join(record.domain_tags),
                "stack": record.stack,
                "read_only_original": record.source.read_only_original,
            },
        )

    library = skill_library or get_default_library()
    skill = _skill_for_record(record, knowledge_id=knowledge_id)
    library.upsert(skill)
    return GoldenIngestResult(
        record=record,
        knowledge_id=knowledge_id,
        skill_names=[skill.name],
        content=content,
    )


def networking_github_queries() -> List[str]:
    return build_github_search_queries(
        vendors=NETWORKING_VENDORS,
        domains=NETWORKING_DOMAINS,
        extra_terms=("python", "tool"),
    )
