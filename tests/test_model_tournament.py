from __future__ import annotations

import json
import time

from skyn3t.core import openrouter_catalog as catalog
from skyn3t.intelligence.model_tournament import (
    ModelTournamentStore,
    ModelTrial,
    candidate_models_from_catalog,
)


def test_tournament_ranks_quality_per_dollar(tmp_path) -> None:
    store = ModelTournamentStore(path=tmp_path / "tournament.json")
    store.record_trial(
        ModelTrial(
            model_id="expensive/good",
            task_id="aruba-cli",
            vendor_tags=["aruba"],
            domain_tags=["field_troubleshooting"],
            score=92,
            cost_usd=0.20,
            passed=True,
        )
    )
    store.record_trial(
        ModelTrial(
            model_id="cheap/good-enough",
            task_id="aruba-cli",
            vendor_tags=["aruba"],
            domain_tags=["field_troubleshooting"],
            score=84,
            cost_usd=0.02,
            passed=True,
        )
    )

    rankings = store.rankings(vendor_tags=["aruba"], domain_tags=["field_troubleshooting"])

    assert rankings[0].model_id == "cheap/good-enough"
    assert rankings[0].pass_rate == 1.0
    assert rankings[0].quality_per_dollar > rankings[1].quality_per_dollar


def test_tournament_filters_by_domain_tags(tmp_path) -> None:
    store = ModelTournamentStore(path=tmp_path / "tournament.json")
    store.record_trial(
        ModelTrial(
            model_id="juniper/model",
            task_id="junos",
            vendor_tags=["juniper"],
            domain_tags=["automation_scripts"],
            score=88,
            cost_usd=0.05,
            passed=True,
        )
    )
    store.record_trial(
        ModelTrial(
            model_id="aruba/model",
            task_id="aoscx",
            vendor_tags=["aruba"],
            domain_tags=["inventory_config"],
            score=90,
            cost_usd=0.04,
            passed=True,
        )
    )

    rankings = store.rankings(vendor_tags=["juniper"], domain_tags=["automation_scripts"])

    assert [row.model_id for row in rankings] == ["juniper/model"]


def test_candidate_models_from_catalog_prefers_relevant_models(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()
    payload = {
        "synced_at": time.time(),
        "ttl_seconds": catalog.DEFAULT_TTL_SECONDS,
        "models": [
            {
                "id": "openrouter/free-mini",
                "name": "Free Mini",
                "description": "general cheap model",
                "context_length": 8192,
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "vendor/network-coder",
                "name": "Network Coder",
                "description": "code agent for network automation tools",
                "context_length": 64000,
                "pricing": {"prompt": "0.0001", "completion": "0.0002"},
            },
        ],
    }
    (tmp_path / catalog.CACHE_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    catalog._catalog_index = None
    catalog._catalog_loaded_at = 0.0

    candidates = candidate_models_from_catalog(limit=2)

    assert candidates
    assert candidates[0]["model_id"] == "vendor/network-coder"
    get_settings.cache_clear()
