"""Apply-handlers for proposal kinds beyond tuning + code_patch.

Registered with the global ProposalStore at orchestrator boot.
Each handler is async and receives the proposal payload dict.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from skyn3t.cortex.feature_suggester import infer_feature_target_file
from skyn3t.cortex.review_utils import normalize_review_risks

logger = logging.getLogger("skyn3t.cortex.handlers")
REPO_ROOT = Path(__file__).resolve().parents[2].resolve()


def install_handlers(orchestrator) -> None:
    """Register apply-handlers for kind='feature' and kind='ingest'."""
    try:
        from skyn3t.cortex import get_store  # local import to avoid circular dependency
    except Exception:
        logger.exception("cortex store unavailable")
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
            target_file = str(payload.get("target_file") or "").strip()
            if target_file:
                target_path = (REPO_ROOT / target_file).resolve()
                try:
                    target_path.relative_to(REPO_ROOT)
                except ValueError:
                    target_file = ""
                else:
                    if not target_path.exists() or not target_path.is_file():
                        target_file = ""
            if not target_file:
                inferred_target = infer_feature_target_file(str(idea), repo_root=REPO_ROOT)
                if inferred_target:
                    target_file = inferred_target
            if not target_file:
                return {"ok": False, "error": "could not infer a starting file for this idea"}

            current_created_at = getattr(current_proposal, "created_at", None)
            if current_created_at is not None:
                older_feature = next(
                    (
                        proposal
                        for proposal in store.list()
                        if proposal.kind == "feature"
                        and proposal.status in {"approved", "applying"}
                        and proposal.id != proposal_id
                        and proposal.created_at <= current_created_at
                        and str((proposal.payload or {}).get("repo_root") or str(REPO_ROOT))
                        == str(REPO_ROOT)
                        and str((proposal.payload or {}).get("target_file") or "") == target_file
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
                    for proposal in store.list()
                    if proposal.kind == "code_patch"
                    and proposal.status in {"pending", "approved", "applying"}
                    and str((proposal.payload or {}).get("repo_root") or "") == str(REPO_ROOT)
                    and str((proposal.payload or {}).get("target_file") or "") == target_file
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
            from skyn3t.core.agent import TaskRequest
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
        repo = payload.get("repo")
        try:
            ingestor = orchestrator.agents.get("github_ingestor")
            if ingestor is None:
                return {"ok": False, "error": "github_ingestor agent not registered"}
            from skyn3t.core.agent import TaskRequest
            input_data: Dict[str, Any] = {
                "max_files": max(1, int(payload.get("limit") or 5)),
            }
            if repo:
                input_data["mode"] = "single_repo"
                input_data["repo"] = repo
            else:
                input_data["mode"] = "search"
                input_data["query"] = topic
            req = TaskRequest(title=f"approved ingest: {topic or repo or 'unspecified'}", input_data=input_data)
            result = await ingestor.execute(req)
            ok = bool(getattr(result, "success", False))
            out = getattr(result, "output", {}) or {}
            return {"ok": ok, "ingested": len(out.get("ingested", []) or []),
                    "summary": out.get("summary", "")}
        except Exception as e:
            logger.exception("ingest_handler failed")
            return {"ok": False, "error": str(e)}

    async def studio_debug_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Approved studio_debug → run CodeImproverAgent on target_file with verdict+risks as rationale."""
        target = payload.get("target_file") or ""
        risks = normalize_review_risks(payload.get("risks") or [])
        verdict = payload.get("verdict") or ""
        if not risks:
            return {"ok": True, "status": "noop", "details": "review flagged no actionable risks"}
        rationale = (
            f"Address Reviewer's critique on `{target}`.\n\n"
            f"Verdict: {verdict}\n\n"
            "Risks to address:\n" + "\n".join(f"- {r}" for r in risks if r)
            + "\n\nProduce a unified diff that resolves these risks while keeping existing structure."
        )
        try:
            improver = orchestrator.agents.get("code_improver")
            if improver is None:
                return {"ok": False, "error": "code_improver not registered"}
            from skyn3t.core.agent import TaskRequest
            req = TaskRequest(
                title="studio_debug retry",
                input_data={
                    "target_file": target,
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
    store.register_handler("studio_debug", studio_debug_handler)
    logger.info("cortex handlers registered: feature, ingest, studio_debug")
