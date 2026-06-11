from __future__ import annotations

from pathlib import Path

from skyn3t.intelligence.domain_benchmark import compare_projects, score_project


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_score_project_rewards_complete_networking_tool(tmp_path: Path) -> None:
    _write(
        tmp_path / "README.md",
        "Juniper Junos inventory diagnostics with API token setup, SNMP, .env.example, "
        "offline sample data, dry-run preview, config validation, backup diff, and "
        "field troubleshooting health checks.",
    )
    _write(tmp_path / ".env.example", "JUNOS_API_TOKEN=\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname='juniper-tool'\n")
    _write(
        tmp_path / "src" / "main.py",
        "import httpx\n# dry-run junos rpc /rest/ interface status inventory diagnostic\n",
    )

    result = score_project(
        tmp_path,
        brief="Build a Juniper inventory and field troubleshooting CLI",
        proof={"ok": True, "verdict": "yes"},
    )

    assert result.score >= 80
    assert result.passed is True
    assert result.gaps == []


def test_compare_projects_requires_verified_candidate_win(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write(baseline / "README.md", "Aruba troubleshooting app with static cards.")
    _write(
        candidate / "README.md",
        "Aruba AOS-CX field troubleshooting CLI with API token docs, .env.example, "
        "offline sample data, dry-run preview, config validation backup diff, "
        "inventory device workflow, and interface status diagnostics.",
    )
    _write(candidate / ".env.example", "ARUBA_API_TOKEN=\n")
    _write(candidate / "package.json", '{"scripts":{"test":"vitest"}}\n')
    _write(
        candidate / "src" / "main.js",
        "fetch('/rest/v10.08/system'); // dry-run inventory diagnostic config validation\n",
    )

    comparison = compare_projects(
        baseline_path=baseline,
        candidate_path=candidate,
        brief="Build an Aruba field troubleshooting and inventory tool",
        baseline_proof={"ok": True, "verdict": "yes"},
        candidate_proof={"ok": True, "verdict": "yes"},
        min_delta=5,
    )

    assert comparison.improved is True
    assert comparison.delta >= 5
    assert "candidate wins" in comparison.reason


def test_compare_projects_blocks_unverified_candidate(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write(baseline / "README.md", "Juniper diagnostics with dry-run and validation.")
    _write(candidate / "README.md", "Juniper diagnostics with dry-run and validation.")

    comparison = compare_projects(
        baseline_path=baseline,
        candidate_path=candidate,
        brief="Build a Juniper diagnostics tool",
        baseline_proof={"ok": True, "verdict": "yes"},
        candidate_proof={"ok": False, "verdict": "no"},
    )

    assert comparison.improved is False
    assert "did not pass" in comparison.reason
