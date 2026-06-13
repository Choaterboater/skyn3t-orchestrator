"""ExperienceIngestor learns from studio project events."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from skyn3t.core.events import Event, EventType
from skyn3t.memory.ingestor import ExperienceIngestor


class _FakeRAG:
    """Records every add_knowledge_one call without needing ChromaDB."""

    def __init__(self) -> None:
        self.docs: List[Dict[str, Any]] = []
        self.next_id: int = 0

    async def initialize(self) -> None:
        return None

    async def add_knowledge_one(
        self,
        *,
        content: str,
        title: str,
        source: str,
        doc_type: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        self.next_id += 1
        eid = f"emb-{self.next_id}"
        self.docs.append({
            "id": eid,
            "content": content,
            "title": title,
            "source": source,
            "doc_type": doc_type,
            "metadata": metadata or {},
        })
        return eid


def _make_ingestor(tmp_path):
    rag = _FakeRAG()
    bus = MagicMock()
    bus.subscribe = MagicMock()
    ingestor = ExperienceIngestor(
        event_bus=bus,
        rag_engine=rag,
        seen_hashes_path=tmp_path / "seen.json",
    )
    ingestor._memory = None  # avoid hitting the SQL store; ingest path tolerates None
    return ingestor, rag


class _FakeMemoryStore:
    def __init__(self) -> None:
        self.saved_docs: List[Dict[str, Any]] = []

    async def save_knowledge_doc(
        self,
        *,
        title: str,
        content: str,
        source: str,
        doc_type: str,
        embedding_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        self.saved_docs.append(
            {
                "title": title,
                "content": content,
                "source": source,
                "doc_type": doc_type,
                "embedding_id": embedding_id,
                "meta": meta or {},
            }
        )
        return f"doc-{len(self.saved_docs)}"


def test_ingestor_subscribes_to_system_alert(tmp_path) -> None:
    ingestor, _rag = _make_ingestor(tmp_path)
    # SYSTEM_ALERT must be among the subscribed event types — that's
    # the channel studio uses for PROJECT_* events.
    subscribed_event_types = [
        call.args[1] for call in ingestor.event_bus.subscribe.call_args_list
        if len(call.args) > 1
    ]
    assert EventType.SYSTEM_ALERT in subscribed_event_types
    assert EventType.TASK_FAILED in subscribed_event_types  # still listens to legacy


def test_ingestor_ingests_contract_blockers_as_failure_experience(tmp_path) -> None:
    ingestor, rag = _make_ingestor(tmp_path)

    async def _go():
        await ingestor.initialize()
        await ingestor.ingest_project_event(
            "CONTRACT_VERIFIER_BLOCKERS",
            {
                "project_slug": "carnary-115",
                "stage": "contract_verifier",
                "stack": "node",
                "feature_tags": ["glassmorphism", "dark"],
                "findings": [
                    {
                        "severity": "blocker",
                        "category": "palette_schism_css",
                        "file": "src/styles.css",
                        "message": "styles.css uses 4 hex colors not in palette.json",
                    },
                    {
                        "severity": "blocker",
                        "category": "missing_feature_evidence",
                        "file": "src/styles.css",
                        "message": "Brief mentions 'glassmorphism' but no backdrop-filter",
                    },
                ],
            },
        )

    asyncio.run(_go())

    assert len(rag.docs) == 1
    doc = rag.docs[0]
    assert doc["doc_type"] == "experience"
    assert doc["metadata"]["success"] is False
    assert doc["metadata"]["kind"] == "CONTRACT_VERIFIER_BLOCKERS"
    assert doc["metadata"]["project_slug"] == "carnary-115"
    assert doc["metadata"]["stack"] == "node"
    # Concrete blocker categories should land in the body so RAG search
    # on "palette schism" or "glassmorphism" matches.
    assert "palette_schism_css" in doc["content"]
    assert "missing_feature_evidence" in doc["content"]
    assert "src/styles.css" in doc["content"]


def test_ingestor_dedupes_identical_project_events(tmp_path) -> None:
    ingestor, rag = _make_ingestor(tmp_path)
    payload = {
        "project_slug": "carnary-115",
        "stage": "contract_verifier",
        "findings": [
            {"severity": "blocker", "category": "palette_schism_css",
             "file": "src/styles.css", "message": "same"},
        ],
    }

    async def _go():
        await ingestor.initialize()
        await ingestor.ingest_project_event("CONTRACT_VERIFIER_BLOCKERS", payload)
        await ingestor.ingest_project_event("CONTRACT_VERIFIER_BLOCKERS", payload)

    asyncio.run(_go())
    assert len(rag.docs) == 1, "second identical event must be deduped"


def test_ingestor_on_system_alert_filters_unknown_kinds(tmp_path) -> None:
    ingestor, rag = _make_ingestor(tmp_path)

    async def _drain():
        await ingestor.initialize()
        # Unknown kind — must be ignored
        evt = Event(
            event_type=EventType.SYSTEM_ALERT,
            source="studio",
            payload={"kind": "SOME_UNRELATED_EVENT", "project_slug": "x"},
        )
        ingestor._on_system_alert(evt)
        # Allow scheduled tasks to flush (there shouldn't be any)
        await asyncio.sleep(0.05)

    asyncio.run(_drain())
    assert rag.docs == []


def test_ingestor_routes_project_completed_with_failure_verdict(tmp_path) -> None:
    ingestor, rag = _make_ingestor(tmp_path)

    async def _go():
        await ingestor.initialize()
        evt = Event(
            event_type=EventType.SYSTEM_ALERT,
            source="studio",
            payload={
                "kind": "PROJECT_COMPLETED",
                "project_slug": "carnary-115",
                "status": "needs_fixes",
                "verdict": "go-with-fixes",
                "stack": "node",
                "feature_tags": ["glassmorphism", "dark"],
                "message": "Review complete: go-with-fixes (60/100).",
                "build_verification": {
                    "verdict": "no",
                    "summary": "vite build failed",
                    "failure_hint": "src/App.jsx imports a missing component",
                },
            },
        )
        ingestor._on_system_alert(evt)
        # Let the asyncio.create_task fire.
        for _ in range(10):
            await asyncio.sleep(0.02)
            if rag.docs:
                break

    asyncio.run(_go())
    assert len(rag.docs) == 1
    doc = rag.docs[0]
    assert doc["doc_type"] == "experience"
    assert "Verdict: go-with-fixes" in doc["content"]
    assert "60/100" in doc["content"]
    assert "Build Verification: verdict=no" in doc["content"]
    assert "vite build failed" in doc["content"]
    assert doc["metadata"]["feature_tags"] == "glassmorphism, dark"
    assert doc["metadata"]["success"] is False


def test_ingestor_embeds_quality_lessons_and_marks_approved(tmp_path) -> None:
    rag = _FakeRAG()
    store = _FakeMemoryStore()
    bus = MagicMock()
    bus.subscribe = MagicMock()
    ingestor = ExperienceIngestor(
        event_bus=bus,
        rag_engine=rag,
        memory_store=store,
        seen_hashes_path=tmp_path / "seen.json",
    )

    async def _go():
        await ingestor.initialize()
        result = await ingestor.ingest_lesson(
            agent="reflection-agent",
            success=True,
            patterns=["fast feedback"],
            suggestions=["keep the loop tight"],
            task_id="task-9",
        )
        assert result == "emb-1"

    asyncio.run(_go())

    assert len(rag.docs) == 1
    assert rag.docs[0]["doc_type"] == "lesson"
    assert len(store.saved_docs) == 1
    doc = store.saved_docs[0]
    assert doc["doc_type"] == "lesson"
    assert doc["embedding_id"] == "emb-1"
    assert doc["meta"]["review_status"] == "approved"
    assert doc["meta"]["memory_layer"] == "operator"


def test_ingestor_auto_rejects_low_signal_lessons_but_keeps_them(tmp_path) -> None:
    rag = _FakeRAG()
    store = _FakeMemoryStore()
    bus = MagicMock()
    bus.subscribe = MagicMock()
    ingestor = ExperienceIngestor(
        event_bus=bus,
        rag_engine=rag,
        memory_store=store,
        seen_hashes_path=tmp_path / "seen.json",
    )

    async def _go():
        await ingestor.initialize()
        await ingestor.ingest_lesson(
            agent="reflection-agent",
            success=False,
            patterns=[],
            suggestions=[],
            task_id="task-10",
        )

    asyncio.run(_go())

    assert rag.docs == []
    assert len(store.saved_docs) == 1
    rejected = store.saved_docs[0]
    assert rejected["meta"]["review_status"] == "rejected"
    assert rejected["meta"]["review_reason"] == "no actionable lesson content"


def test_ingestor_embeds_insights_for_rag(tmp_path) -> None:
    rag = _FakeRAG()
    store = _FakeMemoryStore()
    bus = MagicMock()
    bus.subscribe = MagicMock()
    ingestor = ExperienceIngestor(
        event_bus=bus,
        rag_engine=rag,
        memory_store=store,
        seen_hashes_path=tmp_path / "seen.json",
    )

    async def _go():
        await ingestor.initialize()
        return await ingestor.ingest_insight(
            agent_name="researcher",
            insight="This is a substantive insight about API design patterns.",
            capability="research",
        )

    embedding_id = asyncio.run(_go())
    assert embedding_id is not None
    assert len(rag.docs) == 1
    assert rag.docs[0]["doc_type"] == "insight"
    assert len(store.saved_docs) == 1
    assert store.saved_docs[0]["embedding_id"] == embedding_id
    assert store.saved_docs[0]["meta"]["review_status"] == "approved"


def test_ingestor_embeds_failure_patterns_for_rag(tmp_path) -> None:
    rag = _FakeRAG()
    store = _FakeMemoryStore()
    bus = MagicMock()
    bus.subscribe = MagicMock()
    ingestor = ExperienceIngestor(
        event_bus=bus,
        rag_engine=rag,
        memory_store=store,
        seen_hashes_path=tmp_path / "seen.json",
    )

    async def _go():
        await ingestor.initialize()
        return await ingestor.ingest_failure_pattern(
            pattern_name="missing-await",
            description="Async functions called without await inside coroutines.",
            suggested_fix="Add await or use asyncio.gather.",
            affected_agents=["code_agent"],
        )

    embedding_id = asyncio.run(_go())
    assert embedding_id is not None
    assert len(rag.docs) == 1
    assert rag.docs[0]["doc_type"] == "pattern"
    assert len(store.saved_docs) == 1
    assert store.saved_docs[0]["embedding_id"] == embedding_id
    assert store.saved_docs[0]["meta"]["review_status"] == "approved"


def test_ingestor_auto_rejects_low_signal_insights(tmp_path) -> None:
    rag = _FakeRAG()
    store = _FakeMemoryStore()
    bus = MagicMock()
    bus.subscribe = MagicMock()
    ingestor = ExperienceIngestor(
        event_bus=bus,
        rag_engine=rag,
        memory_store=store,
        seen_hashes_path=tmp_path / "seen.json",
    )

    async def _go():
        await ingestor.initialize()
        await ingestor.ingest_insight(
            agent_name="researcher",
            insight="short",
        )

    asyncio.run(_go())
    assert rag.docs == []
    assert len(store.saved_docs) == 1
    assert store.saved_docs[0]["embedding_id"] is None
    assert store.saved_docs[0]["meta"]["review_status"] == "rejected"
