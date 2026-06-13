"""Reflective skill evolution — the compounding step SkyN3t lacked.

The system already GRADES and PRUNES skills (success/failure counts → score),
but it never REWRITES a failing skill. This module closes that loop: it takes
the worst-performing skills, asks a cheap/free model to rewrite the body from the
observed failures, and files the rewrite as an approval-gated DRAFT (the live
skill is untouched until the owner approves it) plus a proposal for visibility.

Owner directive: cheap/free only — the rewrite call runs on OpenRouter at
temperature 0 (deterministic + response-cacheable), no Claude.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

logger = logging.getLogger("skyn3t.cortex.skill_evolver")

# A rewritten skill body must stay well under the library's working size.
MAX_BODY_CHARS = 15_000


def evolve_candidates(
    library: Any,
    *,
    max_score: float = -0.2,
    min_samples: int = 3,
    limit: int = 3,
) -> List[Any]:
    """Worst-performing skills with enough signal to trust the verdict.

    Only skills with at least ``min_samples`` observations AND a score at or
    below ``max_score`` qualify — so we never rewrite a skill on noise.
    """
    try:
        skills = list(library.all())
    except Exception:
        logger.debug("skill library scan failed", exc_info=True)
        return []
    cands = [
        s for s in skills
        if (getattr(s, "success_count", 0) + getattr(s, "failure_count", 0)) >= min_samples
        and getattr(s, "score", 0.0) <= max_score
    ]
    cands.sort(key=lambda s: getattr(s, "score", 0.0))  # worst first
    return cands[: max(0, int(limit))]


def _rewrite_prompt(skill: Any, traces: List[str]) -> str:
    trace_block = "\n".join(f"- {t}" for t in traces[:8]) or (
        "- (no specific traces captured; the skill simply underperforms its peers)"
    )
    return (
        "You are improving a reusable engineering SKILL that keeps failing in practice.\n\n"
        f"SKILL NAME: {skill.name}\n"
        f"PURPOSE: {getattr(skill, 'description', '') or '(none given)'}\n\n"
        f"CURRENT BODY:\n{getattr(skill, 'body', '')}\n\n"
        f"FAILURES OBSERVED (builds that used this skill still failed):\n{trace_block}\n\n"
        "Rewrite the skill BODY so it fixes the failure mode above. Keep the same "
        "purpose and scope; be concrete and actionable; prefer checklists and "
        "explicit do/don't rules over prose. Output ONLY the new markdown body — "
        f"no frontmatter, no code fences, under {MAX_BODY_CHARS} characters."
    )


async def evolve_one(
    skill: Any,
    traces: List[str],
    *,
    llm: Any,
    library: Any,
    proposal_store: Any,
    source: str = "skill_evolver",
) -> Optional[Any]:
    """Rewrite one skill → draft + proposal. Returns the proposal, or None."""
    prompt = _rewrite_prompt(skill, traces)
    try:
        new_body = await llm.complete(prompt, temperature=0, max_tokens=4000)
    except Exception:
        logger.warning("skill rewrite call failed for %s", getattr(skill, "name", "?"), exc_info=True)
        return None
    new_body = (new_body or "").strip()
    old_body = (getattr(skill, "body", "") or "").strip()
    if not new_body or len(new_body) > MAX_BODY_CHARS or new_body == old_body:
        return None

    # Build the rewritten skill: same identity/tags/triggers, body replaced,
    # counts reset so it re-earns its grade. Written as a DRAFT — the live skill
    # is untouched until the owner approves (approve_draft / dashboard).
    from skyn3t.intelligence.skill_library import Skill

    draft = Skill(
        name=skill.name,
        body=new_body,
        description=getattr(skill, "description", ""),
        author=getattr(skill, "author", ""),
        tags=list(getattr(skill, "tags", [])),
        triggers=list(getattr(skill, "triggers", [])),
        success_count=0,
        failure_count=0,
        source=f"{source} (rewrite of {getattr(skill, 'source', '') or 'unknown'})",
    )
    try:
        library.upsert_draft(draft)
    except Exception:
        logger.warning("failed to write skill draft for %s", skill.name, exc_info=True)
        return None

    detail = (
        f"Reflective rewrite of skill '{skill.name}' "
        f"(score {getattr(skill, 'score', 0.0):+.2f}, "
        f"{getattr(skill, 'success_count', 0)}W/{getattr(skill, 'failure_count', 0)}L).\n\n"
        f"--- OLD BODY ---\n{old_body}\n\n--- NEW BODY ---\n{new_body}\n"
    )
    try:
        return proposal_store.create(
            kind="code_patch",
            title=f"Evolve skill: {skill.name}",
            summary=f"Rewrite underperforming skill '{skill.name}' from failure traces.",
            detail=detail,
            payload={
                "skill_name": skill.name,
                "skill_slug": getattr(skill, "slug", ""),
                "draft": True,
            },
            source=source,
            requires_approval=True,
        )
    except Exception:
        logger.warning("failed to file skill-evolution proposal for %s", skill.name, exc_info=True)
        return None


async def run_once(
    *,
    library: Any = None,
    llm: Any = None,
    proposal_store: Any = None,
    get_traces: Optional[Callable[[Any], List[str]]] = None,
    max_score: float = -0.2,
    min_samples: int = 3,
    limit: int = 3,
) -> List[str]:
    """Evolve up to ``limit`` worst skills. Returns the names proposed.

    Dependencies default to the live library / a cheap OpenRouter client / the
    cortex proposal store, but are injectable for testing.
    """
    if library is None:
        from skyn3t.intelligence.skill_library import get_default_library

        library = get_default_library()
    if proposal_store is None:
        from skyn3t.cortex.proposals import ProposalStore

        proposal_store = ProposalStore()
    if llm is None:
        from skyn3t.adapters import LLMClient

        llm = LLMClient(caller_name="skill_evolver")

    cands = evolve_candidates(
        library, max_score=max_score, min_samples=min_samples, limit=limit
    )
    proposed: List[str] = []
    for skill in cands:
        traces: List[str] = []
        if get_traces is not None:
            try:
                traces = list(get_traces(skill) or [])
            except Exception:
                traces = []
        prop = await evolve_one(
            skill, traces, llm=llm, library=library, proposal_store=proposal_store
        )
        if prop is not None:
            proposed.append(skill.name)
    return proposed
