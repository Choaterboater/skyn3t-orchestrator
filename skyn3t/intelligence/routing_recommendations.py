from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List, Optional

_CACHE_TTL_SECONDS = 15.0
_CACHE_LOCK = threading.Lock()
_CACHE_AT = 0.0
_CACHE_ROWS: List[Dict[str, Any]] = []

_CODELIKE_STAGES = {"code", "code_agent", "code_improver"}


def list_stage_recommendations() -> List[Dict[str, Any]]:
    global _CACHE_AT, _CACHE_ROWS
    now = time.time()
    with _CACHE_LOCK:
        if now - _CACHE_AT <= _CACHE_TTL_SECONDS and _CACHE_ROWS:
            return [dict(row) for row in _CACHE_ROWS]
        rows = _compute_stage_recommendations()
        _CACHE_AT = now
        _CACHE_ROWS = [dict(row) for row in rows]
        return [dict(row) for row in _CACHE_ROWS]


def _compute_stage_recommendations() -> List[Dict[str, Any]]:
    from skyn3t.core.model_router import (
        default_tier_for_stage,
        list_stage_routes,
        tier_details,
    )
    from skyn3t.intelligence.stage_latency import snapshot as stage_latency_snapshot

    routes = list_stage_routes()
    live_stage_tokens = _live_stage_tokens()
    latency = stage_latency_snapshot()
    trajectory = _trajectory_summary()
    heavy = _heavy_stages(routes, live_stage_tokens, latency, trajectory)

    rows: List[Dict[str, Any]] = []
    for route in routes:
        stage = str(route.get("stage") or "").strip().lower()
        current_tier = str(route.get("tier") or "")
        current_backend = str(route.get("backend") or "")
        default_tier = default_tier_for_stage(stage) or current_tier
        default_backend, default_model = tier_details(default_tier)
        judgment_sensitive = "strong" in default_tier
        code_like = stage in _CODELIKE_STAGES

        stage_live_tokens = int(live_stage_tokens.get(stage, 0))
        stage_latency = latency.get(stage, {})
        avg_latency_seconds = float(stage_latency.get("avg_seconds") or 0.0)
        stage_traj = trajectory.get(stage, {})
        route_stats = stage_traj.get("route_stats", {})
        stage_samples = int(stage_traj.get("trajectory_samples") or 0)

        recommended_tier = current_tier
        recommendation_kind = "keep"
        reasons: List[str] = []
        confidence_samples = stage_samples

        current_stat = route_stats.get(current_tier)
        best_stat = _best_route_stat(route_stats, judgment_sensitive=judgment_sensitive)

        if code_like:
            reasons.append("Code already uses per-file routing, so stage-level advice stays conservative.")
        else:
            evidence_choice = _evidence_backed_recommendation(
                current_tier=current_tier,
                current_backend=current_backend,
                current_stat=current_stat,
                best_stat=best_stat,
                judgment_sensitive=judgment_sensitive,
            )
            if evidence_choice is not None:
                recommended_tier = evidence_choice["tier"]
                confidence_samples = int(evidence_choice.get("samples") or stage_samples)
                if judgment_sensitive:
                    recommendation_kind = "quality"
                    reasons.append(
                        f"Recent {stage} runs succeed more often on {recommended_tier}."
                    )
                else:
                    recommendation_kind = "efficiency"
                    reasons.append(
                        f"Recent {stage} runs are more cost-efficient on {recommended_tier}."
                    )

        if recommended_tier == current_tier and stage in heavy and not judgment_sensitive and not code_like:
            cheaper_override = _cheaper_default_recommendation(
                current_tier=current_tier,
                current_backend=current_backend,
                default_tier=default_tier,
                default_backend=default_backend,
                current_source=str(route.get("source") or ""),
            )
            if cheaper_override is not None:
                recommended_tier = cheaper_override
                recommendation_kind = "cheaper"
                reasons.append("This stage is one of the heaviest by token burn or latency, so bias it back toward the cheaper default.")

        if recommended_tier == current_tier and not reasons:
            reasons.append("Current route already matches the best known default for this stage.")

        if stage in heavy:
            reasons.append("Heavy-stage signal detected from recent token usage or latency.")
        if route.get("source") == "persisted" and recommended_tier != current_tier:
            reasons.append("Current route is a saved override; applying the recommendation will replace that persisted policy.")

        rec_backend, rec_model = tier_details(recommended_tier)
        rows.append(
            {
                "stage": stage,
                "current_tier": current_tier,
                "current_backend": route.get("backend"),
                "current_model": route.get("model"),
                "current_source": route.get("source"),
                "recommended_tier": recommended_tier,
                "recommended_backend": rec_backend,
                "recommended_model": rec_model,
                "default_tier": default_tier,
                "recommendation_kind": recommendation_kind,
                "confidence": _confidence_label(confidence_samples),
                "reasons": reasons[:3],
                "signals": {
                    "live_stage_tokens": stage_live_tokens,
                    "trajectory_stage_tokens": int(stage_traj.get("total_tokens") or 0),
                    "avg_latency_seconds": avg_latency_seconds,
                    "trajectory_samples": stage_samples,
                    "mixed_route_samples": int(stage_traj.get("mixed_route_samples") or 0),
                    "current_success_rate": _round_rate(current_stat.get("success_rate")) if isinstance(current_stat, dict) else None,
                    "recommended_success_rate": _round_rate(route_stats.get(recommended_tier, {}).get("success_rate")) if isinstance(route_stats.get(recommended_tier), dict) else None,
                },
                "applyable": bool(recommended_tier and recommended_tier != current_tier),
            }
        )
    return rows


def best_model_for(
    *,
    stage: str,
    stack: Optional[str],
    features: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Rank (stack, stage, feature) routing cells for predictive auto-route.

    Joins per-stage ``route_stats`` (from ``routing_observations.snapshot()``)
    — narrowed to the matching (stack, feature) cells when present — with
    ``ModelTournamentStore.rankings()`` filtered by vendor (backend) + domain
    (stage/stack/feature) tags written by the debate tournament recorder.

    Returns a dict ``{backend, model, tier, score, source, rationale,
    samples}`` for the best/cheapest winning route, or ``None`` when there
    is no observation/tournament evidence (the caller then degrades to the
    static route). Never forces an expensive backend: ties and near-ties
    break toward the lower ``relative_backend_cost``.
    """
    stage_key = str(stage or "").strip().lower()
    if not stage_key:
        return None
    stack_key = str(stack or "").strip().lower() or None
    feature_keys = [
        str(f or "").strip().lower() for f in (features or []) if str(f or "").strip()
    ]

    try:
        from skyn3t.intelligence.routing_observations import snapshot
    except Exception:
        return None
    try:
        observations = snapshot()
    except Exception:
        return None

    stage_row = observations.get(stage_key) if isinstance(observations, dict) else None
    route_stats = stage_row.get("route_stats", {}) if isinstance(stage_row, dict) else {}
    if not isinstance(route_stats, dict) or not route_stats:
        return None

    from skyn3t.core.model_router import relative_backend_cost

    tournament = _tournament_rankings(
        stage=stage_key, stack=stack_key, features=feature_keys
    )

    scored: List[Dict[str, Any]] = []
    for tier_name, stat in route_stats.items():
        if not isinstance(stat, dict):
            continue
        backend = str(stat.get("backend") or "")
        if not backend:
            continue
        model = stat.get("model")
        rate, samples = _cell_rate_and_samples(
            stat, stack=stack_key, features=feature_keys
        )
        if rate is None or samples < _AUTO_ROUTE_MIN_SAMPLES:
            continue
        cost = relative_backend_cost(backend)
        # Efficiency = observed win-rate per relative cost; tournament
        # quality (when the cheap model has won debates) nudges the score
        # up without ever forcing a pricier backend.
        efficiency = _efficiency_score(rate, cost)
        tournament_boost = _tournament_boost(tournament, model)
        score = efficiency * (1.0 + tournament_boost)
        scored.append(
            {
                "tier": tier_name,
                "backend": backend,
                "model": model,
                "rate": rate,
                "samples": samples,
                "cost": cost,
                "score": round(score, 6),
                "tournament_boost": round(tournament_boost, 4),
            }
        )

    if not scored:
        return None

    # Best score wins; cheaper backend breaks ties so we never drift to a
    # pricier option on equal evidence.
    scored.sort(
        key=lambda item: (item["score"], -item["cost"], item["samples"]),
        reverse=True,
    )
    best = scored[0]
    rationale = (
        f"auto-route: {best['backend']} won (stack={stack_key or 'any'}, "
        f"stage={stage_key}, win_rate={best['rate']:.2f}, "
        f"samples={best['samples']}, cost={best['cost']:.2f})"
    )
    if best["tournament_boost"] > 0:
        rationale += f", tournament_boost={best['tournament_boost']:.2f}"
    return {
        "backend": best["backend"],
        "model": best["model"],
        "tier": best["tier"],
        "score": best["score"],
        "samples": best["samples"],
        "success_rate": round(float(best["rate"]), 3),
        "cost": best["cost"],
        "source": "predictive",
        "rationale": rationale,
    }


# Minimum graded samples in a cell before auto-route trusts it.
_AUTO_ROUTE_MIN_SAMPLES = 3


def _cell_rate_and_samples(
    stat: Dict[str, Any],
    *,
    stack: Optional[str],
    features: List[str],
) -> tuple[Optional[float], int]:
    """Pick the most specific (stack, feature) cell for a route stat.

    Falls back to the stage-level aggregate when no matching cell exists,
    so the auto-router still works on bare-stage observations (which is
    the common case before the stack/feature dimension fills in).
    """
    cells = stat.get("cells")
    if isinstance(cells, dict) and (stack or features):
        candidates: List[str] = []
        if stack and features:
            candidates.extend(f"{stack}::{f}" for f in features)
        if stack:
            candidates.append(f"{stack}::")
        if features:
            candidates.extend(f"::{f}" for f in features)
        for key in candidates:
            cell = cells.get(key)
            if isinstance(cell, dict) and int(cell.get("samples") or 0) > 0:
                return (
                    float(cell.get("success_rate") or 0.0),
                    int(cell.get("samples") or 0),
                )
    samples = int(stat.get("samples") or 0)
    if samples <= 0:
        return None, 0
    return float(stat.get("success_rate") or 0.0), samples


def _tournament_rankings(
    *,
    stage: str,
    stack: Optional[str],
    features: List[str],
) -> Dict[str, Any]:
    """Return {model_id: ModelRanking-like dict} from the tournament store.

    Domain tags mirror the debate recorder convention (stage + stack +
    features); vendor tags are left open so any backend that has won cheap
    debates can boost. Graceful empty dict on any error / no data.
    """
    domain_tags = [t for t in [stage, stack, *features] if t]
    try:
        from skyn3t.intelligence.model_tournament import get_default_tournament_store

        rankings = get_default_tournament_store().rankings(
            domain_tags=domain_tags, min_trials=1
        )
    except Exception:
        return {}
    out: Dict[str, Any] = {}
    for ranking in rankings:
        try:
            out[str(ranking.model_id).strip().lower()] = ranking
        except Exception:
            continue
    return out


def _tournament_boost(tournament: Dict[str, Any], model: Optional[str]) -> float:
    """Map a model's tournament pass_rate into a small bounded boost [0, 0.5].

    Only rewards cheap winners — it scales the observed efficiency score,
    it never substitutes a pricier model. Returns 0 when the model has no
    tournament record.
    """
    if not model:
        return 0.0
    ranking = tournament.get(str(model).strip().lower())
    if ranking is None:
        return 0.0
    try:
        pass_rate = float(getattr(ranking, "pass_rate", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(0.5, pass_rate * 0.5))


def _evidence_backed_recommendation(
    *,
    current_tier: str,
    current_backend: str,
    current_stat: Optional[Dict[str, Any]],
    best_stat: Optional[Dict[str, Any]],
    judgment_sensitive: bool,
) -> Optional[Dict[str, Any]]:
    if not best_stat or str(best_stat.get("tier") or "") == current_tier:
        return None
    best_samples = int(best_stat.get("samples") or 0)
    if best_samples < 5:
        return None

    best_success = float(best_stat.get("success_rate") or 0.0)
    if judgment_sensitive:
        current_success = float(current_stat.get("success_rate") or 0.0) if current_stat else 0.0
        if current_stat is None and best_success >= 0.8:
            return best_stat
        if best_success >= current_success + 0.15:
            return best_stat
        return None

    from skyn3t.core.model_router import relative_backend_cost

    best_efficiency = _efficiency_score(
        best_success,
        relative_backend_cost(str(best_stat.get("backend") or "")),
    )
    current_efficiency = _efficiency_score(
        float(current_stat.get("success_rate") or 0.0) if current_stat else 0.0,
        relative_backend_cost(current_backend),
    ) if current_stat else None
    if current_efficiency is None and best_success >= 0.75:
        return best_stat
    if current_efficiency is not None and best_efficiency >= current_efficiency * 1.15:
        return best_stat
    return None


def _best_route_stat(
    route_stats: Dict[str, Dict[str, Any]],
    *,
    judgment_sensitive: bool,
) -> Optional[Dict[str, Any]]:
    from skyn3t.core.model_router import relative_backend_cost

    candidates = [stat for stat in route_stats.values() if int(stat.get("samples") or 0) >= 3]
    if not candidates:
        return None
    if judgment_sensitive:
        return max(
            candidates,
            key=lambda stat: (
                float(stat.get("success_rate") or 0.0),
                -relative_backend_cost(str(stat.get("backend") or "")),
                int(stat.get("samples") or 0),
            ),
        )
    return max(
        candidates,
        key=lambda stat: (
            _efficiency_score(
                float(stat.get("success_rate") or 0.0),
                relative_backend_cost(str(stat.get("backend") or "")),
            ),
            float(stat.get("success_rate") or 0.0),
            int(stat.get("samples") or 0),
        ),
    )


def _cheaper_default_recommendation(
    *,
    current_tier: str,
    current_backend: str,
    default_tier: str,
    default_backend: Optional[str],
    current_source: str,
) -> Optional[str]:
    from skyn3t.core.model_router import relative_backend_cost

    if not default_tier or default_tier == current_tier:
        return None
    if current_source == "default":
        return None
    if "cheap" in default_tier and "cheap" not in current_tier:
        return default_tier
    if relative_backend_cost(str(default_backend or "")) >= relative_backend_cost(current_backend):
        return None
    return default_tier


def _efficiency_score(success_rate: float, backend_cost: float) -> float:
    if success_rate <= 0 or backend_cost <= 0:
        return 0.0
    return success_rate / backend_cost


def _round_rate(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _confidence_label(samples: int) -> str:
    if samples >= 20:
        return "high"
    if samples >= 5:
        return "medium"
    return "low"


def _live_stage_tokens() -> Dict[str, int]:
    from skyn3t.observability.token_tracker import get_default_tracker

    totals: DefaultDict[str, int] = defaultdict(int)
    for project in get_default_tracker().per_project():
        for stage in list(project.get("stages") or []):
            stage_name = _normalize_stage(stage.get("stage"))
            if not stage_name:
                continue
            try:
                totals[stage_name] += int(stage.get("total_tokens") or 0)
            except (TypeError, ValueError):
                continue
    return dict(totals)


def _trajectory_summary() -> Dict[str, Dict[str, Any]]:
    from skyn3t.intelligence.routing_observations import snapshot

    return snapshot()


def _normalize_stage(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text


def _heavy_stages(
    routes: List[Dict[str, Any]],
    live_tokens: Dict[str, int],
    latency: Dict[str, Dict[str, Any]],
    trajectory: Dict[str, Dict[str, Any]],
) -> set[str]:
    stages = [str(route.get("stage") or "").strip().lower() for route in routes if route.get("stage")]
    token_rank = sorted(
        (
            (stage, int(live_tokens.get(stage, 0)) + int(trajectory.get(stage, {}).get("total_tokens") or 0))
            for stage in stages
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    latency_rank = sorted(
        (
            (stage, float(latency.get(stage, {}).get("avg_seconds") or 0.0))
            for stage in stages
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    heavy: set[str] = set()
    for ranked in (token_rank, latency_rank):
        non_zero = [item for item in ranked if item[1] > 0]
        if not non_zero:
            continue
        cutoff = max(1, len(non_zero) // 3)
        heavy.update(stage for stage, _ in non_zero[:cutoff])
    return heavy
