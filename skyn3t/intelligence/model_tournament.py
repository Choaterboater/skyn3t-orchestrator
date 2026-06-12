"""Cheap-model tournament records for domain builds."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from skyn3t.intelligence.domain_corpus import NETWORKING_DOMAINS, NETWORKING_VENDORS

TOURNAMENT_FILENAME = "model_tournament.json"


@dataclass
class ModelTrial:
    model_id: str
    task_id: str
    domain_tags: List[str]
    vendor_tags: List[str]
    score: int
    cost_usd: float
    passed: bool
    latency_seconds: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def quality_per_dollar(self) -> float:
        if self.cost_usd <= 0:
            return float(self.score) * 1000.0
        return float(self.score) / self.cost_usd

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["quality_per_dollar"] = self.quality_per_dollar
        return data


@dataclass
class ModelRanking:
    model_id: str
    trials: int
    pass_rate: float
    avg_score: float
    avg_cost_usd: float
    quality_per_dollar: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _data_dir() -> Path:
    try:
        from skyn3t.config.settings import get_settings

        return Path(get_settings().data_dir)
    except Exception:
        return Path("data")


def _normalize_tags(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        tag = str(value or "").strip().lower()
        if tag and tag not in out:
            out.append(tag)
    return out


class ModelTournamentStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (_data_dir() / TOURNAMENT_FILENAME)

    def load_trials(self) -> List[ModelTrial]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        rows = raw.get("trials") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return []
        trials: List[ModelTrial] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                trials.append(
                    ModelTrial(
                        model_id=str(row.get("model_id") or ""),
                        task_id=str(row.get("task_id") or ""),
                        domain_tags=_normalize_tags(row.get("domain_tags") or []),
                        vendor_tags=_normalize_tags(row.get("vendor_tags") or []),
                        score=max(0, min(100, int(row.get("score") or 0))),
                        cost_usd=max(0.0, float(row.get("cost_usd") or 0.0)),
                        passed=bool(row.get("passed")),
                        latency_seconds=max(0.0, float(row.get("latency_seconds") or 0.0)),
                        created_at=float(row.get("created_at") or time.time()),
                    )
                )
            except Exception:
                continue
        return [trial for trial in trials if trial.model_id]

    def save_trials(self, trials: List[ModelTrial]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"trials": [trial.to_dict() for trial in trials]}, indent=2),
            encoding="utf-8",
        )

    def record_trial(self, trial: ModelTrial) -> None:
        trials = self.load_trials()
        trials.append(trial)
        self.save_trials(trials[-500:])

    def rankings(
        self,
        *,
        vendor_tags: Iterable[str] = (),
        domain_tags: Iterable[str] = (),
        min_trials: int = 1,
    ) -> List[ModelRanking]:
        wanted_vendors = set(_normalize_tags(vendor_tags))
        wanted_domains = set(_normalize_tags(domain_tags))
        buckets: Dict[str, List[ModelTrial]] = {}
        for trial in self.load_trials():
            if wanted_vendors and not wanted_vendors.intersection(trial.vendor_tags):
                continue
            if wanted_domains and not wanted_domains.intersection(trial.domain_tags):
                continue
            buckets.setdefault(trial.model_id, []).append(trial)

        rankings: List[ModelRanking] = []
        for model_id, trials in buckets.items():
            if len(trials) < min_trials:
                continue
            avg_score = sum(t.score for t in trials) / len(trials)
            avg_cost = sum(t.cost_usd for t in trials) / len(trials)
            pass_rate = sum(1 for t in trials if t.passed) / len(trials)
            qpd = avg_score / avg_cost if avg_cost > 0 else avg_score * 1000.0
            rankings.append(
                ModelRanking(
                    model_id=model_id,
                    trials=len(trials),
                    pass_rate=round(pass_rate, 3),
                    avg_score=round(avg_score, 2),
                    avg_cost_usd=round(avg_cost, 6),
                    quality_per_dollar=round(qpd, 3),
                )
            )
        rankings.sort(
            key=lambda item: (
                item.pass_rate,
                item.quality_per_dollar,
                item.avg_score,
            ),
            reverse=True,
        )
        return rankings


def estimate_model_cost_usd(model_meta: Dict[str, Any], *, tokens: int = 12_000) -> float:
    raw_pricing = model_meta.get("pricing")
    pricing: Dict[str, Any] = raw_pricing if isinstance(raw_pricing, dict) else {}
    raw_prompt = pricing.get("prompt") or pricing.get("input") or 0
    raw_completion = pricing.get("completion") or pricing.get("output") or 0
    try:
        prompt_cost = float(raw_prompt)
    except (TypeError, ValueError):
        prompt_cost = 0.0
    try:
        completion_cost = float(raw_completion)
    except (TypeError, ValueError):
        completion_cost = 0.0
    return max(0.0, (prompt_cost + completion_cost) * (tokens / 1_000_000.0))


def candidate_models_from_catalog(
    *,
    limit: int = 8,
    vendor_tags: Iterable[str] = NETWORKING_VENDORS,
    domain_tags: Iterable[str] = NETWORKING_DOMAINS,
) -> List[Dict[str, Any]]:
    """Return cheap/current OpenRouter candidates for networking tournaments."""

    try:
        from skyn3t.core.openrouter_catalog import load_catalog

        snap = load_catalog()
    except Exception:
        return []
    keywords = {
        "code",
        "coder",
        "free",
        "flash",
        "mini",
        "tool",
        "agent",
        "reasoning",
        *[str(v).lower() for v in vendor_tags],
        *[str(d).replace("_", " ").lower() for d in domain_tags],
    }
    candidates: List[Dict[str, Any]] = []
    for model in snap.models:
        mid = str(model.get("id") or "")
        haystack = f"{mid} {model.get('name', '')} {model.get('description', '')}".lower()
        relevance = sum(1 for kw in keywords if kw and kw in haystack)
        if relevance <= 0:
            continue
        cost = estimate_model_cost_usd(model)
        candidates.append(
            {
                "model_id": mid,
                "relevance": relevance,
                "estimated_cost_usd": cost,
                "context_length": model.get("context_length"),
            }
        )
    candidates.sort(
        key=lambda row: (
            row["relevance"],
            -float(row["estimated_cost_usd"]),
            int(row.get("context_length") or 0),
        ),
        reverse=True,
    )
    return candidates[: max(1, int(limit))]


def get_default_tournament_store() -> ModelTournamentStore:
    return ModelTournamentStore()
