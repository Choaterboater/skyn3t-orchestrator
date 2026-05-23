"""Apply-handlers for proposal kinds beyond tuning and code_patch.

Registered with the global ProposalStore at orchestrator boot.
Each handler is async and receives the proposal payload dict.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from skyn3t.cortex.external_pattern_synthesizer import ExternalPatternSynthesizer
from skyn3t.cortex.external_repo_ingest import ExternalRepoDocIngestor
from skyn3t.cortex.feature_suggester import infer_feature_target_file
from skyn3t.cortex.review_utils import normalize_review_risks
from skyn3t.cortex.scout_adaptation import maybe_spawn_feature_after_scout_ingest

logger = logging.getLogger("skyn3t.cortex.handlers")
REPO_ROOT = Path(__file__).resolve().parents[2].resolve()


def _normalize_repo_relative_path(target_file: Any, *, require_exists: bool = False) -> str:
    candidate = str(target_file or "").strip()
    if not candidate:
        return ""
    target_path = Path(candidate)
    if not target_path.is_absolute():
        target_path = REPO_ROOT / target_path
    target_path = target_path.resolve()
    try:
        relative_path = target_path.relative_to(REPO_ROOT)
    except ValueError:
        return ""
    if require_exists and (not target_path.exists() or not target_path.is_file()):
        return ""
    return relative_path.as_posix()


def _resolve_repo_file(target_file: Any) -> str:
    return _normalize_repo_relative_path(target_file, require_exists=True)


def install_handlers(orchestrator) -> None:
    """Register apply-handlers for the review-gated Cortex proposal kinds."""
    try:
        from skyn3t.core.agent import TaskRequest
        from skyn3t.cortex import get_store  # local import to avoid circular dependency
    except Exception:
        logger.exception("cortex handler dependencies unavailable")
        return
    store = get_store()

    async def feature_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Approved feature idea → draft and apply a targeted self-update patch."""
        idea = payload.get("idea") or payload.get("summary") or "improvement"
        try:
            improver = orchestrator.agents.get("code_improver")
            if improver is None:
                return {"ok": False, "error": "code_improver agent not registered"}
            proposal_id = str(payload.get("_proposal_id") or "").strip()
            current_proposal = store.get(proposal_id) if proposal_id else None
            target_file = _resolve_repo_file(payload.get("target_file"))
            if not target_file:
                inferred_target = _resolve_repo_file(
                    infer_feature_target_file(str(idea), repo_root=REPO_ROOT)
                )
                if inferred_target:
                    target_file = inferred_target
            if not target_file:
                return {"ok": False, "error": "could not infer a starting file for this idea"}

            proposals = store.list()
            current_created_at = getattr(current_proposal, "created_at", None)
            if current_created_at is not None:
                older_feature = next(
                    (
                        proposal
                        for proposal in proposals
                        if proposal.kind == "feature"
                        and proposal.status in {"approved", "applying"}
                        and proposal.id != proposal_id
                        and getattr(proposal, "created_at", None) is not None
                        and proposal.created_at <= current_created_at
                        and str((proposal.payload or {}).get("repo_root") or str(REPO_ROOT))
                        == str(REPO_ROOT)
                        and _normalize_repo_relative_path(
                            (proposal.payload or {}).get("target_file")
                        )
                        == target_file
                    ),
                    None,
                )
                if older_feature is not None:
                    return {
                        "ok": True,
                        "status": "already-running",
                        "target_file": target_file,
                        "feature_proposal_id": older_feature.id,
                        "details": "An older approved feature proposal is already running for that file.",
                    }

            active_patch = next(
                (
                    proposal
                    for proposal in proposals
                    if proposal.kind == "code_patch"
                    and proposal.status in {"pending", "approved", "applying"}
                    and str((proposal.payload or {}).get("repo_root") or REPO_ROOT) == str(REPO_ROOT)
                    and _normalize_repo_relative_path(
                        (proposal.payload or {}).get("target_file")
                    ) == target_file
                ),
                None,
            )
            if active_patch is not None:
                return {
                    "ok": True,
                    "status": "already-running",
                    "target_file": target_file,
                    "code_patch_proposal_id": active_patch.id,
                    "details": "A code patch is already active for that file.",
                }
            req = TaskRequest(
                title="apply feature proposal",
                input_data={
                    "target_file": target_file,
                    "repo_root": str(REPO_ROOT),
                    "rationale": str(idea)[:500],
                    "intent": "feature_implementation",
                    "source": "cortex.feature",
                    "user_initiated": True,
                    "use_mcp": False,
                },
            )
            result = await improver.execute(req)
            out = getattr(result, "output", {}) or {}
            if out.get("proposed") and out.get("proposal_id") and not out.get("applied"):
                return {
                    "ok": True,
                    "status": "applying",
                    "spawned": "code_improver",
                    "target_file": target_file,
                    "code_patch_proposal_id": out.get("proposal_id"),
                    "branch": out.get("branch"),
                    "details": "Patch proposal created and is applying in the background.",
                }
            if bool(getattr(result, "success", False)):
                return {
                    "ok": True,
                    "status": "applied" if out.get("applied") else "completed",
                    "spawned": "code_improver",
                    "target_file": target_file,
                    "code_patch_proposal_id": out.get("proposal_id"),
                    "branch": out.get("branch"),
                    "details": out.get("summary") or out.get("reason") or "",
                }
            return {
                "ok": False,
                "target_file": target_file,
                "error": (
                    out.get("error")
                    or getattr(result, "error", None)
                    or out.get("reason")
                    or "feature update failed"
                ),
            }
        except Exception as e:
            logger.exception("feature_handler failed")
            return {"ok": False, "error": str(e)}

    async def ingest_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
        """User approved an ingest proposal → run github_ingestor."""
        topic = str(payload.get("topic") or payload.get("query") or payload.get("idea") or "").strip()
        repo = str(payload.get("repo") or "").strip()
        try:
            from skyn3t.core.agent import TaskRequest

            ingestor = orchestrator.agents.get("github_ingestor")
            if ingestor is None:
                return {"ok": False, "error": "github_ingestor agent not registered"}
            raw_limit = payload.get("limit")
            try:
                max_files = max(1, min(int(raw_limit), 100)) if raw_limit is not None else 5
            except (TypeError, ValueError):
                return {"ok": False, "error": f"invalid ingest limit: {raw_limit!r}"}
            input_data: Dict[str, Any] = {
                "max_files": max_files,
            }
            if repo:
                input_data["mode"] = "single_repo"
                input_data["repo"] = repo
            else:
                if not topic:
                    return {"ok": False, "error": "missing topic/query for search ingest"}
                input_data["mode"] = "search"
                input_data["query"] = topic
            label = topic or (str(repo).strip() if repo else "") or "unspecified"
            req = TaskRequest(title=f"approved ingest: {label}", input_data=input_data)
            result = await ingestor.execute(req)
            ok = bool(getattr(result, "success", False))
            out = getattr(result, "output", {}) or {}
            ingested = list(out.get("ingested") or [])
            response: Dict[str, Any] = {
                "ok": ok,
                "ingested": len(ingested),
                "summary": out.get("summary", ""),
                "errors": list(out.get("errors") or []),
            }
            if ok:
                spawned_feature_id = maybe_spawn_feature_after_scout_ingest(
                    payload=payload,
                    source=str(payload.get("_proposal_source") or ""),
                    parent_proposal_id=str(payload.get("_proposal_id") or ""),
                    ingested=ingested,
                )
                if spawned_feature_id:
                    response["spawned_feature_id"] = spawned_feature_id
            return response
        except Exception as e:
            logger.exception("ingest_handler failed")
            return {"ok": False, "error": str(e)}

    async def external_learning_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Approved external-learning proposal → persist attributed governed memory."""
        memory_store = getattr(orchestrator, "memory_store", None)
        if memory_store is None:
            return {"ok": False, "error": "memory_store not initialized"}

        platform = str(payload.get("source_platform") or "external").strip() or "external"
        repo = str(payload.get("repo") or "").strip()
        repo_url = str(payload.get("repo_url") or "").strip()
        lane = str(payload.get("lane") or "").strip() or "fit"
        query = str(payload.get("query") or payload.get("topic") or "").strip()
        description = str(payload.get("description") or "").strip()
        language = str(payload.get("language") or "").strip() or "unknown"
        license_name = str(payload.get("license") or "unknown").strip() or "unknown"
        reuse_risk = str(payload.get("reuse_risk") or "unknown").strip() or "unknown"
        selection_reason = str(payload.get("selection_reason") or lane).strip() or lane
        stars = int(payload.get("stars") or 0)
        topics = [str(item).strip() for item in (payload.get("topics") or []) if str(item).strip()]
        ingestor = ExternalRepoDocIngestor(
            memory_store=memory_store,
            rag_engine=getattr(getattr(orchestrator, "_ingestor", None), "rag", None),
        )
        result = await ingestor.ingest_repo_approval(
            platform=platform,
            repo=repo,
            repo_url=repo_url,
            lane=lane,
            query=query,
            description=description,
            language=language,
            license_name=license_name,
            reuse_risk=reuse_risk,
            selection_reason=selection_reason,
            topics=topics,
            stars=stars,
        )
        pattern = None
        try:
            synthesizer = ExternalPatternSynthesizer(memory_store)
            pattern = await synthesizer.synthesize_for_doc(str(result["doc_id"]))
        except Exception:
            logger.warning("external pattern synthesis failed for %s", repo or repo_url, exc_info=True)
        return {
            "ok": True,
            "doc_id": result["doc_id"],
            "stored_as": "external_learning",
            "summary_embedding_id": result.get("summary_embedding_id"),
            "ingested": result.get("ingested_count", 0),
            "paths": list(result.get("ingested_paths") or []),
            "warnings": list(result.get("warnings") or []),
            "pattern": pattern,
        }

    async def studio_debug_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Approved studio_debug → run CodeImproverAgent on target_file with verdict+risks as rationale."""
        risks = normalize_review_risks(payload.get("risks") or [])
        verdict = payload.get("verdict") or ""
        # Check risks before resolving the target. A no-actionable-risks
        # review never needs the file to exist on disk — and the proposal
        # may target a still-to-be-generated project artifact.
        if not risks:
            return {"ok": False, "error": "review flagged no actionable risks"}
        target = _resolve_repo_file(payload.get("target_file"))
        if not target:
            return {"ok": False, "error": "invalid or missing target_file"}
        rationale = (
            f"Address Reviewer's critique on `{target}`.\n\n"
            f"Verdict: {verdict}\n\n"
            "Risks to address:\n"
            + "\n".join(f"- {r}" for r in risks if r)
            + "\n\nProduce a unified diff that resolves these risks while keeping existing structure."
        )
        try:
            improver = orchestrator.agents.get("code_improver")
            if improver is None:
                return {"ok": False, "error": "code_improver not registered"}
            req = TaskRequest(
                title="studio_debug retry",
                input_data={
                    "target_file": target,
                    "repo_root": str(REPO_ROOT),
                    "rationale": rationale,
                    "intent": "studio_debug",
                    "review_risks": risks,
                },
            )
            result = await improver.execute(req)
            ok = bool(getattr(result, "success", False))
            out = getattr(result, "output", {}) or {}
            return {"ok": ok, "spawned": "code_improver",
                    "draft_proposal_id": out.get("proposal_id"),
                    "details": out.get("summary") or out.get("reason") or ""}
        except Exception as e:
            logger.exception("studio_debug_handler failed")
            return {"ok": False, "error": str(e)}

    store.register_handler("feature", feature_handler)
    store.register_handler("ingest", ingest_handler)
    store.register_handler("external_learning", external_learning_handler)
    store.register_handler("studio_debug", studio_debug_handler)
    logger.info("cortex handlers registered: feature, ingest, external_learning, studio_debug")
