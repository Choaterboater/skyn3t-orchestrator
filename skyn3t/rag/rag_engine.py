"""RAG engine for knowledge retrieval and generation."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from skyn3t.config.settings import get_settings
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

        return await self.vector_store.add_documents(
            documents=documents,
            metadatas=metadatas,
        )

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
            context = retrieval["context"]
            prompt = (
                f"Based on the following context, answer the question:\n\n"
                f"Context:\n{context}\n\n"
                f"Question: {query}\n\n"
                f"Answer:"
            )

            # This would call the LLM - simplified here
            answer = f"[RAG-generated answer using {len(retrieval['documents'])} documents]"
        else:
            # Return top document as answer if no LLM available
            answer = retrieval["documents"][0]["content"]

        sources = [
            {
                "id": doc["id"],
                "source": doc["metadata"].get("source", "unknown"),
                "title": doc["metadata"].get("title", "Untitled"),
                "relevance": 1.0 - doc.get("distance", 0.0),
            }
            for doc in retrieval["documents"]
        ]

        return {
            "query": query,
            "answer": answer,
            "sources": sources,
            "retrieval": retrieval,
        }

    async def get_stats(self) -> Dict[str, Any]:
        """Get RAG system statistics."""
        return await self.vector_store.get_collection_stats()
