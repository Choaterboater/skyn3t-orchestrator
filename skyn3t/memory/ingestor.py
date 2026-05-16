"""Experience Ingestor — feeds task outcomes, lessons, and insights into RAG.

Every experience the swarm has gets automatically converted into knowledge
and stored in the vector database. This means agents can semantically recall
"how did we solve this before?" just by querying RAG.
"""

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.rag.rag_engine import RAGEngine

logger = logging.getLogger("skyn3t.memory.ingestor")


def _log_task_exception(fut: "asyncio.Future") -> None:
    exc = fut.exception()
    if exc is not None:
        logger.error("ingest task failed", exc_info=exc)


class ExperienceIngestor:
    """Automatically ingest experiences into the RAG vector store.

    Listens to task completion/failure events and reflection knowledge updates,
    formats them as documents, and adds them to the vector DB for semantic recall.
    """

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        rag_engine: Optional[RAGEngine] = None,
        memory_store: Optional[Any] = None,
        seen_hashes_path: Optional[Path] = None,
    ):
        self.event_bus = event_bus
        self.rag = rag_engine or RAGEngine()
        self._memory = memory_store
        self._running = False
        # Persist seen hashes to disk so a restart doesn't re-ingest every
        # task experience the agent has ever processed (which both wastes
        # vector-DB writes and pollutes search results with duplicates).
        self._seen_hashes_path = (
            seen_hashes_path or Path("data/.ingestor_seen_hashes.json")
        )
        self._seen_hashes: set[str] = self._load_seen_hashes()
        # Track when we last persisted so we batch writes (every 32 adds).
        self._unflushed_adds = 0

        if event_bus:
            event_bus.subscribe(self._on_task_completed, EventType.TASK_COMPLETED)
            event_bus.subscribe(self._on_task_failed, EventType.TASK_FAILED)
            event_bus.subscribe(self._on_knowledge_updated, EventType.KNOWLEDGE_UPDATED)
            # Studio publishes project events via SYSTEM_ALERT with a
            # `kind` field on the payload (see StudioRunner._publish).
            # Without this subscription, every canary failure has been
            # invisible to the experience store — we discovered this
            # after 20+ canary runs that taught the system nothing.
            event_bus.subscribe(self._on_system_alert, EventType.SYSTEM_ALERT)

    async def initialize(self) -> None:
        """Initialize the RAG engine."""
        await self.rag.initialize()
        self._running = True

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_task_completed(self, event: Event) -> None:
        """Ingest a successful task outcome."""
        if not self._running:
            return
        payload = event.payload
        t = asyncio.create_task(self.ingest_task_experience(
            task_id=payload.get("task_id", ""),
            agent_name=event.source,
            success=True,
            output=payload.get("output", {}),
            execution_time_ms=payload.get("execution_time_ms", 0.0),
        ))
        t.add_done_callback(_log_task_exception)

    def _on_task_failed(self, event: Event) -> None:
        """Ingest a failed task outcome with error analysis."""
        if not self._running:
            return
        payload = event.payload
        t = asyncio.create_task(self.ingest_task_experience(
            task_id=payload.get("task_id", ""),
            agent_name=event.source,
            success=False,
            output={},
            error=payload.get("error", "unknown"),
            execution_time_ms=payload.get("execution_time_ms", 0.0),
        ))
        t.add_done_callback(_log_task_exception)

    def _on_knowledge_updated(self, event: Event) -> None:
        """Ingest reflection-generated knowledge."""
        if not self._running:
            return
        payload = event.payload
        t = asyncio.create_task(self.ingest_lesson(
            agent=payload.get("agent", event.source),
            success=payload.get("success", True),
            patterns=payload.get("patterns", []),
            suggestions=payload.get("suggestions", []),
            task_id=payload.get("task_id", ""),
        ))
        t.add_done_callback(_log_task_exception)

    # Project events the studio runner emits via SYSTEM_ALERT.
    # Each of these triggers an experience-doc write so the next
    # canary's RAG recall returns a concrete failure example.
    _PROJECT_EVENT_KINDS = {
        "PROJECT_STAGE_FAILED",
        "PROJECT_COMPLETED",
        "CONTRACT_VERIFIER_BLOCKERS",
        "CONSISTENCY_REVIEW_BLOCKERS",
    }

    def _on_system_alert(self, event: Event) -> None:
        """Catch studio project events that ride on SYSTEM_ALERT.

        Studio doesn't have its own EventType members; it stuffs the
        real event name into payload["kind"]. We filter and route the
        ones that carry useful learning signal into the experience store.
        """
        if not self._running:
            return
        payload = event.payload or {}
        kind = payload.get("kind")
        if kind not in self._PROJECT_EVENT_KINDS:
            return
        t = asyncio.create_task(self.ingest_project_event(kind, payload))
        t.add_done_callback(_log_task_exception)

    # ------------------------------------------------------------------
    # Ingestion methods
    # ------------------------------------------------------------------

    async def ingest_task_experience(
        self,
        task_id: str,
        agent_name: str,
        success: bool,
        output: Dict[str, Any],
        execution_time_ms: float = 0.0,
        error: Optional[str] = None,
    ) -> Optional[str]:
        """Ingest a single task outcome as a knowledge document."""
        status = "SUCCESS" if success else "FAILURE"
        content = self._format_task_experience(
            task_id, agent_name, status, output, execution_time_ms, error
        )
        content_hash = self._hash(content)

        # Deduplication check
        if await self._is_duplicate(content_hash):
            return None

        title = f"Task {task_id} — {status} ({agent_name})"
        doc_type = "experience"

        metadata = {
            "task_id": task_id,
            "agent_name": agent_name,
            "success": success,
            "execution_time_ms": execution_time_ms,
            "content_hash": content_hash,
            "error": error,
        }
        embedding_id = await self.rag.add_knowledge_one(
            content=content,
            title=title,
            source=agent_name,
            doc_type=doc_type,
            metadata=metadata,
        )
        if embedding_id:
            self._record_seen(content_hash)
            await self._persist_doc(title, content, agent_name, doc_type, metadata, embedding_id)
        return embedding_id

    async def ingest_lesson(
        self,
        agent: str,
        success: bool,
        patterns: list,
        suggestions: list,
        task_id: str = "",
    ) -> Optional[str]:
        """Ingest a reflection lesson as a knowledge document."""
        content = self._format_lesson(agent, success, patterns, suggestions, task_id)
        content_hash = self._hash(content)

        if await self._is_duplicate(content_hash):
            return None

        title = f"Lesson from {agent} — {'success' if success else 'failure'}"
        metadata = {
            "agent": agent,
            "success": success,
            "patterns": patterns,
            "task_id": task_id,
            "content_hash": content_hash,
        }
        embedding_id = await self.rag.add_knowledge_one(
            content=content,
            title=title,
            source="reflection",
            doc_type="lesson",
            metadata=metadata,
        )
        if embedding_id:
            self._record_seen(content_hash)
            await self._persist_doc(title, content, "reflection", "lesson", metadata, embedding_id)
        return embedding_id

    async def ingest_insight(
        self,
        agent_name: str,
        insight: str,
        capability: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Ingest an agent insight as a knowledge document."""
        content = f"Agent: {agent_name}\n"
        if capability:
            content += f"Capability: {capability}\n"
        content += f"Insight: {insight}\n"
        if context:
            content += f"Context: {context}\n"

        content_hash = self._hash(content)
        if await self._is_duplicate(content_hash):
            return None

        title = f"Insight from {agent_name}"
        metadata = {
            "agent_name": agent_name,
            "capability": capability,
            "content_hash": content_hash,
        }
        embedding_id = await self.rag.add_knowledge_one(
            content=content,
            title=title,
            source=agent_name,
            doc_type="insight",
            metadata=metadata,
        )
        if embedding_id:
            self._record_seen(content_hash)
            await self._persist_doc(title, content, agent_name, "insight", metadata, embedding_id)
        return embedding_id

    async def ingest_failure_pattern(
        self,
        pattern_name: str,
        description: str,
        suggested_fix: str,
        affected_agents: list,
    ) -> Optional[str]:
        """Ingest a failure pattern and its fix as a knowledge document."""
        content = (
            f"Failure Pattern: {pattern_name}\n"
            f"Description: {description}\n"
            f"Suggested Fix: {suggested_fix}\n"
            f"Affected Agents: {', '.join(affected_agents)}\n"
        )
        content_hash = self._hash(content)
        if await self._is_duplicate(content_hash):
            return None

        title = f"Pattern: {pattern_name}"
        metadata = {
            "pattern_name": pattern_name,
            "affected_agents": affected_agents,
            "content_hash": content_hash,
        }
        embedding_id = await self.rag.add_knowledge_one(
            content=content,
            title=title,
            source="reflection",
            doc_type="pattern",
            metadata=metadata,
        )
        if embedding_id:
            self._record_seen(content_hash)
            await self._persist_doc(title, content, "reflection", "pattern", metadata, embedding_id)
        return embedding_id

    async def ingest_project_event(
        self,
        kind: str,
        payload: Dict[str, Any],
    ) -> Optional[str]:
        """Ingest a studio project event (failure/blocker) as an experience.

        Doc type ``experience`` with ``success=False`` so CodeAgent's
        existing RAG query (filters on doc_type=experience + success=False)
        retrieves these directly. The tag fields (``stack``, ``feature_tags``)
        let future queries narrow further when the brief mentions specific
        features the dropped run was building.
        """
        slug = payload.get("project_slug") or payload.get("slug") or "?"
        stage = payload.get("stage") or "?"
        message = payload.get("message") or ""
        error = payload.get("error") or ""
        verdict = payload.get("verdict") or ""

        # Build a structured lesson body. Keep it terse — RAG hits return
        # the content directly into a prompt; we want < 600 chars after
        # the agent's truncation step.
        lines = [
            f"Project Event: {kind}",
            f"Slug: {slug}",
            f"Stage: {stage}",
        ]
        if verdict:
            lines.append(f"Verdict: {verdict}")
        if message:
            lines.append(f"Message: {message[:300]}")
        if error:
            lines.append(f"Error: {error[:300]}")

        # Pull contract-verifier blockers out if present — these are the
        # most actionable signal we can record.
        findings = payload.get("findings") or []
        if findings:
            lines.append("Blockers:")
            for f in findings[:5]:
                cat = (f.get("category") if isinstance(f, dict) else "") or ""
                file = (f.get("file") if isinstance(f, dict) else "") or ""
                msg = (f.get("message") if isinstance(f, dict) else "") or ""
                lines.append(f"  - [{cat}] {file}: {msg[:140]}")

        content = "\n".join(lines)
        content_hash = self._hash(content)
        if await self._is_duplicate(content_hash):
            return None

        # Tag with whatever signal the runner sent through. Frontends
        # querying RAG can match on these for stack-specific recall.
        stack = payload.get("stack") or ""
        feature_tags = payload.get("feature_tags") or []
        if not isinstance(feature_tags, list):
            feature_tags = []

        title = f"Project {kind} — {slug}"
        metadata = {
            "kind": kind,
            "project_slug": slug,
            "stage": stage,
            "stack": stack,
            "feature_tags": ", ".join(feature_tags) if feature_tags else "",
            "success": False,
            "content_hash": content_hash,
        }
        embedding_id = await self.rag.add_knowledge_one(
            content=content,
            title=title,
            source="studio",
            doc_type="experience",
            metadata=metadata,
        )
        if embedding_id:
            self._record_seen(content_hash)
            await self._persist_doc(title, content, "studio", "experience", metadata, embedding_id)
        return embedding_id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_task_experience(
        self,
        task_id: str,
        agent_name: str,
        status: str,
        output: Dict[str, Any],
        execution_time_ms: float,
        error: Optional[str],
    ) -> str:
        lines = [
            f"Task ID: {task_id}",
            f"Agent: {agent_name}",
            f"Status: {status}",
            f"Execution Time: {execution_time_ms:.0f}ms",
        ]
        if error:
            lines.append(f"Error: {error}")
        if output:
            output_str = str(output)
            lines.append(f"Output: {output_str[:800]}")
        return "\n".join(lines)

    def _format_lesson(
        self,
        agent: str,
        success: bool,
        patterns: list,
        suggestions: list,
        task_id: str,
    ) -> str:
        lines = [
            f"Agent: {agent}",
            f"Outcome: {'success' if success else 'failure'}",
            f"Task ID: {task_id}",
        ]
        if patterns:
            lines.append(f"Patterns Detected: {', '.join(patterns)}")
        if suggestions:
            lines.append("Suggestions:")
            for s in suggestions:
                lines.append(f"  - {s}")
        return "\n".join(lines)

    def _hash(self, content: str) -> str:
        """Create a content hash for deduplication."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def _load_seen_hashes(self) -> set[str]:
        """Restore the seen-hashes set from disk, if any."""
        try:
            if self._seen_hashes_path.exists():
                data = json.loads(self._seen_hashes_path.read_text())
                if isinstance(data, list):
                    return set(str(h) for h in data)
        except Exception:
            logger.exception("seen_hashes load failed")
        return set()

    def _persist_seen_hashes(self) -> None:
        """Atomically write the seen-hashes set to disk."""
        try:
            self._seen_hashes_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._seen_hashes_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(sorted(self._seen_hashes)))
            os.replace(tmp, self._seen_hashes_path)
            self._unflushed_adds = 0
        except Exception:
            logger.exception("seen_hashes persist failed")

    def _record_seen(self, content_hash: str) -> None:
        """Add a hash to the seen set; flush every 32 adds."""
        self._seen_hashes.add(content_hash)
        self._unflushed_adds += 1
        if self._unflushed_adds >= 32:
            self._persist_seen_hashes()

    async def _is_duplicate(self, content_hash: str) -> bool:
        """Check if content with this hash was already ingested this session."""
        # In-memory dedup; persistence across restarts is a TODO.
        return content_hash in self._seen_hashes

    async def _persist_doc(
        self,
        title: str,
        content: str,
        source: str,
        doc_type: str,
        metadata: Dict[str, Any],
        embedding_id: str,
    ) -> None:
        """Persist a knowledge doc to MemoryStore alongside RAG, if available."""
        if self._memory is None:
            return
        try:
            saver = getattr(self._memory, "save_knowledge_doc", None)
            if saver is not None:
                await saver(
                    title=title,
                    content=content,
                    source=source,
                    doc_type=doc_type,
                    embedding_id=embedding_id,
                    meta=metadata,
                )
            else:
                save_lesson = getattr(self._memory, "save_lesson", None)
                if save_lesson is not None:
                    await save_lesson(
                        title=title,
                        content=content,
                        source=source,
                        doc_type=doc_type,
                        meta=metadata,
                        embedding_id=embedding_id,
                    )
        except Exception as e:
            logger.warning("failed to persist knowledge doc to MemoryStore: %s", e)
