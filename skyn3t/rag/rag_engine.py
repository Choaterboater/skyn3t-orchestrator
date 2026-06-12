"""RAG engine for knowledge retrieval and generation."""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from skyn3t.rag.document_processor import DocumentProcessor
from skyn3t.rag.vector_store import VectorStore

_logger = logging.getLogger("skyn3t.rag.rag_engine")


# ---------------------------------------------------------------------------
# RAG recall observability metrics (best-effort, no-op without prometheus)
# ---------------------------------------------------------------------------
class _NoOpMetric:
    """No-op stand-in used when prometheus_client is unavailable."""

    def observe(self, value: float) -> None:  # pragma: no cover - trivial
        pass

    def inc(self, amount: float = 1) -> None:  # pragma: no cover - trivial
        pass

    def labels(self, **kwargs: Any) -> "_NoOpMetric":  # pragma: no cover - trivial
        return self


def _build_recall_metrics() -> Dict[str, Any]:
    """Create the recall metrics, falling back to no-ops if prometheus is absent.

    Defined locally (rather than in observability/metrics.py) so the RAG engine
    owns its own instrumentation; emission is always best-effort and never
    raises into the query path.
    """
    try:
        from prometheus_client import Counter, Histogram

        from skyn3t.observability.metrics import get_metrics_registry

        registry = get_metrics_registry()
        if registry is None:
            raise RuntimeError("no prometheus registry")
        latency = Histogram(
            "skyn3t_rag_recall_latency_seconds",
            "Latency of RAGEngine.query vector_store.query calls",
            buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
            registry=registry,
        )
        requested = Counter(
            "skyn3t_rag_recall_requested_total",
            "Total documents requested across RAGEngine.query calls",
            registry=registry,
        )
        returned = Counter(
            "skyn3t_rag_recall_returned_total",
            "Total documents returned across RAGEngine.query calls",
            registry=registry,
        )
        queries = Counter(
            "skyn3t_rag_recall_queries_total",
            "Total RAGEngine.query calls",
            registry=registry,
        )
        hits = Counter(
            "skyn3t_rag_recall_hits_total",
            "RAGEngine.query calls that returned at least one document",
            registry=registry,
        )
        return {
            "latency": latency,
            "requested": requested,
            "returned": returned,
            "queries": queries,
            "hits": hits,
        }
    except Exception:
        noop = _NoOpMetric()
        return {
            "latency": noop,
            "requested": noop,
            "returned": noop,
            "queries": noop,
            "hits": noop,
        }


# Lazily-initialized module-level metric handles (shared across engines).
_RECALL_METRICS: Optional[Dict[str, Any]] = None


def _recall_metrics() -> Dict[str, Any]:
    global _RECALL_METRICS
    if _RECALL_METRICS is None:
        _RECALL_METRICS = _build_recall_metrics()
    return _RECALL_METRICS


class RAGEngine:
    """Retrieval-Augmented Generation engine."""

    def __init__(self):
        self.vector_store = VectorStore()
        self.processor = DocumentProcessor()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the RAG engine."""
        if self._initialized:
            return
        await self.vector_store.initialize()
        self._initialized = True

    async def add_knowledge(
        self,
        content: str,
        title: str = "",
        source: str = "",
        doc_type: str = "text",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Add knowledge to the RAG system."""
        if not self._initialized:
            await self.initialize()

        meta = {
            "title": title,
            "source": source,
            "doc_type": doc_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **(metadata or {}),
        }

        if doc_type == "markdown":
            chunks = self.processor.process_markdown(content, meta)
        elif doc_type in ("python", "javascript", "typescript", "java", "go", "rust"):
            chunks = self.processor.process_code(content, doc_type, meta)
        else:
            chunks = self.processor.process_text(content, meta)

        documents = [c["content"] for c in chunks]
        metadatas = [c["metadata"] for c in chunks]

        document_ids: List[str] = await self.vector_store.add_documents(
            documents=documents,
            metadatas=metadatas,
        )
        # Hybrid (BM25) index caches a corpus snapshot in self._hybrid.
        # Dropping the attribute forces the next query_hybrid() call to
        # rebuild against the fresh corpus instead of returning stale
        # ordering for newly-added documents.
        if hasattr(self, "_hybrid"):
            try:
                delattr(self, "_hybrid")
            except AttributeError:
                pass
        return document_ids

    async def add_knowledge_one(
        self,
        content: str,
        title: str = "",
        source: str = "",
        doc_type: str = "text",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Add knowledge and return the first chunk's id (or None if nothing stored)."""
        ids = await self.add_knowledge(
            content=content,
            title=title,
            source=source,
            doc_type=doc_type,
            metadata=metadata,
        )
        return ids[0] if ids else None

    async def query(
        self,
        query: str,
        n_results: int = 5,
        filter_dict: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Query the knowledge base."""
        # Captured before initialize() so the metric reflects whether the
        # engine had already been warmed up (live shared engine) vs a
        # cold-start that paid the init cost on this very call.
        db_existed = bool(self._initialized)
        if not self._initialized:
            await self.initialize()

        _start = time.perf_counter()
        documents = await self.vector_store.query(
            query_text=query,
            n_results=n_results,
            filter_dict=filter_dict,
        )
        latency_ms = (time.perf_counter() - _start) * 1000.0

        n_returned = len(documents)
        self._emit_recall_metric(
            n_requested=n_results,
            n_returned=n_returned,
            latency_ms=latency_ms,
            db_existed=db_existed,
        )

        # Build context from retrieved documents
        context_parts = []
        for i, doc in enumerate(documents):
            context_parts.append(
                f"[Document {i+1}]\nSource: {doc['metadata'].get('source', 'unknown')}\n"
                f"{doc['content']}\n"
            )

        context = "\n---\n".join(context_parts)

        return {
            "query": query,
            "documents": documents,
            "context": context,
            "document_count": len(documents),
        }

    def _emit_recall_metric(
        self,
        *,
        n_requested: int,
        n_returned: int,
        latency_ms: float,
        db_existed: bool,
    ) -> None:
        """Emit one structured recall metric per query (best-effort).

        Records to Prometheus (no-op when prometheus_client is absent) and a
        structured log line so the dashboard rolling hit-rate can consume it.
        Never raises into the query path.
        """
        try:
            metrics = _recall_metrics()
            metrics["queries"].inc()
            metrics["requested"].inc(n_requested)
            metrics["returned"].inc(n_returned)
            metrics["latency"].observe(latency_ms / 1000.0)
            if n_returned > 0:
                metrics["hits"].inc()
        except Exception:
            # Metric emission is best-effort; never disturb retrieval.
            pass
        try:
            _logger.info(
                "rag_recall n_requested=%d n_returned=%d latency_ms=%.2f db_existed=%s hit=%s",
                n_requested,
                n_returned,
                latency_ms,
                db_existed,
                n_returned > 0,
                extra={
                    "event": "rag_recall",
                    "n_requested": n_requested,
                    "n_returned": n_returned,
                    "latency_ms": round(latency_ms, 3),
                    "db_existed": db_existed,
                    "hit": n_returned > 0,
                },
            )
        except Exception:
            pass

    async def answer(
        self,
        query: str,
        llm_provider: Optional[Any] = None,
        n_results: int = 5,
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Answer a query using RAG."""
        retrieval = await self.query(query, n_results)

        if not retrieval["documents"]:
            return {
                "query": query,
                "answer": "No relevant documents found in the knowledge base.",
                "sources": [],
                "retrieval": retrieval,
            }

        # If an LLM provider is available, generate an answer
        if llm_provider:
            prompt = (
                f"User question:\n{query}\n\n"
                f"Retrieved context:\n{retrieval['context']}\n\n"
                "Answer using only the retrieved context. If the context is insufficient, "
                "say that plainly. When useful, cite documents as [1], [2], etc."
            )
            system = system_prompt or (
                "You are a retrieval-augmented assistant. Give a concise, direct answer "
                "grounded in the provided documents only."
            )
            try:
                candidate = await llm_provider.complete(
                    prompt,
                    system=system,
                    max_tokens=4000,
                    temperature=0.2,
                )
                answer = candidate.strip()
                if not answer or "[deterministic-stub]" in answer:
                    answer = retrieval["documents"][0]["content"]
            except Exception:
                answer = retrieval["documents"][0]["content"]
        else:
            # Return top document as answer if no LLM available
            answer = retrieval["documents"][0]["content"]

        sources = [
            {
                "id": doc["id"],
                "source": doc["metadata"].get("source", "unknown"),
                "title": doc["metadata"].get("title", "Untitled"),
                "relevance": 1.0 - doc.get("distance", 0.0),
                "content": doc["content"],
                "snippet": doc["content"][:200],
                "metadata": doc["metadata"],
            }
            for doc in retrieval["documents"]
        ]

        return {
            "query": query,
            "answer": answer,
            "sources": sources,
            "retrieval": retrieval,
        }

    async def query_hybrid(
        self,
        query: str,
        top_k: int = 5,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Hybrid BM25 + vector retrieval with reciprocal rank fusion."""
        if not self._initialized:
            await self.initialize()
        if not hasattr(self, "_hybrid"):
            from skyn3t.rag.hybrid_search import HybridSearch
            self._hybrid = HybridSearch(self)
        return await self._hybrid.query(query, top_k=top_k, where=where)

    def reindex_hybrid(self) -> None:
        """Rebuild the hybrid (BM25) index from the current corpus."""
        if not hasattr(self, "_hybrid"):
            from skyn3t.rag.hybrid_search import HybridSearch
            self._hybrid = HybridSearch(self)
        self._hybrid.reindex()

    async def get_stats(self) -> Dict[str, Any]:
        """Get RAG system statistics."""
        stats: Dict[str, Any] = await self.vector_store.get_collection_stats()
        return stats
