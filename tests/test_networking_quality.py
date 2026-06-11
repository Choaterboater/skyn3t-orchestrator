from __future__ import annotations

import pytest

from skyn3t.core.agent import TaskRequest
from skyn3t.intelligence.networking_quality import evaluate_networking_quality


def test_networking_quality_flags_missing_operator_workflows() -> None:
    report = evaluate_networking_quality(
        brief="Build an Aruba field troubleshooting CLI",
        contents={"README.md": "Aruba troubleshooting tool placeholder."},
    )

    assert report.applicable is True
    assert report.score < 50
    assert any("dry-run" in gap for gap in report.gaps)
    assert any("vendor/API" in gap for gap in report.gaps)


def test_networking_quality_accepts_complete_network_tool() -> None:
    report = evaluate_networking_quality(
        brief="Build a Juniper inventory and diagnostics CLI",
        contents={
            "README.md": (
                "README credentials setup with API token, SNMP community, .env.example, "
                "offline sample data, dry-run mode, config validation, backup diff, "
                "device inventory, switch site grouping, health check diagnostics."
            ),
            "src/main.py": (
                "import httpx\n"
                "def run():\n"
                "  # junos rpc diagnostic via dry-run preview\n"
                "  return httpx.get('/rest/junos')\n"
            ),
        },
    )

    assert report.applicable is True
    assert report.score >= 85
    assert report.gaps == []


@pytest.mark.asyncio
async def test_reviewer_surfaces_networking_rubric_gaps(monkeypatch, tmp_path) -> None:
    from skyn3t.agents.reviewer import ReviewerAgent

    artifact = tmp_path / "project"
    artifact.mkdir()
    (artifact / "README.md").write_text(
        "Aruba troubleshooting dashboard with pretty cards only.",
        encoding="utf-8",
    )

    async def fake_llm_review(*args, **kwargs):
        return None, None, None

    agent = ReviewerAgent()
    await agent.initialize()
    monkeypatch.setattr(agent, "_llm_review", fake_llm_review)

    result = await agent.execute(
        TaskRequest(
            title="review networking",
            input_data={
                "brief": "Build an Aruba field troubleshooting tool",
                "artifact_dir": str(artifact),
                "packaging_enabled": False,
            },
        )
    )

    assert result.success is True
    assert result.output["score"] < 80
    review_md = (artifact / "review.md").read_text(encoding="utf-8")
    assert "Networking domain quality" in review_md
    assert "missing dry-run" in review_md
