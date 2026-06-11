from __future__ import annotations

import json
from pathlib import Path

import pytest

from skyn3t.intelligence.golden_project_ingestor import (
    ingest_golden_project,
    networking_github_queries,
    snapshot_local_project,
)
from skyn3t.intelligence.skill_library import SkillLibrary


class _FakeRAG:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def add_knowledge_one(self, **kwargs):
        self.calls.append(kwargs)
        return f"golden-{len(self.calls)}"


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_snapshot_local_project_redacts_and_extracts_commands(tmp_path: Path) -> None:
    _write(
        tmp_path / "package.json",
        json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}),
    )
    _write(
        tmp_path / "README.md",
        "Aruba AOS-CX troubleshooting tool\nARUBA_TOKEN=secret\nswitch=10.0.0.5\n",
    )
    _write(tmp_path / "node_modules" / "ignored.js", "token=bad\n")

    snap = snapshot_local_project(tmp_path)

    assert "README.md" in snap.file_shape
    assert "node_modules/ignored.js" not in snap.file_shape
    assert "npm run test" in snap.commands
    assert "npm run build" in snap.commands
    assert "secret" not in snap.snippets["README.md"]
    assert "10.0.0.5" not in snap.snippets["README.md"]


@pytest.mark.asyncio
async def test_ingest_golden_project_writes_rag_and_skill(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'aruba-field-tool'\n")
    _write(
        tmp_path / "README.md",
        "Aruba field troubleshooting CLI with dry-run inventory validation.",
    )
    rag = _FakeRAG()
    lib = SkillLibrary(root=tmp_path / "skills")

    result = await ingest_golden_project(
        source_uri=str(tmp_path),
        title="Aruba field tool",
        vendor_tags=["aruba"],
        domain_tags=["field_troubleshooting", "inventory_config"],
        stack="python",
        reusable_patterns=["Always expose --dry-run before pushing config."],
        quality_notes="Great CLI workflow for AOS-CX inventory validation.",
        rag_engine=rag,
        skill_library=lib,
    )

    assert result.knowledge_id == "golden-1"
    assert rag.calls[0]["doc_type"] == "golden_project"
    assert rag.calls[0]["metadata"]["read_only_original"] is True
    assert "Aruba field tool" in rag.calls[0]["title"]
    skills = lib.all()
    assert len(skills) == 1
    skill = skills[0]
    assert "golden-corpus" in skill.tags
    assert "aruba" in skill.tags
    assert "field_troubleshooting" in skill.tags
    assert skill.memory_doc_id == "golden-1"


@pytest.mark.asyncio
async def test_ingest_github_project_is_metadata_only_read_only(tmp_path: Path) -> None:
    rag = _FakeRAG()
    lib = SkillLibrary(root=tmp_path / "skills")

    result = await ingest_golden_project(
        source_uri="https://github.com/example/juniper-toolkit",
        title="Juniper toolkit",
        vendor_tags=["juniper"],
        domain_tags=["automation_scripts"],
        github_metadata={
            "description": "Junos automation scripts for field diagnostics",
            "language": "Python",
            "topics": ["junos", "netmiko"],
        },
        rag_engine=rag,
        skill_library=lib,
    )

    assert result.record.source.source_type == "github"
    assert result.record.source.read_only_original is True
    assert result.record.source.clone_strategy == "local_clone_or_fork"
    assert "Junos automation" in result.content
    assert "juniper" in lib.all()[0].tags


def test_networking_github_queries_cover_all_vendor_domains() -> None:
    queries = networking_github_queries()

    assert any("aos-cx aruba" in query for query in queries)
    assert any("junos juniper" in query for query in queries)
    assert any("troubleshooting" in query for query in queries)
    assert any("inventory" in query for query in queries)
    assert any("automation" in query for query in queries)
