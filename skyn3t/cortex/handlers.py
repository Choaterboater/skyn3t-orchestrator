"""Apply-handlers for proposal kinds beyond tuning + code_patch.

Registered with the global ProposalStore at orchestrator boot.
Each handler is async and receives the proposal payload dict.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from skyn3t.cortex.review_utils import normalize_review_risks

logger = logging.getLogger("skyn3t.cortex.handlers")


def install_handlers(orchestrator) -> None:
    """Register apply-handlers for kind='feature' and kind='ingest'."""
    try:
        from skyn3t.cortex import get_store
    except Exception:
        logger.exception("cortex store unavailable")
        return
    store = get_store()

    async def feature_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
        """User approved a feature idea → file a code-patch draft proposal."""
        idea = payload.get("idea") or payload.get("summary") or "improvement"
        try:
            improver = orchestrator.agents.get("code_improver")
            if improver is None:
                return {"ok": False, "error": "code_improver agent not registered"}
            from skyn3t.core.agent import TaskRequest
            req = TaskRequest(
                title="draft from feature proposal",
                input_data={
                    "rationale": str(idea)[:200],
                    "intent": "feature_implementation",
                    "source": "cortex.feature",
                },
            )
            result = await improver.execute(req)
            ok = bool(getattr(result, "success", False))
            out = getattr(result, "output", {}) or {}
            return {"ok": ok, "spawned": "code_improver",
                    "draft_proposal_id": out.get("proposal_id"),
                    "details": out.get("summary") or out.get("reason") or ""}
        except Exception as e:
            logger.exception("feature_handler failed")
            return {"ok": False, "error": str(e)}

    async def ingest_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
        """User approved an ingest proposal → run github_ingestor."""
        topic = payload.get("topic") or payload.get("repo") or payload.get("query") or ""
        repo = payload.get("repo")
        try:
            ingestor = orchestrator.agents.get("github_ingestor")
            if ingestor is None:
                return {"ok": False, "error": "github_ingestor agent not registered"}
            from skyn3t.core.agent import TaskRequest
            input_data: Dict[str, Any] = {
                "max_files": int(payload.get("limit", 5)),
            }
            if repo:
                input_data["mode"] = "single_repo"
                input_data["repo"] = repo
            else:
                input_data["mode"] = "search"
                input_data["query"] = topic
            req = TaskRequest(title=f"approved ingest: {topic or repo}", input_data=input_data)
            result = await ingestor.execute(req)
            ok = bool(getattr(result, "success", False))
            out = getattr(result, "output", {}) or {}
            return {"ok": ok, "ingested": len(out.get("ingested", [])),
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
            return {"ok": False, "error": "review flagged no actionable risks"}
        rationale = (
            f"Address Reviewer's critique on `{target}`.\n\n"
            f"Verdict: {verdict}\n\n"
            + "Risks to address:\n" + "\n".join(f"- {r}" for r in risks)
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
