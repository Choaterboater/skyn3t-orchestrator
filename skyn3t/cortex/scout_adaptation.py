"""Bridge repo-scout GitHub ingest into review-gated SkyN3t feature proposals.

Studio/build experience ingestion is handled elsewhere (ExperienceIngestor) and
must never spawn self-update feature proposals. Only GitHub scout ingest that
lands external repo knowledge in RAG may file a follow-on ``feature`` proposal
for operator approval before CodeImprover touches SkyN3t.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.cortex.scout_adaptation")

REPO_ROOT = Path(__file__).resolve().parents[2].resolve()
_FEATURE_COOLDOWN_SECONDS = 86_400.0
_last_feature_filed: Dict[str, float] = {}


def is_scout_github_source(source: str) -> bool:
    return str(source or "").strip().lower().startswith("repo_scout:github")


def build_adaptation_idea(payload: Dict[str, Any]) -> str:
    repo = str(payload.get("repo") or "unknown/repo").strip()
    query = str(payload.get("query") or payload.get("topic") or "").strip()
    lane = str(payload.get("lane") or "fit").strip() or "fit"
    description = str(payload.get("description") or "").strip()
    topics = [str(item).strip() for item in (payload.get("topics") or []) if str(item).strip()]
    language = str(payload.get("language") or "unknown").strip() or "unknown"

    focus = query or ", ".join(topics[:4]) or "its strongest patterns"
    idea_lines = [
        f"Adapt useful patterns from GitHub repo `{repo}` into SkyN3t for the {lane} lane.",
        f"Focus area: {focus}.",
        f"Primary language signal: {language}.",
        "Borrow architecture and workflow ideas only — do not copy code verbatim.",
        "Rebuild the capability using SkyN3t's existing agents, cortex, and web layers.",
    ]
    if description:
        idea_lines.append(f"Scout description: {description[:400]}")
    if topics:
        idea_lines.append(f"Topics: {', '.join(topics[:6])}.")
    return " ".join(idea_lines)


def should_spawn_feature(
    payload: Dict[str, Any],
    *,
    source: str,
    ingested_count: int,
) -> bool:
    try:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not getattr(settings, "cortex_scout_spawn_features", True):
        return False
    if not is_scout_github_source(source):
        return False
    if payload.get("adapt_to_skyn3t") is False:
        return False
    min_ingested = max(0, int(getattr(settings, "cortex_scout_spawn_min_ingested", 1)))
    if ingested_count < min_ingested:
        return False

    lane = str(payload.get("lane") or "").strip().lower()
    if lane and lane not in {"fit", "activity"}:
        return False
    reuse_risk = str(payload.get("reuse_risk") or "").lower()
    if reuse_risk == "high":
        return False
    return True


def file_adaptation_feature(
    *,
    payload: Dict[str, Any],
    source: str,
    parent_proposal_id: str,
    ingested_count: int,
    ingested_paths: List[str],
) -> Optional[str]:
    """File a review-gated feature proposal after scout GitHub ingest."""
    if not should_spawn_feature(payload, source=source, ingested_count=ingested_count):
        return None

    repo = str(payload.get("repo") or "").strip()
    repo_key = str(payload.get("repo_key") or repo).strip() or repo
    signature = f"scout-feature:{repo_key or parent_proposal_id}"
    now = time.time()
    last = _last_feature_filed.get(signature, 0.0)
    if now - last < _FEATURE_COOLDOWN_SECONDS:
        logger.debug("skipping duplicate scout feature for %s (cooldown)", signature)
        return None
    _last_feature_filed[signature] = now

    from skyn3t.cortex import get_store
    from skyn3t.cortex.feature_suggester import infer_feature_target_file

    idea = build_adaptation_idea(payload)
    target_file = infer_feature_target_file(idea, repo_root=REPO_ROOT)
    query = str(payload.get("query") or payload.get("topic") or "").strip()
    lane = str(payload.get("lane") or "fit").strip() or "fit"
    paths_text = ", ".join(ingested_paths[:6]) if ingested_paths else "none captured"

    feature_payload: Dict[str, Any] = {
        "idea": idea,
        "repo": repo,
        "query": query,
        "lane": lane,
        "action": "adapt_scout_pattern",
        "parent_ingest_proposal_id": parent_proposal_id,
        "ingested_count": ingested_count,
        "ingested_paths": list(ingested_paths[:20]),
        "repo_root": str(REPO_ROOT.resolve()),
        "source_platform": str(payload.get("source_platform") or "github"),
    }
    if target_file:
        feature_payload["target_file"] = target_file

    detail_lines = [
        "_Follow-on from an auto-applied GitHub scout ingest. Ingestion into RAG "
        "already ran; approving this proposal lets CodeImprover adapt the pattern "
        "into SkyN3t's own codebase._",
        "",
        f"- Source repo: `{repo}`",
        f"- Scout query: `{query or 'n/a'}`",
        f"- Lane: `{lane}`",
        f"- Ingested paths: {paths_text}",
        "",
        "## Adaptation brief",
        idea,
        "",
        "## On approval",
    ]
    if target_file:
        detail_lines.extend(
            [
                f"- Starting file: `{target_file}`",
                "- CodeImprover will draft and apply a targeted self-update patch.",
            ]
        )
    else:
        detail_lines.append(
            "- Starting file not inferred yet; CodeImprover will pick a target at apply time."
        )

    try:
        proposal = get_store().create(
            kind="feature",
            title=f"Adapt to SkyN3t: {repo or 'GitHub scout finding'}",
            summary=(
                f"Port scout finding from {repo} into SkyN3t ({lane} lane, "
                f"{ingested_count} doc(s) ingested)"
            )[:200],
            detail="\n".join(detail_lines),
            payload=feature_payload,
            source=f"scout_adaptation:{repo_key or parent_proposal_id}",
            force_requires_approval=True,
        )
        return proposal.id
    except Exception:
        logger.exception("failed to file scout adaptation feature for %s", repo)
        return None


def maybe_spawn_feature_after_scout_ingest(
    *,
    payload: Dict[str, Any],
    source: str,
    parent_proposal_id: str,
    ingested: List[Any],
) -> Optional[str]:
    ingested_paths: List[str] = []
    for item in ingested or []:
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            if path:
                ingested_paths.append(path)
    return file_adaptation_feature(
        payload=payload,
        source=source,
        parent_proposal_id=parent_proposal_id,
        ingested_count=len(ingested or []),
        ingested_paths=ingested_paths,
    )
