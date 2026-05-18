"""Tests for RAG components."""

import pytest

from skyn3t.rag.document_processor import DocumentProcessor


class TestDocumentProcessor:
    def test_chunk_text(self):
        processor = DocumentProcessor()
        processor.chunk_size = 10
        processor.chunk_overlap = 2

        text = " ".join([f"word{i}" for i in range(30)])
        chunks = processor._chunk_text(text)

        assert len(chunks) > 1
        assert all(len(c.split()) <= 10 for c in chunks)

    def test_process_markdown(self):
        processor = DocumentProcessor()
        md = "# Header 1\n\nSome content here.\n\n# Header 2\n\nMore content."
        chunks = processor.process_markdown(md)

        assert len(chunks) > 0
        assert all("Section:" in c["content"] for c in chunks)

    def test_extract_entities(self):
        processor = DocumentProcessor()
        text = "Check out https://example.com and email@test.com @user"
        entities = processor.extract_entities(text)

        assert "https://example.com" in entities["urls"]
        assert "email@test.com" in entities["emails"]
        assert "user" in entities["mentions"]


class TestRAGEngine:
    @pytest.mark.asyncio
    async def test_add_and_query(self):
        from skyn3t.rag.rag_engine import RAGEngine

        engine = RAGEngine()
        # Mock vector store to avoid chromadb dependency
        engine.vector_store = MockVectorStore()
        engine._initialized = True

        ids = await engine.add_knowledge(
            content="Python is a programming language.",
            title="About Python",
            source="docs",
        )
        assert len(ids) > 0

        result = await engine.query("What is Python?")
        assert "query" in result
        assert "documents" in result

    @pytest.mark.asyncio
    async def test_query_hybrid_uses_current_rag_api_and_filters(self):
        from skyn3t.rag.rag_engine import RAGEngine

        engine = RAGEngine()
        engine.vector_store = MockVectorStore()
        engine._initialized = True

        await engine.add_knowledge(
            content="Python powers async orchestration.",
            title="Python Guide",
            source="docs",
            metadata={"topic": "python"},
        )
        await engine.add_knowledge(
            content="Rust focuses on ownership and memory safety.",
            title="Rust Guide",
            source="docs",
            metadata={"topic": "rust"},
        )

        results = await engine.query_hybrid(
            "Python orchestration",
            top_k=3,
            where={"topic": "python"},
        )

        assert results
        assert all(doc["metadata"]["topic"] == "python" for doc in results)
        assert any("Python" in doc["content"] for doc in results)

    @pytest.mark.asyncio
    async def test_answer_includes_source_content_for_frontend(self):
        from skyn3t.rag.rag_engine import RAGEngine

        engine = RAGEngine()
        engine.vector_store = MockVectorStore()
        engine._initialized = True

        await engine.add_knowledge(
            content="SkyN3t stores retrieval snippets for the dashboard.",
            title="Frontend contract",
            source="docs",
            metadata={"topic": "rag-ui"},
        )

        result = await engine.answer("What does the dashboard show?")

        assert result["sources"]
        assert result["sources"][0]["title"] == "Frontend contract"
        assert result["sources"][0]["source"] == "docs"
        assert result["sources"][0]["content"] == "SkyN3t stores retrieval snippets for the dashboard."
        assert result["sources"][0]["snippet"] == "SkyN3t stores retrieval snippets for the dashboard."
        assert result["sources"][0]["metadata"]["topic"] == "rag-ui"

    @pytest.mark.asyncio
    async def test_answer_uses_llm_provider_when_available(self):
        from skyn3t.rag.rag_engine import RAGEngine

        class FakeLLM:
            def __init__(self):
                self.calls = []

            async def complete(self, prompt, **kwargs):
                self.calls.append({"prompt": prompt, **kwargs})
                return "Synthesized answer from retrieval."

        engine = RAGEngine()
        engine.vector_store = MockVectorStore()
        engine._initialized = True

        await engine.add_knowledge(
            content="SkyN3t can answer from retrieved context.",
            title="Knowledge note",
            source="docs",
        )
        llm = FakeLLM()

        result = await engine.answer("What can SkyN3t do?", llm_provider=llm)

        assert result["answer"] == "Synthesized answer from retrieval."
        assert llm.calls
        assert "Retrieved context" in llm.calls[0]["prompt"]
        assert llm.calls[0]["temperature"] == 0.2


class MockVectorStore:
    """Mock vector store for testing."""

    def __init__(self):
        self.docs = []

    async def initialize(self):
        pass

    async def add_documents(self, documents, ids=None, metadatas=None):
        ids = ids or [f"id_{len(self.docs) + i}" for i in range(len(documents))]
        for index, doc in enumerate(documents):
            self.docs.append({
                "id": ids[index],
                "content": doc,
                "metadata": metadatas[index] if metadatas else {},
                "distance": 0.1,
            })
        return ids

    async def query(self, query_text, n_results=5, filter_dict=None):
        results = self.docs
        if filter_dict:
            results = [
                doc for doc in results
                if all(doc["metadata"].get(key) == value for key, value in filter_dict.items())
            ]
        return results[:n_results]

    def all_documents(self):
        return [
            {
                "id": doc["id"],
                "content": doc["content"],
                "metadata": doc["metadata"],
            }
            for doc in self.docs
        ]

    async def get_collection_stats(self):
        return {"count": len(self.docs)}
