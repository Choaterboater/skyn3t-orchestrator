"""Persistent memory store for SkyN3t — the swarm's long-term memory."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skyn3t.core.models import (
    Agent as AgentModel,
)
from skyn3t.core.models import (
    AgentStatus,
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
from skyn3t.memory.database import get_session_maker


class MemoryStore:
    """Persistent store for agent states, tasks, messages, lessons, and logs.

    This is the swarm's long-term memory. Everything that happens gets recorded
    here so agents can recall past experiences across restarts.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
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
        async with self._lock:
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
        async with self._lock:
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
                fallback = await session.execute(
                    select(TaskModel).order_by(desc(TaskModel.created_at)).limit(1000)
                )
                for t in fallback.scalars().all():
                    sid = t.input_data.get("_meta", {}).get("session_id") if t.input_data else None
                    if sid == session_id:
                        tasks.append(self._task_to_dict(t))
                        if len(tasks) >= limit:
                            break

            # Also get messages
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
                           context: Optional[Dict[str, Any]] = None) -> str:
        """Save an inter-agent message."""
        from uuid import uuid4
        msg_id = str(uuid4())
        async with self._lock:
            async with await self._session() as session:
                async with session.begin():
                    session.add(MessageModel(
                        id=msg_id,
                        source_agent=source_agent,
                        target_agent=target_agent,
                        content=content,
                        message_type=message_type,
                        context=context or {},
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

    async def save_lesson(self, title: str, content: str, source: str,
                          doc_type: str = "lesson", meta: Optional[Dict[str, Any]] = None,
                          embedding_id: Optional[str] = None) -> str:
        """Save a lesson or knowledge document."""
        from uuid import uuid4
        doc_id = str(uuid4())
        async with self._lock:
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
            return [
                {
                    "id": d.id,
                    "title": d.title,
                    "content": d.content[:500],
                    "source": d.source,
                    "doc_type": d.doc_type,
                    "meta": d.meta,
                    "embedding_id": d.embedding_id,
                    "created_at": d.created_at.isoformat(),
                }
                for d in result.scalars().all()
            ]

    # ------------------------------------------------------------------
    # System logs
    # ------------------------------------------------------------------

    async def save_log(self, level: str, source: str, message: str,
                       meta: Optional[Dict[str, Any]] = None) -> None:
        """Save a system log entry."""
        from uuid import uuid4
        async with self._lock:
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
