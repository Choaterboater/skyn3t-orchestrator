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
