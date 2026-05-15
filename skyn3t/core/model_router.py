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
  build_verifier / boot_verifier — local subprocess, no LLM call.

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
from pathlib import Path
from typing import Dict, Optional, Tuple

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
    "cheap":    ("kimi_cli",    None),
    "balanced": ("copilot_cli", None),
    "strong":   ("claude_cli",  "opus"),
    "ui":       ("copilot_cli", "gpt-5.3-codex"),
}


# Default stage → tier policy. Keys are LOWERCASED stage names matching
# what the runner publishes in PROJECT_STAGE_STARTED / project.json.
_DEFAULT_STAGE_POLICY: Dict[str, str] = {
    # framing & fan-out — cheap is fine
    "brainstorm":         "cheap",
    "research":           "cheap",
    "designer":           "cheap",
    "writer":             "cheap",
    "marketer":           "cheap",
    "business_analyst":   "cheap",

    # system-shape decisions — quality compounds, prefer strong
    "architect":          "strong",
    # Code stage uses per-file routing inside CodeAgent (see
    # resolve_model_for_file). The agent-level tier here is just
    # the fallback for non-file-specific LLM calls (planning).
    "code":               "balanced",
    "code_agent":         "balanced",
    "code_improver":      "balanced",
    "reviewer":           "strong",

    # verifiers don't call LLMs — listed for completeness
    "build_verifier":     "cheap",
    "boot_verifier":      "cheap",
}


def _load_overrides() -> Dict[str, str]:
    """Load env-pointed JSON override, if present.

    Format: a dict mapping stage_name → tier name, e.g.
    ``{"reviewer": "balanced", "code": "balanced"}`` when the user wants
    to cap spend.
    """
    path = os.environ.get("SKYN3T_MODEL_ROUTING")
    if not path:
        return {}
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


def tier_for_stage(stage_name: Optional[str]) -> str:
    """Return the tier label ('cheap' / 'balanced' / 'strong') for a
    stage. Defaults to ``cheap`` when the stage isn't recognized — we'd
    rather under-spend than over-spend on an unknown stage."""
    if not stage_name:
        return "cheap"
    s = stage_name.lower()
    overrides = _load_overrides()
    if s in overrides:
        return overrides[s]
    return _DEFAULT_STAGE_POLICY.get(s, "cheap")


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
    tier = tier_for_stage(stage_name)
    return _TIERS.get(tier, _TIERS["cheap"])


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


def resolve_model_for_file(
    rel_path: str,
    stage_name: Optional[str] = "code",
) -> Tuple[str, Optional[str]]:
    """Pick (backend, model) for a SPECIFIC file inside the code stage.

    Frontend (.jsx, .css, components/, pages/, hooks/) → kimi_cli
    (pretty UI specialist).

    Backend (server/, api/, routes/, adapters/) → copilot_cli
    /gpt-5.3-codex (code-correctness specialist).

    Everything else → stage-level resolution (config files, top-level
    files, unrecognized paths).
    """
    if not rel_path:
        return resolve_model(stage_name)
    rl = rel_path.lower().replace("\\", "/")

    # Critical entrypoint files: route to copilot/codex for reliability.
    # Empirical from v34/v35: Kimi consistently hangs on App.jsx (the
    # largest single frontend file — root component with state, drawer,
    # settings, search, filters). Codex shipped it perfectly in v15.
    # main.jsx is the Vite/React mount point; same concern.
    if rl.endswith(("app.jsx", "app.tsx", "main.jsx", "main.tsx")):
        return _TIERS["ui"]          # copilot_cli + gpt-5.3-codex

    # Frontend by path hint OR extension.
    if any(h in rl for h in _FRONTEND_PATH_HINTS):
        return _TIERS["cheap"]  # kimi_cli — visual specialist
    if rl.endswith(_FRONTEND_EXTS):
        return _TIERS["cheap"]
    if rl.endswith(".css") and "/server/" not in rl:
        return _TIERS["cheap"]
    if rl.endswith(".html"):
        return _TIERS["cheap"]

    # Backend by path hint.
    if any(h in rl for h in _BACKEND_PATH_HINTS):
        return _TIERS["balanced"]  # copilot_cli — backend code

    # Default to the stage-level tier.
    return resolve_model(stage_name)
