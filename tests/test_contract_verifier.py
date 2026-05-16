"""Wiring test for ContractVerifierAgent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from skyn3t.agents import ContractVerifierAgent
from skyn3t.core.agent import TaskRequest


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_contract_verifier_returns_needs_fix_on_palette_schism(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(artifact / "palette.json", json.dumps({"primary": "#E05C1A", "bg": "#0F0D0A"}))
    _write(scaffold / "src" / "styles.css", ":root { --bg: #09111f; --accent: #60a5fa; }\n")

    agent = ContractVerifierAgent()
    task = TaskRequest(
        title="contract-verify",
        input_data={
            "brief": "Build a dashboard with a strong brand palette.",
            "artifact_dir": str(artifact),
        },
    )

    result = asyncio.run(agent.execute(task))

    assert result.success is True
    assert result.output["verdict"] == "needs_fix"
    assert result.output["blocker_count"] >= 1
    report = json.loads(result.output["report_json"])
    assert report["ok"] is False
    assert any(f["category"] == "palette_schism_css" for f in report["findings"])


def test_contract_verifier_returns_pass_on_clean_scaffold(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(artifact / "palette.json", json.dumps({"primary": "#E05C1A", "bg": "#0F0D0A"}))
    _write(
        artifact / "tech_stack.json",
        json.dumps({"frontend": "react-vite", "backend": "express"}),
    )
    _write(
        scaffold / "package.json",
        json.dumps({"dependencies": {"react": "^18", "vite": "^5"}}),
    )
    _write(
        scaffold / "server" / "package.json",
        json.dumps({"dependencies": {"express": "^4"}}),
    )
    _write(scaffold / "src" / "styles.css", ":root { --bg: #0F0D0A; --accent: #E05C1A; }\n")

    agent = ContractVerifierAgent()
    task = TaskRequest(
        title="contract-verify",
        input_data={
            "brief": "Ship a small tool.",
            "artifact_dir": str(artifact),
        },
    )

    result = asyncio.run(agent.execute(task))

    assert result.success is True
    assert result.output["verdict"] == "pass"
    assert result.output["blocker_count"] == 0


def test_contract_verifier_requires_artifact_dir() -> None:
    agent = ContractVerifierAgent()
    task = TaskRequest(title="contract-verify", input_data={"brief": "x"})
    result = asyncio.run(agent.execute(task))
    assert result.success is False
    assert "artifact_dir" in (result.error or "")
