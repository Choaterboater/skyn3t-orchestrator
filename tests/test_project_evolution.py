from __future__ import annotations

import json
from pathlib import Path

from skyn3t.intelligence.project_evolution import (
    compare_candidate_to_baseline,
    prepare_candidate_workspace,
    propose_verified_improvement,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class _FakeProposalStore:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return type("P", (), {"to_public": lambda _self: {"id": "p1", **kwargs}})()


def test_prepare_candidate_workspace_copies_local_without_mutating_original(tmp_path: Path) -> None:
    original = tmp_path / "original"
    _write(original / "README.md", "Aruba tool")
    _write(original / ".git" / "config", "do not copy")
    _write(original / "node_modules" / "x.js", "do not copy")

    workspace = prepare_candidate_workspace(
        str(original),
        candidate_root=tmp_path / "candidates",
        label="try1",
    )
    _write(workspace.candidate_dir / "README.md", "changed candidate")

    assert workspace.read_only_original is True
    assert workspace.git_push_allowed is False
    assert (workspace.candidate_dir / "README.md").read_text() == "changed candidate"
    assert (original / "README.md").read_text() == "Aruba tool"
    assert not (workspace.candidate_dir / ".git").exists()
    assert not (workspace.candidate_dir / "node_modules").exists()


def test_prepare_candidate_workspace_for_github_is_metadata_only(tmp_path: Path) -> None:
    workspace = prepare_candidate_workspace(
        "https://github.com/example/juniper-toolkit",
        candidate_root=tmp_path / "candidates",
        label="candidate",
    )

    assert workspace.source_type == "github"
    assert workspace.git_push_allowed is False
    metadata = json.loads((workspace.candidate_dir / "GITHUB_SOURCE.json").read_text())
    assert metadata["github_repo"] == "example/juniper-toolkit"
    assert metadata["clone_required"] is True


def test_propose_verified_improvement_only_for_winner(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write(baseline / "README.md", "Aruba static app")
    _write(
        candidate / "README.md",
        "Aruba AOS-CX field troubleshooting CLI with API token docs, .env.example, "
        "offline sample data, dry-run preview, config validation backup diff, "
        "inventory workflow, and interface status diagnostics.",
    )
    _write(candidate / ".env.example", "ARUBA_API_TOKEN=\n")
    _write(candidate / "package.json", '{"scripts":{"test":"vitest"}}\n')
    _write(candidate / "src" / "main.js", "fetch('/rest/v10.08/system'); // dry-run diagnostic")
    comparison = compare_candidate_to_baseline(
        baseline_path=baseline,
        candidate_path=candidate,
        brief="Build an Aruba field troubleshooting tool",
        baseline_proof={"ok": True, "verdict": "yes"},
        candidate_proof={"ok": True, "verdict": "yes"},
    )
    store = _FakeProposalStore()

    proposal = propose_verified_improvement(
        comparison=comparison,
        source_uri=str(baseline),
        candidate_dir=candidate,
        proposal_store=store,
    )

    assert proposal is not None
    assert store.created[0]["requires_approval"] is True
    assert store.created[0]["payload"]["read_only_original"] is True
    assert store.created[0]["payload"]["git_push_allowed"] is False


def test_no_proposal_for_non_winner(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write(baseline / "README.md", "Juniper dry-run validation diagnostics")
    _write(candidate / "README.md", "Juniper dry-run validation diagnostics")
    comparison = compare_candidate_to_baseline(
        baseline_path=baseline,
        candidate_path=candidate,
        brief="Build a Juniper diagnostics tool",
        baseline_proof={"ok": True, "verdict": "yes"},
        candidate_proof={"ok": False, "verdict": "no"},
    )

    assert propose_verified_improvement(
        comparison=comparison,
        source_uri=str(baseline),
        candidate_dir=candidate,
        proposal_store=_FakeProposalStore(),
    ) is None
