from __future__ import annotations

import json
from pathlib import Path

from skyn3t.intelligence import routing_observations as obs


def test_record_trajectory_updates_stage_route_stats(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "skyn3t.intelligence.routing_observations.get_settings",
        lambda: type("S", (), {"data_dir": tmp_path})(),
    )
    obs.reset_cache_for_tests()

    obs.record_trajectory(
        {
            "trajectory_id": "traj-1",
            "stage": "reviewer",
            "outcome": "success",
            "events": [
                {
                    "type": "llm_call",
                    "project_stage": "reviewer",
                    "backend": "openrouter",
                    "model": "xiaomi/mimo-v2.5-pro",
                    "total_tokens": 2000,
                }
            ],
        }
    )

    snap = obs.snapshot()
    assert snap["reviewer"]["trajectory_samples"] == 1
    assert snap["reviewer"]["route_stats"]["or_strong"]["samples"] == 1
    assert snap["reviewer"]["route_stats"]["or_strong"]["success_rate"] == 1.0


def test_snapshot_warms_from_existing_trajectory_files(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "skyn3t.intelligence.routing_observations.get_settings",
        lambda: type("S", (), {"data_dir": tmp_path})(),
    )
    obs.reset_cache_for_tests()
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir(parents=True, exist_ok=True)
    (trajectory_dir / "2026-05-22.jsonl").write_text(
        json.dumps(
            {
                "trajectory_id": "traj-2",
                "stage": "brainstorm",
                "outcome": "failure",
                "events": [
                    {
                        "type": "llm_call",
                        "project_stage": "brainstorm",
                        "backend": "openrouter",
                        "model": "openrouter/owl-alpha",
                        "total_tokens": 900,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snap = obs.snapshot(trajectory_dir=trajectory_dir)

    assert snap["brainstorm"]["trajectory_samples"] == 1
    assert snap["brainstorm"]["route_stats"]["or_cheap"]["failures"] == 1
    assert Path(tmp_path / "routing_observations.json").exists()
