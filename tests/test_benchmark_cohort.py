from __future__ import annotations

import pytest

from skyn3t.studio.benchmark_cohort import (
    benchmark_start_payloads,
    get_benchmark_case,
    list_benchmark_cases,
)
from skyn3t.studio.benchmark_results import collect_benchmark_results


def test_benchmark_cohort_contains_required_quality_cases():
    cases = list_benchmark_cases()
    ids = {case["id"] for case in cases}

    assert {
        "react-vite-dashboard",
        "fullstack-service-dashboard",
        "fastapi-health-service",
        "networking-domain-tool",
    }.issubset(ids)
    assert all(case["template"] == "auto" for case in cases)
    assert all(case["expected_gates"] for case in cases)


def test_benchmark_start_payloads_are_local_quality_runs():
    payloads = benchmark_start_payloads()

    assert len(payloads) == len(list_benchmark_cases())
    for payload in payloads:
        assert payload["template"] == "auto"
        assert "brief" in payload and payload["brief"]
        assert payload["extra"]["quality_floor_score"] == 85
        assert payload["extra"]["benchmark_case"]


def test_get_benchmark_case_returns_case_by_id():
    case = get_benchmark_case("fastapi-health-service")

    assert case is not None
    assert case.stack == "fastapi"


@pytest.mark.asyncio
async def test_studio_benchmark_cohort_api():
    import skyn3t.web.app as web_app

    result = await web_app.studio_benchmark_cohort()

    assert "cases" in result
    assert any(case["id"] == "networking-domain-tool" for case in result["cases"])


@pytest.mark.asyncio
async def test_studio_benchmark_results_api(monkeypatch, tmp_path):
    import skyn3t.web.app as web_app

    class FakeRunner:
        projects_root = tmp_path / "projects"

    project = FakeRunner.projects_root / "bench-1"
    project.mkdir(parents=True)
    (project / "project.json").write_text(
        '{"slug":"bench-1","benchmark_case":"fastapi-health-service",'
        '"real_project_scorecard":{"passed":false,"score":70,"penalties":{"build_integrity":30}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: FakeRunner())

    result = await web_app.studio_benchmark_results()

    assert result["summary"][0]["case_id"] == "fastapi-health-service"
    assert result["summary"][0]["passes"] == 0


def test_collect_benchmark_results_groups_project_scorecards(tmp_path):
    project = tmp_path / "projects" / "bench-1"
    project.mkdir(parents=True)
    (project / "project.json").write_text(
        """
        {
          "slug": "bench-1",
          "benchmark_case": "react-vite-dashboard",
          "benchmark_stack": "react_vite",
          "status": "done",
          "quality_summary": {"score": 91},
          "build_verification": {"verdict": "yes"},
          "real_project_scorecard": {"passed": true, "score": 96, "penalties": {}},
          "updated_at": 10
        }
        """,
        encoding="utf-8",
    )

    result = collect_benchmark_results(tmp_path / "projects")

    assert result["runs"][0]["case_id"] == "react-vite-dashboard"
    assert result["summary"][0]["runs"] == 1
    assert result["summary"][0]["passes"] == 1
    assert result["summary"][0]["avg_score"] == 96
