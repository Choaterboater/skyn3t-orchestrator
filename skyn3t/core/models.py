"""Database models for the orchestrator."""

import enum
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from skyn3t.config.settings import get_settings


class Base(AsyncAttrs, DeclarativeBase):
    pass


class AgentStatus(str, enum.Enum):
    """Agent status enumeration."""

    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"
    ERROR = "error"
    RECOVERING = "recovering"
    MAINTENANCE = "maintenance"


class TaskStatus(str, enum.Enum):
    """Task status enumeration."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class Agent(Base):
    """Agent model."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus), default=AgentStatus.IDLE, nullable=False
    )
    capabilities: Mapped[List[str]] = mapped_column(JSON, default=list)
    config: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    meta: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )

    tasks: Mapped[List["Task"]] = relationship(
        "Task", back_populates="agent", foreign_keys="Task.agent_id"
    )
    messages: Mapped[List["Message"]] = relationship(
        "Message", back_populates="agent", foreign_keys="Message.agent_id"
    )


class Task(Base):
    """Task model."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.PENDING, nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=0)
    agent_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("agents.id"), nullable=True
    )
    parent_task_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("tasks.id"), nullable=True
    )
    input_data: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    output_data: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )

    agent: Mapped[Optional["Agent"]] = relationship(
        "Agent", back_populates="tasks", foreign_keys=[agent_id]
    )
    subtasks: Mapped[List["Task"]] = relationship(
        "Task", backref="parent", remote_side="Task.id"
    )


class Message(Base):
    """Inter-agent message model."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    agent_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("agents.id"), nullable=True
    )
    source_agent: Mapped[str] = mapped_column(String(255), nullable=False)
    target_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(String(50), default="chat")
    context: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    agent: Mapped[Optional["Agent"]] = relationship(
        "Agent", back_populates="messages", foreign_keys=[agent_id]
    )


class KnowledgeDocument(Base):
    """Knowledge document for RAG."""

    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    doc_type: Mapped[str] = mapped_column(String(50), default="text")
    meta: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    embedding_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class SystemLog(Base):
    """System log entry."""

    __tablename__ = "system_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


async def init_db() -> None:
    """Initialize the database."""
    from skyn3t.memory.database import get_engine
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Get a database session."""
    from skyn3t.memory.database import get_session_maker
    maker = get_session_maker()
    async with maker() as session:
        yield session
