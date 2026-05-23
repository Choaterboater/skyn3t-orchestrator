"""Model routing policy — pick the right tier for each task.

The pipeline already supports per-agent backend + model overrides in
``data/agent_overrides.json``. What was missing was a deliberate
"draft cheap, review strong" policy. Today every stage runs at the
same tier whether it's brainstorm (which a haiku-class model does
fine) or reviewer (which benefits from a stronger model).

This module exposes a single function:

  resolve_model(stage_name, brief) -> (backend, model)

with a static policy table that callers can override via
``SKYN3T_MODEL_ROUTING`` env var pointing at a JSON file.

Stages today (in order of "cheap is fine" → "strong matters"):
  brainstorm   — small idea fan-out. Cheap is fine.
  research     — per-service spec extraction. Cheap is fine; we
                 already have research fan-out so each call is small.
  architect    — system-design pass. Strong matters (one mistake
                 here cascades into every downstream file).
  designer     — visual brand. Cheap is fine; we strip this stage
                 entirely when the brief locks the aesthetic.
  code         — main generation. Strong matters; this is where
                 quality compounds across N files.
  reviewer     — judges output. Strong matters more here than
                 anywhere else; cheap models miss real issues.
  build_verifier / boot_verifier / integration_verifier — local subprocess,
               no LLM call.

Tier name → CLI backend mapping:
  cheap   → kimi_cli  (free, fast, "good enough" for fan-out)
  strong  → claude_cli (Opus — high reasoning quality, free on
            subscription, slower)
  balanced → copilot_cli (GPT-class via Copilot subscription —
            middle ground)

Stage-level routing is intentionally a SHALLOW policy. Per-call
routing (e.g. "this specific file is critical, use strong") stays
inside the agent. We're not building a full bandit here.
"""

from __future__ import annotations

import json
import logging
import os
import random
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("skyn3t.core.model_router")


# Default tier table. Each entry is (backend, model). The model field
# can be None to let the backend pick its default (claude → opus,
# kimi → k2, etc.).
#
# The 'ui' tier (codex) is a specialist for React/UI generation.
# Empirical: v15 used codex on the code stage and produced the
# best-looking dashboard we've shipped (100/100). v22-v32 with
# claude opus on code produced correct but visually flat output.
# Codex is a UI specialist; opus is a reasoning specialist. Use
# opus for reviewer (judging) and codex for code (generating).
_TIERS: Dict[str, Tuple[str, Optional[str]]] = {
    # OpenRouter-first tiers (HTTP-based; no 240s CLI idle-timeout cliff).
    # Capability-routed per file type — see _resolve_static below.
    "or_cheap":   ("openrouter", "openrouter/owl-alpha"),
    "or_ui":      ("openrouter", "xiaomi/mimo-v2-flash"),
    "or_backend": ("openrouter", "qwen/qwen3-coder"),
    "or_strong":  ("openrouter", "xiaomi/mimo-v2.5-pro"),
    "or_docs":    ("openrouter", "openai/gpt-oss-120b:free"),
    # Local-CLI tiers — kept as safety net via _BACKEND_ALTERNATIVES.
    "cheap":    ("kimi_cli",    None),
    "balanced": ("copilot_cli", None),
    "strong":   ("claude_cli",  "opus"),
    "ui":       ("copilot_cli", "gpt-5.3-codex"),
}


# Default stage → tier policy. Keys are LOWERCASED stage names matching
# what the runner publishes in PROJECT_STAGE_STARTED / project.json.
_DEFAULT_STAGE_POLICY: Dict[str, str] = {
    # framing & fan-out — OpenRouter cheap/fast by default so the
    # common path stays HTTP-based instead of bouncing through CLIs.
    "brainstorm":         "or_cheap",
    "research":           "or_cheap",
    "designer":           "or_cheap",
    "writer":             "or_cheap",
    "marketer":           "or_cheap",
    "business_analyst":   "or_cheap",

    # system-shape decisions — still prefer a stronger model, but on
    # OpenRouter rather than a CLI-backed Claude default.
    "architect":          "or_strong",
    "architecture":       "or_strong",
    # Code-stage planning still uses the agent-level route before
    # per-file specialization kicks in. Keep it on the HTTP path, but
    # use a code-focused model instead of the ultra-cheap general tier.
    "code":               "or_backend",
    "code_agent":         "or_backend",
    "code_improver":      "or_backend",
    "reviewer":           "or_strong",
    "contract_verifier":  "or_strong",
    "consistency_reviewer": "or_strong",
    "packaging_agent":    "or_cheap",
    "verifier":           "or_cheap",
    "docs":               "or_docs",

    # verifiers don't call LLMs — listed for completeness
    "build_verifier":     "or_cheap",
    "boot_verifier":      "or_cheap",
    "integration_verifier": "or_cheap",
}


@lru_cache(maxsize=4)
def _load_override_file(path: str) -> Dict[str, str]:
    """Load env-pointed JSON override, if present.

    Format: a dict mapping stage_name → tier name, e.g.
    ``{"reviewer": "balanced", "code": "balanced"}`` when the user wants
    to cap spend.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            logger.warning(
                "SKYN3T_MODEL_ROUTING at %s is not a JSON object — ignoring",
                path,
            )
            return {}
        out: Dict[str, str] = {}
        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            if v not in _TIERS:
                logger.warning(
                    "SKYN3T_MODEL_ROUTING: stage %s wants tier %s which "
                    "doesn't exist (valid: %s) — ignoring",
                    k, v, list(_TIERS),
                )
                continue
            out[k.lower()] = v
        return out
    except FileNotFoundError:
        logger.warning("SKYN3T_MODEL_ROUTING=%s — file not found", path)
        return {}
    except Exception:
        logger.warning(
            "SKYN3T_MODEL_ROUTING at %s could not be parsed",
            path, exc_info=True,
        )
        return {}


def _load_env_overrides() -> Dict[str, str]:
    path = os.environ.get("SKYN3T_MODEL_ROUTING")
    if not path:
        return {}
    return _load_override_file(path)


def _load_persisted_overrides() -> Dict[str, Dict[str, Optional[str]]]:
    try:
        from skyn3t.config.model_routing import get_model_routing_store

        data = get_model_routing_store().entries()
    except Exception:
        logger.debug("persisted model routing lookup failed", exc_info=True)
        return {}
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        tier = str(v.get("tier") or "").strip()
        if tier not in _TIERS:
            logger.warning(
                "Persisted model routing: stage %s wants tier %s which "
                "doesn't exist (valid: %s) — ignoring",
                k, tier, list(_TIERS),
            )
            continue
        applied_via = str(v.get("applied_via") or "").strip().lower() or None
        out[str(k).lower()] = {
            "tier": tier,
            "applied_via": (
                applied_via if applied_via in {"manual", "recommendation"} else None
            ),
        }
    return out


def available_tiers() -> List[Dict[str, Optional[str]]]:
    return [
        {"name": name, "backend": backend, "model": model}
        for name, (backend, model) in _TIERS.items()
    ]


def tier_details(tier_name: str) -> Tuple[Optional[str], Optional[str]]:
    backend, model = _TIERS.get(tier_name, (None, None))
    return backend, model


def default_tier_for_stage(stage_name: Optional[str]) -> Optional[str]:
    if not stage_name:
        return None
    return _DEFAULT_STAGE_POLICY.get(str(stage_name).strip().lower())


def tier_for_backend_model(backend: str, model: Optional[str]) -> Optional[str]:
    backend_name = str(backend or "").strip()
    model_name = str(model or "").strip() or None
    for tier_name, (tier_backend, tier_model) in _TIERS.items():
        if tier_backend != backend_name:
            continue
        if (tier_model or None) == model_name:
            return tier_name
    return None


def _route_for_stage(stage_name: Optional[str]) -> Dict[str, Optional[str]]:
    if not stage_name:
        backend, model = _TIERS["cheap"]
        return {
            "stage": None,
            "tier": "cheap",
            "backend": backend,
            "model": model,
            "source": "fallback",
            "persisted_via": None,
        }
    stage = stage_name.lower()
    persisted = _load_persisted_overrides()
    if stage in persisted:
        tier = str(persisted[stage].get("tier") or "")
        backend, model = _TIERS.get(tier, _TIERS["cheap"])
        return {
            "stage": stage,
            "tier": tier,
            "backend": backend,
            "model": model,
            "source": "persisted",
            "persisted_via": persisted[stage].get("applied_via"),
        }
    env = _load_env_overrides()
    if stage in env:
        tier = env[stage]
        backend, model = _TIERS.get(tier, _TIERS["cheap"])
        return {
            "stage": stage,
            "tier": tier,
            "backend": backend,
            "model": model,
            "source": "env",
            "persisted_via": None,
        }
    if stage in _DEFAULT_STAGE_POLICY:
        tier = _DEFAULT_STAGE_POLICY[stage]
        backend, model = _TIERS.get(tier, _TIERS["cheap"])
        return {
            "stage": stage,
            "tier": tier,
            "backend": backend,
            "model": model,
            "source": "default",
            "persisted_via": None,
        }
    backend, model = _TIERS["cheap"]
    return {
        "stage": stage,
        "tier": "cheap",
        "backend": backend,
        "model": model,
        "source": "fallback",
        "persisted_via": None,
    }


def describe_stage_route(stage_name: Optional[str]) -> Dict[str, Optional[str]]:
    return dict(_route_for_stage(stage_name))


def list_stage_routes() -> List[Dict[str, Optional[str]]]:
    known = {
        *(_DEFAULT_STAGE_POLICY.keys()),
        *(_load_env_overrides().keys()),
        *(_load_persisted_overrides().keys()),
    }
    return [describe_stage_route(stage) for stage in sorted(known)]


def tier_for_stage(stage_name: Optional[str]) -> str:
    """Return the tier label ('cheap' / 'balanced' / 'strong') for a
    stage. Defaults to ``cheap`` when the stage isn't recognized — we'd
    rather under-spend than over-spend on an unknown stage."""
    return str(_route_for_stage(stage_name).get("tier") or "cheap")


def has_stage_policy(stage_name: Optional[str]) -> bool:
    """Whether a stage has an explicit routing policy entry.

    Used by operator UIs to distinguish between a real routed default
    (e.g. reviewer → OpenRouter strong) and the router's generic cheap
    fallback for unknown names.
    """
    if not stage_name:
        return False
    source = _route_for_stage(stage_name).get("source")
    return source in {"persisted", "env", "default"}


def resolve_model(
    stage_name: Optional[str],
    *,
    brief: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Pick (backend, model) for a stage.

    Honors:
      1. env override (``SKYN3T_MODEL_ROUTING`` JSON file)
      2. default stage → tier policy
      3. tier → (backend, model) mapping

    ``brief`` is plumbed through for future brief-aware overrides
    (e.g. "if brief mentions security, force reviewer→strong even
    when the override file caps it lower"). Not used today.
    """
    route = _route_for_stage(stage_name)
    return (
        str(route.get("backend") or _TIERS["cheap"][0]),
        route.get("model"),
    )


# ── Per-file routing for CodeAgent ──────────────────────────────────
#
# Empirical observations from v15-v32 testing:
#   * Kimi (kimi_cli) writes prettier React UI with cleaner themes.
#     Less good at backend correctness (CJS/ESM, route wiring).
#   * Codex (copilot_cli / gpt-5.3-codex) writes solid backend code
#     — Express adapters, route handlers, env-var plumbing. Less
#     visually polished on React.
#   * Claude opus is a strong reasoner but generates visually flat
#     React. Good for reviewer; not the right tool for UI files.
#
# So at code stage, route PER FILE by type instead of per-stage.

_FRONTEND_EXTS: Tuple[str, ...] = (
    ".jsx", ".tsx", ".vue", ".svelte", ".astro",
)
_FRONTEND_PATH_HINTS: Tuple[str, ...] = (
    "src/components/", "src/pages/", "src/hooks/", "src/styles/",
    "src/app/", "src/lib/ui", "src/theme",
    "components/", "pages/",  # next.js-style top-level
)
_BACKEND_PATH_HINTS: Tuple[str, ...] = (
    "server/", "api/", "backend/", "routes/", "adapters/",
    "handlers/", "controllers/", "middleware/",
)


# ── Adaptive routing ────────────────────────────────────────────────
#
# The static decisions above encode empirical priors. They DON'T learn.
# When ``stack`` and a scoreboard are supplied, the router asks the
# scoreboard whether the statically-picked backend is winning for THIS
# stack; if it's been losing for ``_MIN_SAMPLES`` graded attempts at a
# rate below ``_DEMOTE_BELOW``, demote to the next-best alternative.
#
# Three env vars tune the behavior:
#   SKYN3T_ROUTER_ADAPTIVE=0       hard-disable; always return static
#   SKYN3T_ROUTER_DEMOTE_BELOW=X   demote when win rate < X (default 0.4)
#   SKYN3T_ROUTER_DEMOTE_AFTER=N   require N graded attempts (default 5)
#   SKYN3T_ROUTER_EXPLORATION_EPS=Y  with prob Y still try the original
#                                  (default 0.1, ε-greedy so a demoted
#                                  backend can recover)

# Per-backend "next best" map. Empirical complement of the static
# tiers: visual specialist demotes to code specialist (and vice versa);
# strong reasoner demotes back to code specialist.
_BACKEND_ALTERNATIVES: Dict[str, Tuple[str, Optional[str]]] = {
    # OpenRouter demotes to local CLIs only when the scoreboard shows
    # OR losing on a given stack — keeps the safety net intact.
    "openrouter":  ("copilot_cli", None),
    "kimi_cli":    ("copilot_cli", None),
    "copilot_cli": ("claude_cli",  "opus"),
    "claude_cli":  ("copilot_cli", None),
}

# Static cost tiers per backend. Values are relative (1.0 = cheap
# baseline). Used by adaptive routing to optimize "works AND is
# cheap" — when two backends have a similar win rate on a stack,
# prefer the cheaper one. Empirical ballpark from observed token
# spend over the project history; tune via ``SKYN3T_ROUTER_BACKEND_COSTS``
# env var (JSON dict ``{"backend": cost}``) if these drift.
_BACKEND_COST: Dict[str, float] = {
    "openrouter":  0.5,     # default model is openrouter/owl-alpha (free)
    "kimi_cli":    1.0,     # subscription-backed CLI, effectively free per call
    "copilot_cli": 1.0,     # GitHub Copilot CLI, included in subscription
    "claude_cli":  3.0,     # Anthropic API per-token, strong model
    "openai_cli":  2.5,     # OpenAI API per-token
}

# Default cost for an unknown backend — pessimistic so the router
# doesn't accidentally prefer something we haven't priced.
_UNKNOWN_BACKEND_COST = 2.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("router: %s=%r not a float; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("router: %s=%r not an int; using default %s", name, raw, default)
        return default


def _adaptive_enabled() -> bool:
    raw = os.environ.get("SKYN3T_ROUTER_ADAPTIVE", "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    return True


def _cost_weight_enabled() -> bool:
    """Cost-weighted routing is opt-in (default ON). Disable when
    operators want pure-rate decisions for debugging or research."""
    raw = os.environ.get("SKYN3T_ROUTER_COST_WEIGHTED", "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    return True


def _backend_cost(backend: str) -> float:
    """Return the relative cost of one call to ``backend``.

    Reads the static ``_BACKEND_COST`` table first, then any env-var
    override (``SKYN3T_ROUTER_BACKEND_COSTS`` as a JSON dict).
    Unknown backends fall through to ``_UNKNOWN_BACKEND_COST`` —
    pessimistic so a new backend isn't accidentally preferred.
    """
    override_raw = os.environ.get("SKYN3T_ROUTER_BACKEND_COSTS", "").strip()
    if override_raw:
        try:
            overrides = json.loads(override_raw)
            if isinstance(overrides, dict) and backend in overrides:
                return float(overrides[backend])
        except (ValueError, TypeError):
            logger.warning(
                "router: SKYN3T_ROUTER_BACKEND_COSTS=%r is not valid JSON",
                override_raw,
            )
    return float(_BACKEND_COST.get(backend, _UNKNOWN_BACKEND_COST))


def relative_backend_cost(backend: str) -> float:
    return _backend_cost(backend)


def _expected_cost_per_success(
    backend: str, rate: Optional[float],
) -> Optional[float]:
    """``cost / win_rate`` — the average cost to get one working file
    out of this backend on the current stack.

    Returns None when ``rate`` is None (no data yet) or 0 (would
    divide by zero — treat as "infinite cost"). The caller uses
    None to mean "no information; don't factor cost into the
    decision."
    """
    if rate is None or rate <= 0.0:
        return None
    return _backend_cost(backend) / rate


def _maybe_demote(
    backend: str,
    model: Optional[str],
    *,
    rel_path: str,
    stack: str,
    scoreboard: Any,
    event_bus: Any = None,
) -> Tuple[str, Optional[str]]:
    """Demote ``(backend, model)`` if the scoreboard says it's losing.

    Pure function over scoreboard state — same inputs, same decision
    (except for the ε-greedy coin flip). Logs every demotion so the
    decision is auditable.
    """
    rate = scoreboard.backend_rate(
        stack, backend, min_samples=_env_int("SKYN3T_ROUTER_DEMOTE_AFTER", 5),
    )
    if rate is None:
        return backend, model
    threshold = _env_float("SKYN3T_ROUTER_DEMOTE_BELOW", 0.4)
    if rate >= threshold:
        return backend, model
    # ε-greedy: occasionally let the demoted backend try anyway so
    # it has a chance to recover from a streak of bad luck.
    epsilon = _env_float("SKYN3T_ROUTER_EXPLORATION_EPS", 0.1)
    if epsilon > 0 and random.random() < epsilon:
        logger.info(
            "router: keeping %s for %s despite low win rate %.2f (ε-greedy explore)",
            backend, rel_path, rate,
        )
        return backend, model
    alt = _BACKEND_ALTERNATIVES.get(backend)
    if not alt:
        return backend, model
    alt_backend, alt_model = alt
    # Don't demote to a backend that is ALSO losing on this stack — if
    # there's no good option, stick with the original so the system
    # still produces output (even if poorly).
    alt_rate = scoreboard.backend_rate(
        stack, alt_backend, min_samples=_env_int("SKYN3T_ROUTER_DEMOTE_AFTER", 5),
    )
    if alt_rate is not None and alt_rate < threshold:
        logger.info(
            "router: would demote %s→%s for %s but alt rate %.2f also below %.2f; keeping",
            backend, alt_backend, rel_path, alt_rate, threshold,
        )
        return backend, model
    logger.info(
        "router: demoting %s→%s for %s (win rate %.2f < %.2f on stack=%s)",
        backend, alt_backend, rel_path, rate, threshold, stack,
    )
    _publish_router_decision(
        event_bus,
        action="demote_backend",
        reason=(
            f"win rate {rate:.2f} < threshold {threshold:.2f} "
            f"on stack={stack}"
        ),
        input={
            "rel_path": rel_path,
            "stack": stack,
            "from_backend": backend,
            "from_model": model,
            "to_backend": alt_backend,
            "to_model": alt_model,
            "rate": rate,
            "threshold": threshold,
        },
    )
    return alt_backend, alt_model


def _maybe_cost_demote(
    backend: str,
    model: Optional[str],
    *,
    rel_path: str,
    stack: str,
    scoreboard: Any,
    event_bus: Any = None,
) -> Tuple[str, Optional[str]]:
    """Prefer a cheaper backend when both are working fine.

    Distinct from ``_maybe_demote`` (which fires on a *losing*
    backend): this fires when both the static pick and its
    alternative are above the win-rate threshold but the
    alternative's cost-per-success is meaningfully lower.

    "Meaningfully" is governed by ``SKYN3T_ROUTER_COST_SAVINGS``
    (default 0.25 — require 25% relative savings) so the router
    doesn't flap on small differences. Honors the kill switch
    ``SKYN3T_ROUTER_COST_WEIGHTED=0``.
    """
    if not _cost_weight_enabled():
        return backend, model
    min_samples = _env_int("SKYN3T_ROUTER_DEMOTE_AFTER", 5)
    threshold = _env_float("SKYN3T_ROUTER_DEMOTE_BELOW", 0.4)
    rate = scoreboard.backend_rate(stack, backend, min_samples=min_samples)
    # Below threshold (or no data) → defer to _maybe_demote's logic.
    if rate is None or rate < threshold:
        return backend, model
    alt = _BACKEND_ALTERNATIVES.get(backend)
    if not alt:
        return backend, model
    alt_backend, alt_model = alt
    alt_rate = scoreboard.backend_rate(stack, alt_backend, min_samples=min_samples)
    if alt_rate is None or alt_rate < threshold:
        return backend, model
    cur_cps = _expected_cost_per_success(backend, rate)
    alt_cps = _expected_cost_per_success(alt_backend, alt_rate)
    if cur_cps is None or alt_cps is None or cur_cps <= 0:
        return backend, model
    relative_savings = (cur_cps - alt_cps) / cur_cps
    savings_threshold = _env_float("SKYN3T_ROUTER_COST_SAVINGS", 0.25)
    if relative_savings < savings_threshold:
        return backend, model
    logger.info(
        "router: cost-demoting %s→%s for %s "
        "(cost/success %.2f→%.2f, savings %.0f%% on stack=%s)",
        backend, alt_backend, rel_path,
        cur_cps, alt_cps, relative_savings * 100, stack,
    )
    _publish_router_decision(
        event_bus,
        action="cost_demote_backend",
        reason=(
            f"cost/success {cur_cps:.2f}→{alt_cps:.2f} "
            f"(savings {relative_savings:.0%}) on stack={stack}"
        ),
        input={
            "rel_path": rel_path,
            "stack": stack,
            "from_backend": backend,
            "from_model": model,
            "to_backend": alt_backend,
            "to_model": alt_model,
            "from_rate": rate,
            "to_rate": alt_rate,
            "from_cost_per_success": cur_cps,
            "to_cost_per_success": alt_cps,
            "relative_savings": relative_savings,
        },
    )
    return alt_backend, alt_model


def _publish_router_decision(event_bus, **kwargs) -> None:
    """Emit a CORTEX_DECISION event for an adaptive routing decision.

    Tolerant of a missing event_bus (no-op) — the router is a pure
    function in many callsites and shouldn't require the orchestrator
    to be present just to make a routing decision.
    """
    if event_bus is None:
        return
    try:
        from skyn3t.intelligence.cortex_decisions import publish_decision
        publish_decision(
            event_bus,
            system="router",
            source="model_router",
            **kwargs,
        )
    except Exception:
        logger.debug("router decision publish failed", exc_info=True)


def resolve_model_for_file(
    rel_path: str,
    stage_name: Optional[str] = "code",
    *,
    stack: Optional[str] = None,
    scoreboard: Any = None,
    event_bus: Any = None,
) -> Tuple[str, Optional[str]]:
    """Pick (backend, model) for a SPECIFIC file inside the code stage.

    Frontend (.jsx, .css, components/, pages/, hooks/) → kimi_cli
    (pretty UI specialist).

    Backend (server/, api/, routes/, adapters/) → copilot_cli
    /gpt-5.3-codex (code-correctness specialist).

    Everything else → stage-level resolution (config files, top-level
    files, unrecognized paths).

    When ``stack`` and ``scoreboard`` are both supplied AND the
    ``SKYN3T_ROUTER_ADAPTIVE`` env var is not disabled, the result is
    additionally filtered through ``_maybe_demote``: a backend that
    has been losing on the supplied stack falls back to the next
    alternative. See module-level doc for env-tuning knobs.

    ``event_bus`` is optional; when supplied, demotion decisions are
    published as ``CORTEX_DECISION`` events so the Activity timeline
    can render them alongside other autonomous-system decisions.
    """
    backend, model = _resolve_static(rel_path, stage_name)
    if (
        stack and scoreboard is not None and _adaptive_enabled()
        and hasattr(scoreboard, "backend_rate")
    ):
        try:
            # Failure-driven demote first: if the static pick is
            # consistently losing, switch regardless of cost.
            demoted = _maybe_demote(
                backend, model,
                rel_path=rel_path, stack=stack, scoreboard=scoreboard,
                event_bus=event_bus,
            )
            if demoted != (backend, model):
                return demoted
            # Both backends are working acceptably → consider cost.
            return _maybe_cost_demote(
                backend, model,
                rel_path=rel_path, stack=stack, scoreboard=scoreboard,
                event_bus=event_bus,
            )
        except Exception:
            logger.debug("router: adaptive demote failed; falling back", exc_info=True)
    return backend, model


def _resolve_static(
    rel_path: str,
    stage_name: Optional[str] = "code",
) -> Tuple[str, Optional[str]]:
    """The pure-static decision (split out so it's unit-testable and
    so the adaptive path can call it without recursion)."""
    if not rel_path:
        return resolve_model(stage_name)
    rl = rel_path.lower().replace("\\", "/")

    # Critical entrypoint files → OpenRouter UI specialist
    # (xiaomi/mimo-v2-flash). Same files that empirically broke
    # local CLIs (Kimi hangs on App.jsx, Copilot times out on
    # server/index.js) — now routed to an HTTP backend with a
    # 120s timeout instead of the 240s CLI idle.
    if rl.endswith(("app.jsx", "app.tsx", "main.jsx", "main.tsx")):
        return _TIERS["or_ui"]       # openrouter + xiaomi/mimo-v2-flash

    # Frontend by path hint OR extension → OpenRouter UI specialist.
    if any(h in rl for h in _FRONTEND_PATH_HINTS):
        return _TIERS["or_ui"]
    if rl.endswith(_FRONTEND_EXTS):
        return _TIERS["or_ui"]
    if rl.endswith(".css") and "/server/" not in rl:
        return _TIERS["or_ui"]
    if rl.endswith(".html"):
        return _TIERS["or_ui"]

    # Backend by path hint → OpenRouter code specialist (qwen3-coder).
    if any(h in rl for h in _BACKEND_PATH_HINTS):
        return _TIERS["or_backend"]  # openrouter + qwen/qwen3-coder

    # Default to the stage-level tier.
    return resolve_model(stage_name)
