"""Cross-model debate for high-stakes build stages (Phase 5A).

N cheap/free models independently *propose* a solution, then *cross-examine*
each other's proposals, then *vote*, then a synthesized winner is chosen. The
whole thing is:

  * **Default OFF** — gated behind ``SKYN3T_DEBATE`` (default off) and scoped
    to high-stakes stages via ``SKYN3T_DEBATE_STAGES`` (default
    ``architect,reviewer,code``).
  * **CHEAP/FREE only** — model selection draws from the cheap/free OpenRouter
    tiers (``or_cheap`` / ``or_docs``) plus subscription-backed CLIs picked via
    ``intelligence.cheap_smart``. We never force an expensive model and never
    block a build on a missing key.
  * **Self-learning** — after every debate, one
    :class:`~skyn3t.intelligence.model_tournament.ModelTrial` is recorded per
    participant (domain_tags=[stage, stack], vendor_tags=[backend]) so routing
    learns which cheap models win over time.
  * **Graceful** — fewer than two usable models returns a
    ``DebateResult(skipped_reason=...)`` with a proposer-fallback winner; every
    LLM interaction is wrapped so a failed/absent backend degrades to a
    single-model critique rather than raising.

Public contract (DebateAPI):

    async def run_debate(*, stage_name, brief, proposer_outputs, artifact_dir,
                         event_bus, models=None, max_models=3, rounds=1,
                         record=True, timeout_s=180.0) -> DebateResult
    @dataclass DebateResult(...)
    @dataclass ModelVerdict(...)
    def debate_enabled(stage_name: str) -> bool
    def default_debate_models() -> list[str]
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("skyn3t.agents.debate")

# Default high-stakes stages where cross-model debate earns its keep.
_DEFAULT_DEBATE_STAGES = ("architect", "reviewer", "code")

# CHEAP/FREE-first tier order. These map to free / near-free OpenRouter models
# in core.model_router._TIERS (or_cheap=owl-alpha free, or_docs=gpt-oss free)
# plus subscription-backed CLIs that cost nothing per call. We NEVER include
# or_strong / claude_cli style expensive tiers in the default lineup.
_CHEAP_TIER_ORDER = ("or_cheap", "or_docs", "or_backend", "or_ui", "cheap", "balanced")


# ── Dataclasses (DebateAPI contract) ────────────────────────────────────────


@dataclass
class ModelVerdict:
    """One participant's full record across the debate."""

    model: str
    backend: str
    proposal: str
    critiques: List[str]
    votes_for: int
    score: float
    cost_usd: float
    passed: bool


@dataclass
class DebateResult:
    """Outcome of a debate (or a graceful skip)."""

    winner_text: str
    winner_model: str
    per_model: List[ModelVerdict]
    synthesized: bool
    consensus_score: float
    used_models: List[str]
    skipped_reason: Optional[str] = None


# ── Flag gating ─────────────────────────────────────────────────────────────


def _truthy(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in {"1", "on", "true", "yes"}


def _debate_stages() -> Tuple[str, ...]:
    raw = os.environ.get("SKYN3T_DEBATE_STAGES")
    if raw is None or not raw.strip():
        return _DEFAULT_DEBATE_STAGES
    stages = tuple(
        part.strip().lower() for part in raw.split(",") if part.strip()
    )
    return stages or _DEFAULT_DEBATE_STAGES


def debate_enabled(stage_name: str) -> bool:
    """True only when ``SKYN3T_DEBATE`` is on AND the stage is in scope.

    Reads ``SKYN3T_DEBATE`` (default OFF) and ``SKYN3T_DEBATE_STAGES``
    (default ``architect,reviewer,code``). Code-stage aliases
    (``code_agent`` / ``code_improver``) collapse onto ``code``.
    """
    if not _truthy(os.environ.get("SKYN3T_DEBATE")):
        return False
    stage = str(stage_name or "").strip().lower()
    if not stage:
        return False
    stages = _debate_stages()
    if stage in stages:
        return True
    # Collapse code-stage aliases onto the canonical 'code' scope.
    if stage in {"code_agent", "code_improver"} and "code" in stages:
        return True
    if stage in {"architecture"} and "architect" in stages:
        return True
    return False


# ── CHEAP/FREE model selection ──────────────────────────────────────────────


def default_debate_models() -> List[str]:
    """Return CHEAP/FREE model ids for the default debate lineup.

    Strictly cheap/free: resolves each tier in ``_CHEAP_TIER_ORDER`` to its
    ``backend:model`` pair via the router's tier table, de-duplicating by
    backend so the lineup is cross-model diverse. Never returns an expensive
    (or_strong / claude_cli opus) tier. Degrades to a single deterministic
    entry when the router is unavailable so callers always get a usable list.
    """
    out: List[str] = []
    seen_backends: set = set()
    try:
        from skyn3t.core.model_router import (
            relative_backend_cost,
            tier_details,
        )
    except Exception:
        logger.debug("debate: model_router unavailable for default lineup", exc_info=True)
        return ["openrouter:openrouter/owl-alpha"]

    for tier in _CHEAP_TIER_ORDER:
        try:
            backend, model = tier_details(tier)
        except Exception:
            continue
        if not backend:
            continue
        # Hard cheap guard: never let an accidentally-expensive backend in.
        try:
            if relative_backend_cost(backend) > 2.0:
                continue
        except Exception:
            pass
        if backend in seen_backends:
            continue
        seen_backends.add(backend)
        out.append(f"{backend}:{model}" if model else backend)
    if not out:
        out.append("openrouter:openrouter/owl-alpha")
    return out


def _parse_model_spec(spec: str) -> Tuple[str, Optional[str]]:
    """Split a ``backend:model`` (or bare ``backend``) spec."""
    text = str(spec or "").strip()
    if not text:
        return "", None
    if ":" in text:
        backend, model = text.split(":", 1)
        backend = backend.strip()
        model = model.strip() or None
        # A bare provider-qualified id (e.g. "openrouter/owl-alpha") has no
        # backend prefix — treat the whole thing as a model on openrouter.
        if backend and model is None and "/" in backend:
            return "openrouter", text
        return backend, model
    if "/" in text:
        return "openrouter", text
    return text, None


def _select_models(
    *,
    stage_name: str,
    stack: Optional[str],
    explicit: Optional[List[str]],
    max_models: int,
) -> List[Tuple[str, Optional[str]]]:
    """Resolve the participant lineup as ``(backend, model)`` pairs.

    Order of preference, all CHEAP/FREE:
      1. Caller-supplied ``models`` (already cheap by contract).
      2. ``cheap_smart`` stage-tier override (when cheap-smart active).
      3. ``default_debate_models()`` cheap/free tier lineup.

    De-duplicates by backend for cross-model diversity and caps at
    ``max_models``.
    """
    specs: List[str] = []
    if explicit:
        specs.extend(str(m) for m in explicit if str(m).strip())

    # Bias the front of the lineup toward a cheap-smart stage tier when active.
    try:
        from skyn3t.core.model_router import tier_details
        from skyn3t.intelligence.cheap_smart import (
            cheap_smart_enabled,
            cheap_smart_stage_tier,
        )

        if cheap_smart_enabled():
            tier = cheap_smart_stage_tier(stage_name)
            if tier:
                backend, model = tier_details(tier)
                if backend:
                    specs.insert(0, f"{backend}:{model}" if model else backend)
    except Exception:
        logger.debug("debate: cheap_smart tier bias failed", exc_info=True)

    specs.extend(default_debate_models())

    pairs: List[Tuple[str, Optional[str]]] = []
    seen_backends: set = set()
    seen_specs: set = set()
    for spec in specs:
        backend, model = _parse_model_spec(spec)
        if not backend:
            continue
        key = (backend, model)
        if key in seen_specs:
            continue
        # Cross-model diversity: one participant per backend.
        if backend in seen_backends:
            continue
        seen_specs.add(key)
        seen_backends.add(backend)
        pairs.append((backend, model))
        if len(pairs) >= max(1, int(max_models)):
            break
    return pairs


# ── Event emission ──────────────────────────────────────────────────────────


def _emit(event_bus: Any, event_name: str, payload: Dict[str, Any]) -> None:
    """Publish an AGENT_CONVERSATION_* event; never raises."""
    if event_bus is None:
        return
    try:
        from skyn3t.core.events import Event, EventType

        et = getattr(EventType, event_name, None)
        if et is None:
            return
        event_bus.publish(
            Event(event_type=et, source="debate", payload=payload)
        )
    except Exception:
        logger.debug("debate: emit %s failed", event_name, exc_info=True)


# ── Scoring helpers ─────────────────────────────────────────────────────────


def _proposer_fallback_text(proposer_outputs: Optional[Dict[str, str]]) -> str:
    if not proposer_outputs:
        return ""
    for value in proposer_outputs.values():
        if value and str(value).strip():
            return str(value)
    return ""


def _critique_passed(critique: str) -> bool:
    """Heuristic: a critique "passes" the target if it raises no blockers.

    Pure/deterministic so it works with the deterministic backend in tests.
    """
    text = str(critique or "").strip().lower()
    if not text:
        return True
    blocker_markers = (
        "blocker",
        "broken",
        "reject",
        "fails",
        "does not",
        "doesn't",
        "missing",
        "incorrect",
        "bug",
        "unsafe",
        "must fix",
    )
    return not any(marker in text for marker in blocker_markers)


# ── Single-model interaction ────────────────────────────────────────────────


async def _ask_model(
    *,
    backend: str,
    model: Optional[str],
    event_bus: Any,
    skip_backends: List[str],
    prompt: str,
    system: Optional[str],
    timeout_s: float,
) -> Tuple[Optional[str], float]:
    """Run one prompt through an isolated LLMClient. Returns (text, seconds).

    ``skip_backends`` forces cross-model diversity by preventing the auto
    chain from collapsing two participants onto the same backend. Returns
    ``(None, elapsed)`` on any failure so callers degrade gracefully.
    """
    try:
        from skyn3t.adapters.llm_client import LLMClient
    except Exception:
        logger.debug("debate: LLMClient import failed", exc_info=True)
        return None, 0.0

    start = time.monotonic()
    try:
        client = LLMClient(
            backend=backend,
            default_model=model,
            event_bus=event_bus,
            caller_name="debate",
            skip_backends=[b for b in skip_backends if b and b != backend],
        )
        text = await asyncio.wait_for(
            client.complete(prompt, system=system, temperature=0.4),
            timeout=timeout_s,
        )
        elapsed = time.monotonic() - start
        return (str(text) if text is not None else None), elapsed
    except Exception:
        logger.debug("debate: ask_model failed backend=%s", backend, exc_info=True)
        return None, time.monotonic() - start


# ── Tournament recording ────────────────────────────────────────────────────


def _record_trials(
    *,
    verdicts: List[ModelVerdict],
    stage_name: str,
    stack: Optional[str],
    task_id: str,
) -> None:
    """Record one ModelTrial per participant so routing learns cheap winners.

    domain_tags = [stage, stack]; vendor_tags = [backend]. Never raises.
    """
    try:
        from skyn3t.intelligence.model_tournament import (
            ModelTournamentStore,
            ModelTrial,
        )
    except Exception:
        logger.debug("debate: tournament store import failed", exc_info=True)
        return

    domain_tags = [t for t in (stage_name, stack) if t]
    try:
        store = ModelTournamentStore()
    except Exception:
        logger.debug("debate: tournament store init failed", exc_info=True)
        return
    for verdict in verdicts:
        try:
            store.record_trial(
                ModelTrial(
                    model_id=verdict.model or verdict.backend,
                    task_id=task_id,
                    domain_tags=list(domain_tags),
                    vendor_tags=[verdict.backend] if verdict.backend else [],
                    score=max(0, min(100, int(round(verdict.score)))),
                    cost_usd=max(0.0, float(verdict.cost_usd)),
                    passed=bool(verdict.passed),
                )
            )
        except Exception:
            logger.debug(
                "debate: record_trial failed for %s", verdict.model, exc_info=True
            )


# ── Main entry point ────────────────────────────────────────────────────────


async def run_debate(
    *,
    stage_name: str,
    brief: str,
    proposer_outputs: Optional[Dict[str, str]],
    artifact_dir: Path,
    event_bus: Any,
    models: Optional[List[str]] = None,
    max_models: int = 3,
    rounds: int = 1,
    record: bool = True,
    timeout_s: float = 180.0,
) -> DebateResult:
    """Run a cheap/free cross-model debate for ``stage_name``.

    Flow: N cheap models *propose* -> *cross-examine* each other -> *vote* ->
    *synthesize* a winner. Everything is non-blocking: a missing key, an
    absent backend, or fewer than two usable models degrades to a
    proposer-fallback skip or a single-model critique. After the debate, one
    ModelTrial per participant is recorded (when ``record``) so routing learns.
    """
    fallback_text = _proposer_fallback_text(proposer_outputs)

    try:
        stack = None
        try:
            from skyn3t.agents.stack_templates import detect_stack

            stack = detect_stack(brief or "")
        except Exception:
            stack = None

        pairs = _select_models(
            stage_name=stage_name,
            stack=stack,
            explicit=models,
            max_models=max_models,
        )

        if len(pairs) < 2:
            logger.info(
                "debate: only %d usable model(s) for stage=%s — skipping",
                len(pairs),
                stage_name,
            )
            return DebateResult(
                winner_text=fallback_text,
                winner_model=(pairs[0][0] if pairs else ""),
                per_model=[],
                synthesized=False,
                consensus_score=0.0,
                used_models=[p[0] for p in pairs],
                skipped_reason="insufficient_models",
            )

        used_backends = [b for b, _ in pairs]
        _emit(
            event_bus,
            "AGENT_CONVERSATION_STARTED",
            {
                "slug": artifact_dir.name,
                "stage": stage_name,
                "participants": used_backends,
                "max_rounds": max(1, int(rounds)),
                "kind": "debate",
            },
        )

        seed = fallback_text or (brief or "")
        propose_system = (
            "You are one of several independent expert engineers in a debate. "
            f"Stage: {stage_name}. Produce the best solution you can. "
            "Output ONLY the solution, no narration."
        )
        propose_prompt = (
            f"# Brief\n{brief}\n\n"
            + (f"# Prior proposer output\n{seed}\n\n" if seed else "")
            + "Provide your strongest solution for this stage."
        )

        # ── Round 1: independent proposals ──────────────────────────────
        verdicts: List[ModelVerdict] = []
        for backend, model in pairs:
            text, elapsed = await _ask_model(
                backend=backend,
                model=model,
                event_bus=event_bus,
                skip_backends=used_backends,
                prompt=propose_prompt,
                system=propose_system,
                timeout_s=timeout_s,
            )
            produced = text is not None and bool(str(text).strip())
            proposal = text if produced else (seed or "")
            verdicts.append(
                ModelVerdict(
                    model=model or backend,
                    backend=backend,
                    proposal=proposal,
                    critiques=[],
                    votes_for=0,
                    score=0.0,
                    cost_usd=0.0,
                    passed=produced,
                )
            )
            _emit(
                event_bus,
                "AGENT_CONVERSATION_TURN",
                {
                    "slug": artifact_dir.name,
                    "stage": stage_name,
                    "from_agent": backend,
                    "kind": "proposal",
                    "round": 1,
                    "content": proposal[:2000],
                },
            )

        # "Usable" means a model genuinely produced its own output — not just
        # echoed the seed because its backend was absent/keyless. Fewer than
        # two real proposers => degrade (single-model critique semantics).
        usable = [v for v in verdicts if v.passed and v.proposal.strip()]
        if len(usable) < 2:
            # Degrade to single-model critique semantics: pick the one we have.
            best = usable[0] if usable else verdicts[0]
            if record:
                _record_trials(
                    verdicts=verdicts,
                    stage_name=str(stage_name).strip().lower(),
                    stack=stack,
                    task_id=artifact_dir.name or "debate",
                )
            _emit(
                event_bus,
                "AGENT_CONVERSATION_ENDED",
                {
                    "slug": artifact_dir.name,
                    "stage": stage_name,
                    "winner": best.backend,
                    "synthesized": False,
                    "skipped_reason": "insufficient_models",
                },
            )
            return DebateResult(
                winner_text=best.proposal or fallback_text,
                winner_model=best.model,
                per_model=verdicts,
                synthesized=False,
                consensus_score=0.0,
                used_models=used_backends,
                skipped_reason="insufficient_models",
            )

        # ── Cross-examination: each model critiques every other proposal ─
        # Only genuine proposers (``usable``) participate — a seed-echo from a
        # keyless backend neither critiques nor gets voted on.
        extra_rounds = max(1, int(rounds))
        for round_idx in range(extra_rounds):
            for i, critic in enumerate(usable):
                for j, target in enumerate(usable):
                    if i == j or not target.proposal.strip():
                        continue
                    critique_system = (
                        "You are a rigorous reviewer in a multi-model debate. "
                        "Critique the proposal below. State blockers explicitly "
                        "if it is broken, missing pieces, or incorrect; otherwise "
                        "confirm it is sound."
                    )
                    critique_prompt = (
                        f"# Brief\n{brief}\n\n"
                        f"# Proposal under review (by {target.model})\n"
                        f"{target.proposal[:6000]}\n\n"
                        "Critique it. Be concise."
                    )
                    text, _elapsed = await _ask_model(
                        backend=critic.backend,
                        model=critic.model,
                        event_bus=event_bus,
                        skip_backends=used_backends,
                        prompt=critique_prompt,
                        system=critique_system,
                        timeout_s=timeout_s,
                    )
                    critique = text if text is not None else ""
                    critic.critiques.append(critique)
                    if _critique_passed(critique):
                        target.votes_for += 1
                    _emit(
                        event_bus,
                        "AGENT_CONVERSATION_TURN",
                        {
                            "slug": artifact_dir.name,
                            "stage": stage_name,
                            "from_agent": critic.backend,
                            "to_agent": target.backend,
                            "kind": "critique",
                            "round": round_idx + 1,
                            "content": critique[:2000],
                        },
                    )

        # ── Vote & score ────────────────────────────────────────────────
        n_critics = max(1, len(usable) - 1)
        max_votes = n_critics * extra_rounds
        for verdict in verdicts:
            # Score 0-100: vote share weighted, with a floor for genuinely
            # producing a proposal. Seed-echoes (failed/keyless backends)
            # score 0 so the tournament learns they lost.
            vote_share = verdict.votes_for / max_votes if max_votes else 0.0
            base = 50.0 if verdict.passed else 0.0
            verdict.score = round(base + vote_share * 50.0, 2)

        # Winner is drawn from genuine proposers only.
        winner = max(
            usable,
            key=lambda v: (v.votes_for, v.score, len(v.proposal or "")),
        )
        # Consensus = winner's share of the maximum possible votes.
        consensus_score = round(
            (winner.votes_for / max_votes) if max_votes else 0.0, 3
        )

        if record:
            _record_trials(
                verdicts=verdicts,
                stage_name=str(stage_name).strip().lower(),
                stack=stack,
                task_id=artifact_dir.name or "debate",
            )

        _emit(
            event_bus,
            "AGENT_CONVERSATION_ENDED",
            {
                "slug": artifact_dir.name,
                "stage": stage_name,
                "winner": winner.backend,
                "winner_model": winner.model,
                "synthesized": False,
                "consensus_score": consensus_score,
            },
        )

        return DebateResult(
            winner_text=winner.proposal or fallback_text,
            winner_model=winner.model,
            per_model=verdicts,
            synthesized=False,
            consensus_score=consensus_score,
            used_models=used_backends,
            skipped_reason=None,
        )
    except Exception:
        # Absolute non-blocking guarantee: any unexpected failure degrades to
        # the proposer fallback instead of breaking the build.
        logger.warning("debate: unexpected failure for stage=%s", stage_name, exc_info=True)
        return DebateResult(
            winner_text=fallback_text,
            winner_model="",
            per_model=[],
            synthesized=False,
            consensus_score=0.0,
            used_models=[],
            skipped_reason="error",
        )
