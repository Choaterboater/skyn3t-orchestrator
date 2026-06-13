"""Persistent memory store for SkyN3t — the swarm's long-term memory."""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skyn3t.core.models import (
    Agent as AgentModel,
)
from skyn3t.core.models import (
    AgentStatus,
    ExperienceIndex,
    KnowledgeDocument,
    SystemLog,
    TaskStatus,
)
from skyn3t.core.models import (
    Message as MessageModel,
)
from skyn3t.core.models import (
    Task as TaskModel,
)
from skyn3t.core.models import (
    User as UserModel,
)
from skyn3t.memory.database import get_session_maker

_KEEP = object()


class MemoryStore:
    """Persistent store for agent states, tasks, messages, lessons, and logs.

    This is the swarm's long-term memory. Everything that happens gets recorded
    here so agents can recall past experiences across restarts.
    """

    def __init__(self):
        self._session_maker = get_session_maker()

    async def _session(self) -> AsyncSession:
        """Get a new database session."""
        session: AsyncSession = self._session_maker()
        return session

    # ------------------------------------------------------------------
    # Agent state
    # ------------------------------------------------------------------

    async def save_agent(self, agent_id: str, name: str, agent_type: str,
                         provider: str, status: str, capabilities: List[str],
                         config: Dict[str, Any], meta: Dict[str, Any],
                         role: Optional[str] = None,
                         reports_to: Optional[str] = None,
                         lifecycle: Optional[str] = None) -> None:
        """Upsert an agent record."""
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(AgentModel).where(AgentModel.name == name)
                )
                existing = result.scalar_one_or_none()
                if existing:
                    existing.agent_type = agent_type
                    existing.provider = provider
                    existing.status = AgentStatus(status) if status in [s.value for s in AgentStatus] else AgentStatus.IDLE
                    existing.role = role
                    existing.reports_to = reports_to
                    existing.lifecycle = lifecycle
                    existing.capabilities = capabilities
                    existing.config = config
                    existing.meta = meta
                    existing.last_heartbeat = datetime.now(timezone.utc)
                else:
                    session.add(AgentModel(
                        id=agent_id,
                        name=name,
                        agent_type=agent_type,
                        provider=provider,
                        status=AgentStatus(status) if status in [s.value for s in AgentStatus] else AgentStatus.IDLE,
                        role=role,
                        reports_to=reports_to,
                        lifecycle=lifecycle,
                        capabilities=capabilities,
                        config=config,
                        meta=meta,
                        last_heartbeat=datetime.now(timezone.utc),
                    ))

    async def get_agent(self, name: str) -> Optional[Dict[str, Any]]:
        """Get an agent by name."""
        async with await self._session() as session:
            result = await session.execute(
                select(AgentModel).where(AgentModel.name == name)
            )
            agent = result.scalar_one_or_none()
            if agent:
                return {
                    "id": agent.id,
                    "name": agent.name,
                    "agent_type": agent.agent_type,
                    "provider": agent.provider,
                    "status": agent.status.value,
                    "role": agent.role,
                    "reports_to": agent.reports_to,
                    "lifecycle": agent.lifecycle,
                    "capabilities": agent.capabilities,
                    "config": agent.config,
                    "meta": agent.meta,
                    "last_heartbeat": agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
                    "created_at": agent.created_at.isoformat(),
                }
            return None

    async def list_agents(self) -> List[Dict[str, Any]]:
        """List all agents."""
        async with await self._session() as session:
            result = await session.execute(select(AgentModel))
            return [
                {
                    "id": a.id,
                    "name": a.name,
                    "agent_type": a.agent_type,
                    "provider": a.provider,
                    "status": a.status.value,
                    "role": a.role,
                    "reports_to": a.reports_to,
                    "lifecycle": a.lifecycle,
                    "capabilities": a.capabilities,
                    "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
                }
                for a in result.scalars().all()
            ]

    # ------------------------------------------------------------------
    # Task results
    # ------------------------------------------------------------------

    async def save_task(self, task_id: str, title: str, description: str,
                        status: str, priority: int, agent_id: Optional[str],
                        agent_name: Optional[str], parent_task_id: Optional[str],
                        input_data: Dict[str, Any], output_data: Dict[str, Any],
                        error_message: Optional[str], retry_count: int,
                        max_retries: int, started_at: Optional[datetime],
                        completed_at: Optional[datetime],
                        session_id: Optional[str] = None) -> None:
        """Upsert a task record."""
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(TaskModel).where(TaskModel.id == task_id)
                )
                existing = result.scalar_one_or_none()
                if existing:
                    existing.title = title
                    existing.description = description
                    existing.status = TaskStatus(status) if status in [s.value for s in TaskStatus] else TaskStatus.PENDING
                    existing.priority = priority
                    existing.agent_id = agent_id
                    existing.parent_task_id = parent_task_id
                    existing.input_data = input_data
                    existing.output_data = output_data
                    existing.error_message = error_message
                    existing.retry_count = retry_count
                    existing.max_retries = max_retries
                    existing.started_at = started_at
                    existing.completed_at = completed_at
                    if session_id:
                        existing_meta = existing.input_data.get("_meta", {})
                        existing_meta["session_id"] = session_id
                        existing.input_data = {**existing.input_data, "_meta": existing_meta}
                else:
                    task_input = dict(input_data)
                    if session_id:
                        task_input["_meta"] = {**(task_input.get("_meta") or {}), "session_id": session_id}
                    session.add(TaskModel(
                        id=task_id,
                        title=title,
                        description=description,
                        status=TaskStatus(status) if status in [s.value for s in TaskStatus] else TaskStatus.PENDING,
                        priority=priority,
                        agent_id=agent_id,
                        parent_task_id=parent_task_id,
                        input_data=task_input,
                        output_data=output_data,
                        error_message=error_message,
                        retry_count=retry_count,
                        max_retries=max_retries,
                        started_at=started_at,
                        completed_at=completed_at,
                    ))

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get a task by ID."""
        async with await self._session() as session:
            result = await session.execute(
                select(TaskModel).where(TaskModel.id == task_id)
            )
            task = result.scalar_one_or_none()
            if task:
                return self._task_to_dict(task)
            return None

    async def get_task_history(self, agent_name: Optional[str] = None,
                               capability: Optional[str] = None,
                               status: Optional[str] = None,
                               limit: int = 100,
                               offset: int = 0) -> List[Dict[str, Any]]:
        """Get recent task history with optional filters."""
        async with await self._session() as session:
            query = select(TaskModel).order_by(desc(TaskModel.created_at))
            if status:
                query = query.where(TaskModel.status == TaskStatus(status))
            query = query.limit(limit).offset(offset)
            result = await session.execute(query)
            tasks = result.scalars().all()

            # Filter by agent_name and capability in Python since agent_name
            # is stored in output_data meta, not a column
            out = []
            for t in tasks:
                d = self._task_to_dict(t)
                if agent_name and d.get("agent_name") != agent_name:
                    continue
                if capability:
                    caps = d.get("input_data", {}).get("_meta", {}).get("capabilities", [])
                    if capability not in caps:
                        continue
                out.append(d)
            return out

    async def get_recent_context(self, session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent tasks and messages for a session."""
        async with await self._session() as session:
            # Try to filter session_id in SQL via json_extract; fall back to Python
            # filter (bounded scan) if the dialect doesn't support it.
            tasks: List[Dict[str, Any]] = []
            try:
                sql_result = await session.execute(
                    select(TaskModel)
                    .where(
                        func.json_extract(TaskModel.input_data, "$._meta.session_id") == session_id
                    )
                    .order_by(desc(TaskModel.created_at))
                    .limit(limit)
                )
                tasks = [self._task_to_dict(t) for t in sql_result.scalars().all()]
            except Exception:
                task_fallback_result = await session.execute(
                    select(TaskModel).order_by(desc(TaskModel.created_at)).limit(1000)
                )
                for t in task_fallback_result.scalars().all():
                    sid = t.input_data.get("_meta", {}).get("session_id") if t.input_data else None
                    if sid == session_id:
                        tasks.append(self._task_to_dict(t))
                        if len(tasks) >= limit:
                            break

            # Also get messages. ``context.contains()`` works on
            # PostgreSQL JSONB but is fragile on SQLite's JSON1 backend
            # — fall back to a bounded Python-side scan whenever the
            # SQL-level filter errors or yields nothing.
            messages: List[Dict[str, Any]] = []
            try:
                msg_result = await session.execute(
                    select(MessageModel)
                    .where(MessageModel.context.contains({"session_id": session_id}))
                    .order_by(desc(MessageModel.created_at))
                    .limit(limit)
                )
                messages = [
                    {
                        "id": m.id,
                        "source_agent": m.source_agent,
                        "target_agent": m.target_agent,
                        "content": m.content,
                        "message_type": m.message_type,
                        "context": m.context,
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in msg_result.scalars().all()
                ]
            except Exception:
                messages = []
            if not messages:
                message_fallback_result = await session.execute(
                    select(MessageModel)
                    .order_by(desc(MessageModel.created_at))
                    .limit(1000)
                )
                for m in message_fallback_result.scalars().all():
                    ctx = m.context or {}
                    if isinstance(ctx, dict) and ctx.get("session_id") == session_id:
                        messages.append({
                            "id": m.id,
                            "source_agent": m.source_agent,
                            "target_agent": m.target_agent,
                            "content": m.content,
                            "message_type": m.message_type,
                            "context": m.context,
                            "created_at": m.created_at.isoformat(),
                        })
                        if len(messages) >= limit:
                            break

            # Merge and sort by created_at
            combined = [{"type": "task", **t} for t in tasks[:limit]]
            combined += [{"type": "message", **m} for m in messages]
            combined.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return combined[:limit]

    def _task_to_dict(self, task: TaskModel) -> Dict[str, Any]:
        return {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "priority": task.priority,
            "agent_id": task.agent_id,
            "agent_name": task.output_data.get("_meta", {}).get("agent_name") if task.output_data else None,
            "parent_task_id": task.parent_task_id,
            "input_data": task.input_data,
            "output_data": task.output_data,
            "error_message": task.error_message,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "created_at": task.created_at.isoformat(),
        }

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def save_message(self, source_agent: str, target_agent: Optional[str],
                           content: str, message_type: str = "chat",
                           context: Optional[Dict[str, Any]] = None,
                           session_id: Optional[str] = None) -> str:
        """Save an inter-agent message.

        ``session_id`` is hoisted into the stored ``context`` so
        ``get_recent_context(session_id=...)`` can find the message
        later. Without this hoist, callers that pass only
        ``context=None`` (or context without a session_id) make
        session-scoped recall return zero rows.
        """
        from uuid import uuid4
        msg_id = str(uuid4())
        merged_context: Dict[str, Any] = dict(context or {})
        if session_id and "session_id" not in merged_context:
            merged_context["session_id"] = session_id
        async with await self._session() as session:
            async with session.begin():
                session.add(MessageModel(
                    id=msg_id,
                    source_agent=source_agent,
                    target_agent=target_agent,
                    content=content,
                    message_type=message_type,
                    context=merged_context,
                ))
        return msg_id

    async def get_messages_between(self, agent_a: str, agent_b: str,
                                    limit: int = 50) -> List[Dict[str, Any]]:
        """Get messages between two agents."""
        async with await self._session() as session:
            result = await session.execute(
                select(MessageModel)
                .where(
                    ((MessageModel.source_agent == agent_a) & (MessageModel.target_agent == agent_b))
                    | ((MessageModel.source_agent == agent_b) & (MessageModel.target_agent == agent_a))
                )
                .order_by(desc(MessageModel.created_at))
                .limit(limit)
            )
            return [
                {
                    "id": m.id,
                    "source_agent": m.source_agent,
                    "target_agent": m.target_agent,
                    "content": m.content,
                    "message_type": m.message_type,
                    "created_at": m.created_at.isoformat(),
                }
                for m in result.scalars().all()
            ]

    # ------------------------------------------------------------------
    # Lessons / Knowledge
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_knowledge_doc(
        doc: KnowledgeDocument,
        *,
        preview_only: bool = True,
    ) -> Dict[str, Any]:
        return {
            "id": doc.id,
            "title": doc.title,
            "content": doc.content[:500] if preview_only else doc.content,
            "source": doc.source,
            "doc_type": doc.doc_type,
            "meta": doc.meta,
            "embedding_id": doc.embedding_id,
            "created_at": doc.created_at.isoformat(),
        }

    async def save_lesson(self, title: str, content: str, source: str,
                          doc_type: str = "lesson", meta: Optional[Dict[str, Any]] = None,
                          embedding_id: Optional[str] = None) -> str:
        """Save a lesson or knowledge document."""
        from uuid import uuid4
        doc_id = str(uuid4())
        async with await self._session() as session:
            async with session.begin():
                session.add(KnowledgeDocument(
                    id=doc_id,
                    title=title,
                    content=content,
                    source=source,
                    doc_type=doc_type,
                    meta=meta or {},
                    embedding_id=embedding_id,
                ))
        return doc_id

    async def save_knowledge_doc(
        self,
        title: str,
        content: str,
        source: str,
        doc_type: str = "lesson",
        embedding_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Thin shim used by ExperienceIngestor; delegates to save_lesson."""
        return await self.save_lesson(
            title=title,
            content=content,
            source=source,
            doc_type=doc_type,
            meta=meta,
            embedding_id=embedding_id,
        )

    async def get_lessons(self, doc_type: Optional[str] = None,
                          source: Optional[str] = None,
                          limit: int = 50) -> List[Dict[str, Any]]:
        """Get lessons/knowledge documents."""
        async with await self._session() as session:
            query = select(KnowledgeDocument).order_by(desc(KnowledgeDocument.created_at))
            if doc_type:
                query = query.where(KnowledgeDocument.doc_type == doc_type)
            if source:
                query = query.where(KnowledgeDocument.source == source)
            query = query.limit(limit)
            result = await session.execute(query)
            return [self._serialize_knowledge_doc(d) for d in result.scalars().all()]

    async def get_knowledge_doc(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one knowledge document by id."""
        async with await self._session() as session:
            result = await session.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
            )
            doc = result.scalar_one_or_none()
            if doc is None:
                return None
            return self._serialize_knowledge_doc(doc, preview_only=False)

    async def list_knowledge_drafts(
        self,
        *,
        review_status: str = "draft",
        doc_type: Optional[str] = None,
        limit: int = 50,
        preview_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """List knowledge documents by review status.

        Uses SQLite's JSON extract when available and falls back to Python-side
        filtering if the backend cannot evaluate the JSON expression.
        """
        async with await self._session() as session:
            query = select(KnowledgeDocument).order_by(desc(KnowledgeDocument.created_at))
            if doc_type:
                query = query.where(KnowledgeDocument.doc_type == doc_type)
            try:
                query = query.where(
                    func.json_extract(KnowledgeDocument.meta, "$.review_status") == review_status
                ).limit(limit)
                result = await session.execute(query)
                docs = result.scalars().all()
            except Exception:
                result = await session.execute(
                    select(KnowledgeDocument)
                    .order_by(desc(KnowledgeDocument.created_at))
                    .limit(max(limit * 5, 50))
                )
                docs = [
                    doc for doc in result.scalars().all()
                    if (doc.meta or {}).get("review_status") == review_status
                    and (doc_type is None or doc.doc_type == doc_type)
                ][:limit]
            return [
                self._serialize_knowledge_doc(doc, preview_only=preview_only)
                for doc in docs
            ]

    async def update_knowledge_doc_review(
        self,
        doc_id: str,
        *,
        review_status: str,
        reviewed_by: str = "operator",
        reason: str = "",
        embedding_id: Optional[str] = None,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update review metadata for a knowledge document."""
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
                )
                doc = result.scalar_one_or_none()
                if doc is None:
                    return None
                now = datetime.now(timezone.utc).isoformat()
                meta = dict(doc.meta or {})
                meta.update(extra_meta or {})
                meta["review_status"] = review_status
                meta["reviewed_by"] = reviewed_by
                meta["reviewed_at"] = now
                if review_status == "approved":
                    meta["approved_at"] = now
                    meta.pop("rejected_at", None)
                    meta.pop("review_reason", None)
                elif review_status == "rejected":
                    meta["rejected_at"] = now
                    if reason:
                        meta["review_reason"] = reason
                elif reason:
                    meta["review_reason"] = reason
                doc.meta = meta
                if embedding_id is not None:
                    doc.embedding_id = embedding_id
            await session.refresh(doc)
            return self._serialize_knowledge_doc(doc)

    async def merge_knowledge_doc_meta(
        self,
        doc_id: str,
        extra_meta: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Merge arbitrary metadata into a knowledge document."""
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
                )
                doc = result.scalar_one_or_none()
                if doc is None:
                    return None
                meta = dict(doc.meta or {})
                meta.update(extra_meta)
                doc.meta = meta
            await session.refresh(doc)
            return self._serialize_knowledge_doc(doc)

    async def update_knowledge_doc(
        self,
        doc_id: str,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        embedding_id: Any = _KEEP,
    ) -> Optional[Dict[str, Any]]:
        """Update content/title/meta for a knowledge document."""
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
                )
                doc = result.scalar_one_or_none()
                if doc is None:
                    return None
                if title is not None:
                    doc.title = title
                if content is not None:
                    doc.content = content
                if meta is not None:
                    doc.meta = meta
                if embedding_id is not _KEEP:
                    doc.embedding_id = embedding_id
            await session.refresh(doc)
            return self._serialize_knowledge_doc(doc, preview_only=False)

    async def find_knowledge_doc_by_meta(
        self,
        *,
        meta_key: str,
        meta_value: Any,
        doc_type: Optional[str] = None,
        review_status: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find the most recent knowledge document by a metadata key/value pair."""
        async with await self._session() as session:
            query = select(KnowledgeDocument).order_by(desc(KnowledgeDocument.created_at))
            if doc_type:
                query = query.where(KnowledgeDocument.doc_type == doc_type)
            try:
                query = query.where(
                    func.json_extract(KnowledgeDocument.meta, f"$.{meta_key}") == meta_value
                )
                if review_status:
                    query = query.where(
                        func.json_extract(KnowledgeDocument.meta, "$.review_status") == review_status
                    )
                result = await session.execute(query.limit(1))
                doc = result.scalar_one_or_none()
            except Exception:
                result = await session.execute(query.limit(500))
                doc = next(
                    (
                        item for item in result.scalars().all()
                        if (item.meta or {}).get(meta_key) == meta_value
                        and (review_status is None or (item.meta or {}).get("review_status") == review_status)
                    ),
                    None,
                )
            if doc is None:
                return None
            return self._serialize_knowledge_doc(doc, preview_only=False)

    async def list_skill_candidate_docs(
        self,
        *,
        min_confidence: float = 0.7,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List approved reusable memory docs that have not yet been skill-promoted."""
        async with await self._session() as session:
            result = await session.execute(
                select(KnowledgeDocument)
                .order_by(desc(KnowledgeDocument.created_at))
                .limit(max(limit * 5, 100))
            )
            docs = []
            for doc in result.scalars().all():
                meta = dict(doc.meta or {})
                if meta.get("review_status") != "approved":
                    continue
                if not meta.get("reusable"):
                    continue
                if meta.get("skill_promotion_status"):
                    continue
                if doc.doc_type == "external_learning":
                    if meta.get("external_doc_ingest_status") != "docs_ingested":
                        continue
                    if not list(meta.get("external_doc_paths_ingested") or []):
                        continue
                try:
                    confidence = float(meta.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                if confidence < min_confidence:
                    continue
                docs.append(self._serialize_knowledge_doc(doc))
                if len(docs) >= limit:
                    break
            return docs

    # ------------------------------------------------------------------
    # Experience index (Phase-2 structured fix recall)
    # ------------------------------------------------------------------

    async def record_experience_index(
        self,
        *,
        embedding_id: str,
        task_id: Optional[str],
        stack: Optional[str],
        stage: Optional[str],
        error_signature: Optional[str],
        fix_applied: Optional[str],
        fix_worked: Optional[bool],
        success: bool,
    ) -> None:
        """Insert one row into the experience index.

        The experience index denormalizes the structured fields of an
        experience so SQL can rank fixes without materializing the
        RAG embedding. One row per ``embedding_id``; upserts are
        silently ignored (defensive: dedup is already handled upstream
        by the ingestor's content-hash check).
        """
        if not embedding_id:
            return
        async with await self._session() as session:
            async with session.begin():
                existing = await session.execute(
                    select(ExperienceIndex).where(
                        ExperienceIndex.embedding_id == embedding_id,
                    )
                )
                if existing.scalar_one_or_none() is not None:
                    return
                session.add(ExperienceIndex(
                    embedding_id=embedding_id,
                    task_id=task_id,
                    stack=stack,
                    stage=stage,
                    error_signature=error_signature,
                    fix_applied=fix_applied,
                    fix_worked=fix_worked,
                    success=success,
                ))

    async def mark_fix_worked(
        self,
        embedding_id: str,
        worked: bool,
    ) -> bool:
        """Update an experience index row with the post-fix outcome.

        Returns True when the row was found and updated, False when
        not. Used by the verifier follow-up path: after a targeted
        fix is applied, the next verifier pass either confirms or
        invalidates it; that resolution lands here so the next time
        we recall this signature we know which fix actually worked.
        """
        if not embedding_id:
            return False
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(ExperienceIndex).where(
                        ExperienceIndex.embedding_id == embedding_id,
                    )
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return False
                row.fix_worked = bool(worked)
                return True

    async def mark_latest_unresolved_fix_worked(
        self,
        error_signature: str,
        worked: bool,
    ) -> Optional[str]:
        """Resolve the most-recent unresolved fix row for a signature.

        Used by the post-fix verifier path: after applying a targeted
        fix, the next verifier pass either confirms or invalidates it.
        That outcome lands here as a True/False on the index row whose
        ``error_signature`` matches and whose ``fix_applied`` is set
        but ``fix_worked`` is still None.

        Returns the ``embedding_id`` of the updated row, or None when
        no unresolved row exists for this signature.
        """
        sig = (error_signature or "").strip()
        if not sig:
            return None
        async with await self._session() as session:
            async with session.begin():
                # Sort by created_at + auto-increment id. SQLite's
                # CURRENT_TIMESTAMP has 1-second resolution; two
                # rows inserted in the same second tie on
                # created_at and need the PK as the tiebreaker
                # so "newest" is unambiguous.
                result = await session.execute(
                    select(ExperienceIndex)
                    .where(
                        ExperienceIndex.error_signature == sig,
                        ExperienceIndex.fix_applied.is_not(None),
                        ExperienceIndex.fix_worked.is_(None),
                    )
                    .order_by(
                        desc(ExperienceIndex.created_at),
                        desc(ExperienceIndex.id),
                    )
                    .limit(1)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                row.fix_worked = bool(worked)
                return row.embedding_id

    async def rank_fixes_for_signature(
        self,
        error_signature: str,
        *,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Rank known fixes for an error signature by historical win rate.

        Aggregates the experience index over ``fix_applied`` for the
        given signature and returns the top-``limit`` entries sorted
        by win rate (descending), breaking ties by total attempts so
        a fix with more samples wins over an under-sampled one at the
        same rate.

        Only rows with both a ``fix_applied`` AND a non-null
        ``fix_worked`` are scored — half-resolved fixes (we tried but
        haven't confirmed) don't move the denominator.

        Returns a list of ``{fix_applied, wins, attempts, rate}``
        dicts. Empty when the signature has no resolved attempts.
        """
        sig = (error_signature or "").strip()
        if not sig:
            return []
        async with await self._session() as session:
            result = await session.execute(
                select(ExperienceIndex).where(
                    ExperienceIndex.error_signature == sig,
                    ExperienceIndex.fix_applied.is_not(None),
                    ExperienceIndex.fix_worked.is_not(None),
                )
            )
            rows = result.scalars().all()
        if not rows:
            return []
        tallies: Dict[str, Dict[str, int]] = {}
        for row in rows:
            slot = tallies.setdefault(
                str(row.fix_applied), {"wins": 0, "attempts": 0},
            )
            slot["attempts"] += 1
            if row.fix_worked:
                slot["wins"] += 1
        ranked = [
            {
                "fix_applied": label,
                "wins": stats["wins"],
                "attempts": stats["attempts"],
                "rate": stats["wins"] / stats["attempts"],
            }
            for label, stats in tallies.items()
            if stats["attempts"] > 0
        ]
        ranked.sort(key=lambda r: (r["rate"], r["attempts"]), reverse=True)
        return ranked[: max(0, int(limit))]

    async def anti_patterns_for_signature(
        self,
        error_signature: str,
        *,
        min_attempts: int = 2,
        max_rate: float = 0.34,
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        """Return fixes that historically FAILED for this signature.

        Symmetric to ``rank_fixes_for_signature`` but reads the loser
        end of the rank: a fix is an anti-pattern when it has
        ``>= min_attempts`` graded tries AND a win rate ``<= max_rate``
        (default thresholds: at least 2 attempts, ≤ 34% success).

        Result entries are sorted by rate ASCENDING (worst first),
        breaking ties by attempts descending so a much-tried failure
        wins over a barely-tried one at the same rate.

        Used by the CodeAgent prompt to inject an "avoid these"
        section paired with the ranked-fix winners — same signature,
        opposite end of the distribution.
        """
        sig = (error_signature or "").strip()
        if not sig:
            return []
        async with await self._session() as session:
            result = await session.execute(
                select(ExperienceIndex).where(
                    ExperienceIndex.error_signature == sig,
                    ExperienceIndex.fix_applied.is_not(None),
                    ExperienceIndex.fix_worked.is_not(None),
                )
            )
            rows = result.scalars().all()
        if not rows:
            return []
        tallies: Dict[str, Dict[str, int]] = {}
        for row in rows:
            slot = tallies.setdefault(
                str(row.fix_applied), {"wins": 0, "attempts": 0},
            )
            slot["attempts"] += 1
            if row.fix_worked:
                slot["wins"] += 1
        min_a = max(1, int(min_attempts))
        cap_rate = float(max_rate)
        losers = []
        for label, stats in tallies.items():
            rate = stats["wins"] / stats["attempts"]
            if stats["attempts"] >= min_a and rate <= cap_rate:
                losers.append({
                    "fix_applied": label,
                    "wins": stats["wins"],
                    "attempts": stats["attempts"],
                    "rate": rate,
                })
        # Sort primarily by win-rate (ascending — worst first), then by
        # attempts (most-tried first as a tiebreaker). The cast keeps mypy
        # happy on the unary minus; the dict values are populated above.
        from typing import cast
        losers.sort(key=lambda r: (cast(float, r["rate"]), -cast(int, r["attempts"])))
        return losers[: max(0, int(limit))]

    # ------------------------------------------------------------------
    # System logs
    # ------------------------------------------------------------------

    async def save_log(self, level: str, source: str, message: str,
                       meta: Optional[Dict[str, Any]] = None) -> None:
        """Save a system log entry."""
        from uuid import uuid4
        async with await self._session() as session:
            async with session.begin():
                session.add(SystemLog(
                    id=str(uuid4()),
                    level=level,
                    source=source,
                    message=message,
                    meta=meta or {},
                ))

    async def get_logs(self, level: Optional[str] = None, source: Optional[str] = None,
                       limit: int = 100) -> List[Dict[str, Any]]:
        """Get system logs."""
        async with await self._session() as session:
            query = select(SystemLog).order_by(desc(SystemLog.created_at))
            if level:
                query = query.where(SystemLog.level == level)
            if source:
                query = query.where(SystemLog.source == source)
            query = query.limit(limit)
            result = await session.execute(query)
            return [
                {
                    "id": log_entry.id,
                    "level": log_entry.level,
                    "source": log_entry.source,
                    "message": log_entry.message,
                    "meta": log_entry.meta,
                    "created_at": log_entry.created_at.isoformat(),
                }
                for log_entry in result.scalars().all()
            ]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self) -> Dict[str, Any]:
        """Get memory store statistics."""
        async with await self._session() as session:
            agent_count = await session.scalar(select(func.count()).select_from(AgentModel))
            task_count = await session.scalar(select(func.count()).select_from(TaskModel))
            message_count = await session.scalar(select(func.count()).select_from(MessageModel))
            knowledge_count = await session.scalar(select(func.count()).select_from(KnowledgeDocument))
            log_count = await session.scalar(select(func.count()).select_from(SystemLog))

            # Success rate
            completed_result = await session.execute(
                select(func.count()).select_from(TaskModel).where(TaskModel.status == TaskStatus.COMPLETED)
            )
            failed_result = await session.execute(
                select(func.count()).select_from(TaskModel).where(TaskModel.status == TaskStatus.FAILED)
            )
            completed = completed_result.scalar() or 0
            failed = failed_result.scalar() or 0
            total = completed + failed
            success_rate = completed / total if total > 0 else 0.0

            return {
                "agents": agent_count,
                "tasks": task_count,
                "messages": message_count,
                "knowledge_documents": knowledge_count,
                "logs": log_count,
                "success_rate": round(success_rate, 3),
                "total_completed": completed,
                "total_failed": failed,
            }

    # ------------------------------------------------------------------
    # FTS5 search
    # ------------------------------------------------------------------

    async def search_messages(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Full-text search over messages.content via FTS5."""
        return await self._fts_search(query, "messages", limit)

    async def search_tasks(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Full-text search over tasks.title + tasks.description via FTS5."""
        return await self._fts_search(query, "tasks", limit)

    async def search_logs(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Full-text search over system_logs.message via FTS5."""
        return await self._fts_search(query, "logs", limit)

    async def search_all(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Full-text search across messages, tasks, and logs via FTS5."""
        return await self._fts_search(query, None, limit)

    async def _fts_search(self, query: str, table_name: Optional[str], limit: int) -> List[Dict[str, Any]]:
        """Core FTS5 search implementation."""
        async with await self._session() as session:
            from sqlalchemy import text
            sql = """
                SELECT table_name, record_id
                FROM fts_search
                WHERE content MATCH :query
            """
            params: Dict[str, Any] = {"query": query, "limit": limit}
            if table_name:
                sql += " AND table_name = :table_name"
                params["table_name"] = table_name
            sql += " LIMIT :limit"
            result = await session.execute(text(sql), params)
            rows = result.fetchall()

            # Resolve actual records
            out: List[Dict[str, Any]] = []
            for row in rows:
                tname, rid = row.table_name, row.record_id
                if tname == "messages":
                    msg_result = await session.execute(
                        select(MessageModel).where(MessageModel.id == rid)
                    )
                    msg = msg_result.scalar_one_or_none()
                    if msg:
                        out.append({
                            "table": "messages",
                            "id": msg.id,
                            "source_agent": msg.source_agent,
                            "target_agent": msg.target_agent,
                            "content": msg.content,
                            "created_at": msg.created_at.isoformat() if msg.created_at else None,
                        })
                elif tname == "tasks":
                    task_result = await session.execute(
                        select(TaskModel).where(TaskModel.id == rid)
                    )
                    task = task_result.scalar_one_or_none()
                    if task:
                        out.append({
                            "table": "tasks",
                            "id": task.id,
                            "title": task.title,
                            "description": task.description,
                            "status": task.status.value if task.status else None,
                            "created_at": task.created_at.isoformat() if task.created_at else None,
                        })
                elif tname == "logs":
                    log_result = await session.execute(
                        select(SystemLog).where(SystemLog.id == rid)
                    )
                    log = log_result.scalar_one_or_none()
                    if log:
                        out.append({
                            "table": "logs",
                            "id": log.id,
                            "level": log.level,
                            "source": log.source,
                            "message": log.message,
                            "created_at": log.created_at.isoformat() if log.created_at else None,
                        })
            return out

    async def rebuild_fts_index(self) -> Dict[str, Any]:
        """Drop and rebuild the FTS5 index from existing data."""
        async with await self._session() as session:
            async with session.begin():
                from sqlalchemy import text
                await session.execute(text("DELETE FROM fts_search"))
                # Re-index messages
                msg_result = await session.execute(select(MessageModel))
                for msg in msg_result.scalars().all():
                    await session.execute(text(
                        "INSERT INTO fts_search(content, table_name, record_id) VALUES (:c, 'messages', :id)"
                    ), {"c": msg.content, "id": msg.id})
                # Re-index tasks
                task_result = await session.execute(select(TaskModel))
                for task in task_result.scalars().all():
                    content = f"{task.title or ''} {task.description or ''}"
                    await session.execute(text(
                        "INSERT INTO fts_search(content, table_name, record_id) VALUES (:c, 'tasks', :id)"
                    ), {"c": content, "id": task.id})
                # Re-index logs
                log_result = await session.execute(select(SystemLog))
                for log in log_result.scalars().all():
                    await session.execute(text(
                        "INSERT INTO fts_search(content, table_name, record_id) VALUES (:c, 'logs', :id)"
                    ), {"c": log.message, "id": log.id})
        return {"rebuilt": True}

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def get_or_create_user(self, platform_id: str, platform: str,
                                 display_name: Optional[str] = None) -> Dict[str, Any]:
        """Get existing user or create a new one."""
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(UserModel).where(
                        UserModel.platform_id == platform_id,
                        UserModel.platform == platform,
                    )
                )
                user = result.scalar_one_or_none()
                if user:
                    if display_name and user.display_name != display_name:
                        user.display_name = display_name
                    return {
                        "id": user.id,
                        "platform_id": user.platform_id,
                        "platform": user.platform,
                        "display_name": user.display_name,
                        "profile": json.loads(user.profile_json) if user.profile_json else {},
                        "message_count": user.message_count,
                        "session_count": user.session_count,
                        "created_at": user.created_at.isoformat() if user.created_at else None,
                        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
                    }
                user = UserModel(
                    id=str(uuid.uuid4()),
                    platform_id=platform_id,
                    platform=platform,
                    display_name=display_name,
                    profile_json="{}",
                )
                session.add(user)
                return {
                    "id": user.id,
                    "platform_id": user.platform_id,
                    "platform": user.platform,
                    "display_name": user.display_name,
                    "profile": {},
                    "message_count": 0,
                    "session_count": 0,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "updated_at": user.updated_at.isoformat() if user.updated_at else None,
                }

    async def get_user_profile(self, platform_id: str, platform: str) -> Optional[Dict[str, Any]]:
        """Get user profile by platform id."""
        async with await self._session() as session:
            result = await session.execute(
                select(UserModel).where(
                    UserModel.platform_id == platform_id,
                    UserModel.platform == platform,
                )
            )
            user = result.scalar_one_or_none()
            if not user:
                return None
            return {
                "id": user.id,
                "platform_id": user.platform_id,
                "platform": user.platform,
                "display_name": user.display_name,
                "profile": json.loads(user.profile_json) if user.profile_json else {},
                "message_count": user.message_count,
                "session_count": user.session_count,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "updated_at": user.updated_at.isoformat() if user.updated_at else None,
            }

    async def update_user_profile(self, platform_id: str, platform: str,
                                  profile: Dict[str, Any]) -> bool:
        """Merge profile updates into an existing user."""
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(UserModel).where(
                        UserModel.platform_id == platform_id,
                        UserModel.platform == platform,
                    )
                )
                user = result.scalar_one_or_none()
                if not user:
                    return False
                existing = json.loads(user.profile_json) if user.profile_json else {}
                existing.update(profile)
                user.profile_json = json.dumps(existing)
                return True

    async def increment_user_stats(self, platform_id: str, platform: str,
                                   messages: int = 0, sessions: int = 0) -> bool:
        """Bump message/session counters for a user."""
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(UserModel).where(
                        UserModel.platform_id == platform_id,
                        UserModel.platform == platform,
                    )
                )
                user = result.scalar_one_or_none()
                if not user:
                    return False
                user.message_count += messages
                user.session_count += sessions
                return True

    # ------------------------------------------------------------------
    # Scheduled jobs
    # ------------------------------------------------------------------

    async def save_scheduled_job(self, job_id: str, name: str, schedule_expr: str,
                                 agent_name: Optional[str] = None,
                                 prompt: Optional[str] = None,
                                 enabled: bool = True,
                                 last_run: Optional[datetime] = None,
                                 next_run: Optional[datetime] = None,
                                 run_count: int = 0) -> None:
        """Upsert a scheduled job."""
        from skyn3t.core.models import ScheduledJob as ScheduledJobModel
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(ScheduledJobModel).where(ScheduledJobModel.id == job_id)
                )
                existing = result.scalar_one_or_none()
                if existing:
                    existing.name = name
                    existing.schedule_expr = schedule_expr
                    existing.agent_name = agent_name
                    existing.prompt = prompt
                    existing.enabled = enabled
                    existing.last_run = last_run
                    existing.next_run = next_run
                    existing.run_count = run_count
                else:
                    session.add(ScheduledJobModel(
                        id=job_id,
                        name=name,
                        schedule_expr=schedule_expr,
                        agent_name=agent_name,
                        prompt=prompt,
                        enabled=enabled,
                        last_run=last_run,
                        next_run=next_run,
                        run_count=run_count,
                    ))

    async def get_scheduled_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get a scheduled job by ID."""
        from skyn3t.core.models import ScheduledJob as ScheduledJobModel
        async with await self._session() as session:
            result = await session.execute(
                select(ScheduledJobModel).where(ScheduledJobModel.id == job_id)
            )
            job = result.scalar_one_or_none()
            if not job:
                return None
            return {
                "id": job.id,
                "name": job.name,
                "schedule_expr": job.schedule_expr,
                "agent_name": job.agent_name,
                "prompt": job.prompt,
                "enabled": job.enabled,
                "last_run": job.last_run.isoformat() if job.last_run else None,
                "next_run": job.next_run.isoformat() if job.next_run else None,
                "run_count": job.run_count,
                "created_at": job.created_at.isoformat() if job.created_at else None,
            }

    async def list_scheduled_jobs(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """List all scheduled jobs."""
        from skyn3t.core.models import ScheduledJob as ScheduledJobModel
        async with await self._session() as session:
            query = select(ScheduledJobModel).order_by(ScheduledJobModel.created_at)
            if enabled_only:
                query = query.where(ScheduledJobModel.enabled)
            result = await session.execute(query)
            return [
                {
                    "id": job.id,
                    "name": job.name,
                    "schedule_expr": job.schedule_expr,
                    "agent_name": job.agent_name,
                    "prompt": job.prompt,
                    "enabled": job.enabled,
                    "last_run": job.last_run.isoformat() if job.last_run else None,
                    "next_run": job.next_run.isoformat() if job.next_run else None,
                    "run_count": job.run_count,
                    "created_at": job.created_at.isoformat() if job.created_at else None,
                }
                for job in result.scalars().all()
            ]

    async def delete_scheduled_job(self, job_id: str) -> bool:
        """Delete a scheduled job."""
        from skyn3t.core.models import ScheduledJob as ScheduledJobModel
        async with await self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(ScheduledJobModel).where(ScheduledJobModel.id == job_id)
                )
                job = result.scalar_one_or_none()
                if not job:
                    return False
                await session.delete(job)
                return True

    # ------------------------------------------------------------------
    # Retention / pruning (H24)
    # ------------------------------------------------------------------

    async def prune_system_logs(
        self,
        older_than_days: int,
        keep_last: int = 1000,
        levels: Optional[List[str]] = None,
    ) -> int:
        """Delete system logs older than ``older_than_days``, keeping at least
        ``keep_last`` rows. Restrict to ``levels`` when provided."""
        if older_than_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = cutoff.replace(day=cutoff.day - older_than_days)
        async with await self._session() as session:
            async with session.begin():
                keep_query = (
                    select(SystemLog.id)
                    .order_by(desc(SystemLog.created_at))
                    .limit(keep_last)
                )
                if levels:
                    keep_query = keep_query.where(SystemLog.level.in_(levels))
                keep_result = await session.execute(keep_query)
                keep_ids = {row[0] for row in keep_result.all()}

                cond = SystemLog.created_at < cutoff
                if levels:
                    cond = and_(cond, SystemLog.level.in_(levels))
                if keep_ids:
                    cond = and_(cond, SystemLog.id.notin_(keep_ids))
                result = await session.execute(delete(SystemLog).where(cond))
                return getattr(result, "rowcount", 0) or 0

    async def prune_messages(
        self, older_than_days: int, keep_last: int = 1000
    ) -> int:
        """Delete old inter-agent messages, keeping the most recent ``keep_last``."""
        if older_than_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = cutoff.replace(day=cutoff.day - older_than_days)
        async with await self._session() as session:
            async with session.begin():
                keep_result = await session.execute(
                    select(MessageModel.id)
                    .order_by(desc(MessageModel.created_at))
                    .limit(keep_last)
                )
                keep_ids = {row[0] for row in keep_result.all()}
                cond = MessageModel.created_at < cutoff
                if keep_ids:
                    cond = and_(cond, MessageModel.id.notin_(keep_ids))
                result = await session.execute(delete(MessageModel).where(cond))
                return getattr(result, "rowcount", 0) or 0

    async def prune_experience_index(
        self, older_than_days: int, keep_last: int = 1000
    ) -> int:
        """Delete old experience-index rows, keeping the most recent ``keep_last``."""
        if older_than_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = cutoff.replace(day=cutoff.day - older_than_days)
        async with await self._session() as session:
            async with session.begin():
                keep_result = await session.execute(
                    select(ExperienceIndex.id)
                    .order_by(desc(ExperienceIndex.created_at))
                    .limit(keep_last)
                )
                keep_ids = {row[0] for row in keep_result.all()}
                cond = ExperienceIndex.created_at < cutoff
                if keep_ids:
                    cond = and_(cond, ExperienceIndex.id.notin_(keep_ids))
                result = await session.execute(delete(ExperienceIndex).where(cond))
                return getattr(result, "rowcount", 0) or 0

    async def prune_completed_tasks(
        self, older_than_days: int, keep_last: int = 500
    ) -> int:
        """Delete terminal tasks older than ``older_than_days``,
        keeping the most recent ``keep_last``."""
        if older_than_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = cutoff.replace(day=cutoff.day - older_than_days)
        terminal = {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
        async with await self._session() as session:
            async with session.begin():
                keep_result = await session.execute(
                    select(TaskModel.id)
                    .where(TaskModel.status.in_(terminal))
                    .order_by(desc(TaskModel.created_at))
                    .limit(keep_last)
                )
                keep_ids = {row[0] for row in keep_result.all()}
                cond = and_(
                    TaskModel.status.in_(terminal),
                    TaskModel.created_at < cutoff,
                )
                if keep_ids:
                    cond = and_(cond, TaskModel.id.notin_(keep_ids))
                result = await session.execute(delete(TaskModel).where(cond))
                return getattr(result, "rowcount", 0) or 0

    async def prune_all(
        self,
        *,
        logs_days: int = 7,
        messages_days: int = 30,
        experience_days: int = 90,
        completed_tasks_days: int = 30,
    ) -> Dict[str, int]:
        """Run all retention pruners with sensible defaults."""
        return {
            "logs": await self.prune_system_logs(logs_days),
            "messages": await self.prune_messages(messages_days),
            "experience_index": await self.prune_experience_index(experience_days),
            "completed_tasks": await self.prune_completed_tasks(completed_tasks_days),
        }

    # ------------------------------------------------------------------
    # Consciousness snapshots (H18)
    # ------------------------------------------------------------------

    async def save_consciousness_snapshot(
        self, snapshot: Dict[str, Any], reason: str = "manual"
    ) -> str:
        """Persist a CollectiveConsciousness snapshot and return its id."""
        from skyn3t.core.models import ConsciousnessSnapshot

        snapshot_id = str(uuid.uuid4())
        async with await self._session() as session:
            async with session.begin():
                session.add(
                    ConsciousnessSnapshot(
                        id=snapshot_id,
                        reason=reason,
                        snapshot=snapshot,
                    )
                )
        return snapshot_id

    async def load_latest_consciousness_snapshot(
        self,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent consciousness snapshot blob, if any."""
        from skyn3t.core.models import ConsciousnessSnapshot

        async with await self._session() as session:
            result = await session.execute(
                select(ConsciousnessSnapshot)
                .order_by(desc(ConsciousnessSnapshot.created_at))
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return row.snapshot if row else None
