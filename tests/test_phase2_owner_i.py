"""Phase 2 — Owner I: RAG recall metric instrumentation (rag_engine.py).

Proves RAGEngine.query emits exactly one structured recall metric per call
with the contracted fields (n_requested, n_returned, latency_ms, db_existed)
WITHOUT a live ChromaDB (vector_store is stubbed), and that query()'s return
shape (query/documents/context/document_count) is unchanged.
"""

import logging

import pytest

from skyn3t.rag.rag_engine import RAGEngine


class _StubVectorStore:
    """Returns a fixed number of docs; never touches chromadb."""

    def __init__(self, docs):
        self._docs = docs
        self.queries = []

    async def initialize(self):
        # Used by the cold-start branch to verify db_existed flips correctly.
        return None

    async def query(self, query_text, n_results=5, filter_dict=None):
        self.queries.append((query_text, n_results, filter_dict))
        return self._docs[:n_results]


def _doc(i):
    return {
        "id": f"id_{i}",
        "content": f"content {i}",
        "metadata": {"source": f"src_{i}"},
        "distance": 0.1,
    }


@pytest.mark.asyncio
async def test_query_emits_recall_metric_with_contract_fields(monkeypatch):
    engine = RAGEngine()
    engine.vector_store = _StubVectorStore([_doc(0), _doc(1)])
    engine._initialized = True  # warm engine -> db_existed should be True

    captured = {}

    def fake_emit(*, n_requested, n_returned, latency_ms, db_existed):
        captured["n_requested"] = n_requested
        captured["n_returned"] = n_returned
        captured["latency_ms"] = latency_ms
        captured["db_existed"] = db_existed

    monkeypatch.setattr(engine, "_emit_recall_metric", fake_emit)

    result = await engine.query("hello", n_results=3)

    # Exactly one emission with the contracted fields.
    assert captured["n_requested"] == 3
    assert captured["n_returned"] == 2  # only 2 docs available
    assert isinstance(captured["latency_ms"], float)
    assert captured["latency_ms"] >= 0.0
    assert captured["db_existed"] is True

    # Return shape MUST be unchanged.
    assert set(result.keys()) == {"query", "documents", "context", "document_count"}
    assert result["query"] == "hello"
    assert result["document_count"] == 2
    assert len(result["documents"]) == 2


@pytest.mark.asyncio
async def test_db_existed_false_on_cold_start(monkeypatch):
    engine = RAGEngine()
    engine.vector_store = _StubVectorStore([_doc(0)])
    engine._initialized = False  # cold -> db_existed should be False

    captured = {}
    monkeypatch.setattr(
        engine,
        "_emit_recall_metric",
        lambda **kw: captured.update(kw),
    )

    await engine.query("cold", n_results=5)

    assert captured["db_existed"] is False
    assert captured["n_requested"] == 5
    assert captured["n_returned"] == 1


@pytest.mark.asyncio
async def test_emit_recall_metric_logs_structured_fields(caplog):
    """The real _emit_recall_metric must log a structured rag_recall record
    (best-effort, no prometheus required) so the dashboard can consume it."""
    engine = RAGEngine()
    engine.vector_store = _StubVectorStore([_doc(0), _doc(1), _doc(2)])
    engine._initialized = True

    with caplog.at_level(logging.INFO, logger="skyn3t.rag.rag_engine"):
        await engine.query("logme", n_results=2)

    recall_records = [r for r in caplog.records if getattr(r, "event", None) == "rag_recall"]
    assert len(recall_records) == 1
    rec = recall_records[0]
    assert rec.n_requested == 2
    assert rec.n_returned == 2  # capped at n_results
    assert rec.hit is True
    assert rec.db_existed is True
    assert isinstance(rec.latency_ms, float)


@pytest.mark.asyncio
async def test_emit_recall_metric_never_raises_into_query(monkeypatch):
    """Metric emission is best-effort: a broken metrics backend must not
    propagate into the query path."""
    import skyn3t.rag.rag_engine as mod

    engine = RAGEngine()
    engine.vector_store = _StubVectorStore([_doc(0)])
    engine._initialized = True

    def boom():
        raise RuntimeError("prometheus exploded")

    monkeypatch.setattr(mod, "_recall_metrics", boom)

    # Should not raise despite the metrics backend blowing up.
    result = await engine.query("safe", n_results=1)
    assert result["document_count"] == 1


@pytest.mark.asyncio
async def test_zero_results_marks_miss(monkeypatch):
    engine = RAGEngine()
    engine.vector_store = _StubVectorStore([])  # no docs -> miss
    engine._initialized = True

    captured = {}
    monkeypatch.setattr(
        engine,
        "_emit_recall_metric",
        lambda **kw: captured.update(kw),
    )

    result = await engine.query("nothing", n_results=4)

    assert captured["n_returned"] == 0
    assert result["document_count"] == 0
