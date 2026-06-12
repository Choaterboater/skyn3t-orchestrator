"""Vector store for RAG using ChromaDB."""

import logging
from typing import Any, Dict, List, Optional

from skyn3t.config.settings import get_settings

_logger = logging.getLogger("skyn3t.rag.vector_store")


def _sanitize_metadata(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    # Chroma only accepts str | int | float | bool as metadata values.
    # Anything else (None, dict, list, Path, etc.) is coerced to str so an
    # ingest doesn't crash the whole pipeline on one stray field.
    if not meta:
        return {}
    out: Dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, (str, int, float, bool)):
            out[str(k)] = v
        elif v is None:
            continue
        else:
            try:
                out[str(k)] = str(v)
            except Exception:
                continue
    return out


class VectorStore:
    """Vector store for document embeddings and retrieval."""

    def __init__(self, collection_name: str = "skyn3t_knowledge"):
        self.collection_name = collection_name
        self.client: Any = None
        self.collection: Any = None
        self.embedding_function: Any = None
        self._initialized = False

    def _require_collection(self) -> Any:
        """Return the active collection or raise if initialization failed."""
        if self.collection is None:
            raise RuntimeError("Vector store collection is not initialized")
        return self.collection

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

            # Embed the model name in collection metadata so we detect
            # accidental embedding-model swaps. ChromaDB doesn't store the
            # embedder anywhere persistent; without this, a model change
            # silently corrupts cosine similarity (vectors mix dimensions
            # / spaces from two different models).
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self.embedding_function,
                metadata={
                    "hnsw:space": "cosine",
                    "embedding_model": model_name,
                    "schema_version": "1",
                },
            )

            existing_meta = getattr(self.collection, "metadata", None) or {}
            stored_model = existing_meta.get("embedding_model")
            if stored_model and stored_model != model_name:
                raise RuntimeError(
                    f"Vector collection '{self.collection_name}' was built with "
                    f"embedding model '{stored_model}' but current settings "
                    f"point to '{model_name}'. Either revert the setting or "
                    f"reindex the collection (delete + re-add); silent "
                    f"querying across mismatched models corrupts similarity."
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
        else:
            metadatas = [_sanitize_metadata(m) for m in metadatas]

        collection = self._require_collection()
        collection.add(
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
        collection = self._require_collection()

        try:
            results = collection.query(
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

    def all_documents(self) -> List[Dict[str, Any]]:
        """Return the full corpus for hybrid indexing.

        Paginated with limit/offset: a single full-collection ``get()`` hits
        SQLite's "too many SQL variables" limit once the store is large (the
        experience RAG is hundreds of MB), which previously returned [] and
        silently broke hybrid recall while spamming warnings.
        """
        if not self._initialized:
            return []
        collection = self.collection
        if collection is None:
            return []

        corpus: List[Dict[str, Any]] = []
        batch = 500
        offset = 0
        while True:
            try:
                results = collection.get(
                    include=["documents", "metadatas"],
                    limit=batch,
                    offset=offset,
                )
            except Exception as e:
                _logger.warning("chroma get failed (offset=%s): %s", offset, e)
                break
            ids = results.get("ids") or []
            if not ids:
                break
            documents = results.get("documents") or []
            metadatas = results.get("metadatas") or []
            for index, doc_id in enumerate(ids):
                corpus.append({
                    "id": doc_id,
                    "content": documents[index] if index < len(documents) else "",
                    "metadata": metadatas[index] if index < len(metadatas) else {},
                })
            if len(ids) < batch:
                break
            offset += batch

        return corpus

    def recent_documents(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the newest ``limit`` docs by metadata timestamp.

        Cheap variant of ``all_documents`` for dashboard polling: scans
        metadatas only (pulling 59k full document bodies per poll froze
        the event loop), then fetches bodies for just the winners.
        """
        if not self._initialized or self.collection is None:
            return []
        collection = self.collection

        stamped: List[tuple[str, str]] = []  # (timestamp, id), newest first
        batch = 2000
        offset = 0
        while True:
            try:
                results = collection.get(
                    include=["metadatas"], limit=batch, offset=offset,
                )
            except Exception as e:
                _logger.warning(
                    "chroma metadata scan failed (offset=%s): %s", offset, e,
                )
                break
            ids = results.get("ids") or []
            if not ids:
                break
            metadatas = results.get("metadatas") or []
            for index, doc_id in enumerate(ids):
                meta = metadatas[index] if index < len(metadatas) else {}
                stamped.append((str((meta or {}).get("timestamp") or ""), doc_id))
            if len(ids) < batch:
                break
            offset += batch

        stamped.sort(reverse=True)
        newest = [doc_id for _ts, doc_id in stamped[: max(1, int(limit))]]
        if not newest:
            return []
        try:
            results = collection.get(
                ids=newest, include=["documents", "metadatas"],
            )
        except Exception as e:
            _logger.warning("chroma recent fetch failed: %s", e)
            return []
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        by_id: Dict[str, Dict[str, Any]] = {}
        for index, doc_id in enumerate(results.get("ids") or []):
            by_id[doc_id] = {
                "id": doc_id,
                "content": documents[index] if index < len(documents) else "",
                "metadata": metadatas[index] if index < len(metadatas) else {},
            }
        return [by_id[i] for i in newest if i in by_id]

    async def delete(self, ids: List[str]) -> None:
        """Delete documents by ID."""
        if not self._initialized:
            await self.initialize()

        collection = self._require_collection()
        collection.delete(ids=ids)

    async def update(
        self,
        ids: List[str],
        documents: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Update existing documents."""
        if not self._initialized:
            await self.initialize()

        if metadatas is not None:
            metadatas = [_sanitize_metadata(m) for m in metadatas]

        collection = self._require_collection()
        collection.update(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
        )

    async def get_collection_stats(self) -> Dict[str, Any]:
        """Get collection statistics."""
        if not self._initialized:
            await self.initialize()
        collection = self._require_collection()

        return {
            "name": self.collection_name,
            "count": collection.count(),
            "embedding_model": get_settings().embedding_model,
        }

    async def reset(self) -> None:
        """Reset the collection."""
        client = self.client
        if client:
            try:
                client.delete_collection(self.collection_name)
            except Exception:
                pass
            self.collection = client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self.embedding_function,
            )
