"""RAG engine for knowledge retrieval and generation."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from skyn3t.rag.document_processor import DocumentProcessor
from skyn3t.rag.vector_store import VectorStore


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
        if not self._initialized:
            await self.initialize()

        documents = await self.vector_store.query(
            query_text=query,
            n_results=n_results,
            filter_dict=filter_dict,
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
                    max_tokens=900,
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
