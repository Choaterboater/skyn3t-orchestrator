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


class MockVectorStore:
    """Mock vector store for testing."""

    def __init__(self):
        self.docs = []

    async def initialize(self):
        pass

    async def add_documents(self, documents, ids=None, metadatas=None):
        return ids or [f"id_{i}" for i in range(len(documents))]

    async def query(self, query_text, n_results=5, filter_dict=None):
        return [
            {
                "id": "id_1",
                "content": "Python is a programming language.",
                "metadata": {"title": "About Python"},
                "distance": 0.1,
            }
        ]

    async def get_collection_stats(self):
        return {"count": len(self.docs)}
