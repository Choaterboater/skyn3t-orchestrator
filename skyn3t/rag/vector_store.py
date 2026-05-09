"""Vector store for RAG using ChromaDB."""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from skyn3t.config.settings import get_settings

_logger = logging.getLogger("skyn3t.rag.vector_store")


class VectorStore:
    """Vector store for document embeddings and retrieval."""

    def __init__(self, collection_name: str = "skyn3t_knowledge"):
        self.collection_name = collection_name
        self.client = None
        self.collection = None
        self.embedding_function = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize ChromaDB and embedding model."""
        if self._initialized:
            return

        try:
            import chromadb
            from chromadb.utils import embedding_functions

            settings = get_settings()
            persist_dir = settings.vector_db_path

            self.client = chromadb.PersistentClient(path=persist_dir)

            # Use sentence-transformers for embeddings
            model_name = settings.embedding_model
            self.embedding_function = (
                embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=model_name
                )
            )

            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self.embedding_function,
                metadata={"hnsw:space": "cosine"},
            )

            self._initialized = True

        except ImportError as e:
            raise ImportError(
                f"Required package not installed: {e}. "
                "Run: pip install chromadb sentence-transformers"
            )

    async def add_documents(
        self,
        documents: List[str],
        ids: Optional[List[str]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        """Add documents to the vector store."""
        if not self._initialized:
            await self.initialize()

        if ids is None:
            import uuid

            ids = [str(uuid.uuid4()) for _ in documents]

        if metadatas is None:
            metadatas = [{} for _ in documents]

        self.collection.add(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
        )

        return ids

    async def query(
        self,
        query_text: str,
        n_results: int = 5,
        filter_dict: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Query the vector store for similar documents."""
        if not self._initialized:
            await self.initialize()

        settings = get_settings()
        n_results = min(n_results, settings.top_k_retrieval)

        try:
            results = self.collection.query(
                query_texts=[query_text],
                n_results=n_results,
                where=filter_dict,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            _logger.warning("chroma query failed: %s", e)
            return []

        documents = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                documents.append({
                    "id": doc_id,
                    "content": results["documents"][0][i] if results["documents"] else "",
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "distance": results["distances"][0][i] if results["distances"] else 0.0,
                })

        return documents

    async def delete(self, ids: List[str]) -> None:
        """Delete documents by ID."""
        if not self._initialized:
            await self.initialize()

        self.collection.delete(ids=ids)

    async def update(
        self,
        ids: List[str],
        documents: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Update existing documents."""
        if not self._initialized:
            await self.initialize()

        self.collection.update(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
        )

    async def get_collection_stats(self) -> Dict[str, Any]:
        """Get collection statistics."""
        if not self._initialized:
            await self.initialize()

        return {
            "name": self.collection_name,
            "count": self.collection.count(),
            "embedding_model": get_settings().embedding_model,
        }

    async def reset(self) -> None:
        """Reset the collection."""
        if self.client:
            try:
                self.client.delete_collection(self.collection_name)
            except Exception:
                pass
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self.embedding_function,
            )
