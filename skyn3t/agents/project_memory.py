from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.cortex.review_utils import parse_review_markdown

logger = logging.getLogger("skyn3t.agents.project_memory")

# files to ingest (everything text-like under projects/<slug>/)
INGEST_EXTS = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".ts", ".tsx", ".js", ".jsx",
               ".html", ".css", ".sh", ".sql", ".toml", ".cfg"}
SKIP_NAMES  = {"__pycache__", ".git", "node_modules", ".DS_Store"}
MAX_BYTES = 200_000


class ProjectMemoryAgent(BaseAgent):
    """Ingests completed Studio projects into RAG so the swarm learns from its own work.

    Subscribes to PROJECT_COMPLETED (carried via SYSTEM_ALERT events with kind='PROJECT_COMPLETED'
    from StudioRunner) and to direct execute() invocations with input_data={'slug': ...}.
    """

    def __init__(self, name: str = "project_memory", *, event_bus: Optional[EventBus] = None,
                 rag=None, config: Optional[Dict[str, Any]] = None):
        super().__init__(name=name, agent_type="memory", provider="local",
                         event_bus=event_bus or EventBus(), config=config)
        self.rag = rag
        self.add_capability(AgentCapability(
            name="project_memory",
            description="ingest completed projects into RAG + generate project lessons",
            parameters={"slug": "str"}))
        self._wired = False

    async def initialize(self) -> None:
        if not self._wired:
            try:
                self.event_bus.subscribe(self._on_event)
                self._wired = True
            except Exception:
                logger.exception("could not subscribe")
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    def _on_event(self, event) -> None:
        try:
            payload = getattr(event, "payload", {}) or {}
            if payload.get("kind") == "PROJECT_COMPLETED" or payload.get("event") == "PROJECT_COMPLETED":
                slug = payload.get("slug")
                if slug:
                    asyncio.create_task(self._ingest_project(slug, payload))
        except Exception:
            logger.exception("on_event failed")

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        slug = (task.input_data or {}).get("slug")
        if not slug:
            return TaskResult(task_id=task.task_id, success=False, error="missing slug")
        try:
            await self.think(f"ingesting project {slug}")
        except Exception:
            logger.debug("think() failed during ingest start", exc_info=True)
        result = await self._ingest_project(slug, task.input_data or {})
        return TaskResult(task_id=task.task_id, success=True, output=result)

    async def _ingest_project(self, slug: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        root = Path("projects") / slug
        if not root.exists():
            return {"ingested": 0, "skipped": 0, "reason": "project dir not found"}

        files_ingested: List[Dict[str, Any]] = []
        files_skipped: List[Dict[str, str]] = []

        # 1. ingest each artifact
        for p in sorted(root.rglob("*")):
            if any(part in SKIP_NAMES for part in p.parts):
                continue
            if not p.is_file():
                continue
            if p.suffix.lower() not in INGEST_EXTS:
                files_skipped.append({"path": str(p.relative_to(root)), "reason": "extension"})
                continue
            try:
                size = p.stat().st_size
                if size > MAX_BYTES:
                    files_skipped.append({"path": str(p.relative_to(root)), "reason": "too large"})
                    continue
                text = p.read_text(encoding="utf-8")
            except Exception as e:
                files_skipped.append({"path": str(p.relative_to(root)), "reason": str(e)[:80]})
                continue
            metadata = {
                "source": "project_memory",
                "slug": slug,
                "template": meta.get("template", ""),
                "title": meta.get("title", ""),
                "path": str(p.relative_to(root)),
                "kind": p.stem,                       # brainstorm, architecture, brand, review, ...
                "ext": p.suffix.lstrip("."),
                "ingested_at": datetime.now(timezone.utc).isoformat(),
            }
            embedding_id = await self._add_to_rag(content=text, metadata=metadata)
            files_ingested.append({"path": metadata["path"], "embedding_id": embedding_id, "bytes": size})
            try:
                await self.think(f"ingested {metadata['path']}")
            except Exception:
                logger.debug("think() failed during per-file ingest", exc_info=True)

        # 2. write a project-level summary lesson
        summary = self._build_summary(slug, root, meta, files_ingested)
        summary_id = await self._add_to_rag(
            content=summary,
            metadata={"source": "project_memory", "kind": "project_summary",
                      "slug": slug, "template": meta.get("template", ""),
                      "title": meta.get("title", "")},
        )

        # 3. emit a learning event for the dashboard
        try:
            from skyn3t.core.events import Event, EventType
            self.event_bus.publish(Event(
                event_type=EventType.AGENT_LEARNING,
                source=self.name,
                payload={"lesson": f"ingested project {slug}: {len(files_ingested)} artifacts",
                         "scope": "project_memory",
                         "slug": slug, "summary_id": summary_id},
            ))
        except Exception:
            logger.debug("learning event publish failed", exc_info=True)

        try:
            await self.share_learning(
                f"project_memory: ingested {len(files_ingested)} files from {slug}",
                scope="rag")
        except Exception:
            logger.debug("share_learning(rag) failed", exc_info=True)

        return {
            "slug": slug,
            "ingested": len(files_ingested),
            "skipped": len(files_skipped),
            "summary_id": summary_id,
            "files": files_ingested[:20],
            "summary": (
                f"Ingested {len(files_ingested)} artifacts from {slug} "
                f"(skipped {len(files_skipped)})"
            ),
        }

    def _build_summary(self, slug: str, root: Path, meta: Dict[str, Any],
                        files_ingested: List[Dict[str, Any]]) -> str:
        # pull a brief from project.json if present
        brief = ""
        proj_json = root / "project.json"
        if proj_json.exists():
            try:
                import json
                d = json.loads(proj_json.read_text())
                brief = d.get("brief") or ""
            except Exception:
                logger.debug("project.json parse failed for %s", slug, exc_info=True)

        # pull review verdict if review.md exists
        verdict = ""
        review = root / "review.md"
        if review.exists():
            try:
                verdict, _ = parse_review_markdown(review.read_text(encoding="utf-8"))
            except Exception:
                logger.debug("review.md scan failed for %s", slug, exc_info=True)

        # pull primary direction from brainstorm.md if present
        direction = ""
        brain = root / "brainstorm.md"
        if brain.exists():
            try:
                t = brain.read_text(encoding="utf-8")
                grab = False
                for line in t.splitlines():
                    if line.startswith("## ") and "direction" in line.lower():
                        grab = True
                        continue
                    if grab and line.strip().startswith("**"):
                        direction = line.strip().strip("*").strip()
                        break
            except Exception:
                logger.debug("brainstorm.md scan failed for %s", slug, exc_info=True)

        artifacts_summary = "\n".join(f"- {f['path']}" for f in files_ingested[:30])
        return (
            f"# Project: {meta.get('title') or slug}\n"
            f"_slug_: {slug}\n"
            f"_template_: {meta.get('template','')}\n"
            f"\n"
            f"## Brief\n{brief}\n\n"
            f"## Direction\n{direction}\n\n"
            f"## Verdict\n{verdict}\n\n"
            f"## Artifacts ({len(files_ingested)})\n{artifacts_summary}\n"
        )

    async def _add_to_rag(self, *, content: str, metadata: Dict[str, Any]) -> Optional[str]:
        if not self.rag:
            return None
        try:
            if hasattr(self.rag, "add_knowledge_one"):
                embedding_id = await self.rag.add_knowledge_one(
                    content=content,
                    metadata=metadata,
                )
                return embedding_id if isinstance(embedding_id, str) else None
            if hasattr(self.rag, "add_knowledge"):
                ids = await self.rag.add_knowledge(content=content, metadata=metadata)
                return str(ids[0]) if isinstance(ids, list) and ids else None
        except Exception:
            logger.exception("rag add failed")
        return None
