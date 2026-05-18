"""Hybrid BM25 + vector retrieval with reciprocal rank fusion.

The vector arm uses whatever the existing RAGEngine.query exposes. The BM25
arm reads the same documents from the underlying VectorStore (we treat its
documents() as the corpus) and scores via term frequency / inverse doc freq.

Top-k from each is fused via RRF (k=60 standard) and returned.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("skyn3t.rag.hybrid_search")

_TOKEN = re.compile(r"\w+")


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


class BM25Index:
    """Tiny in-memory BM25 index. Rebuilt from corpus on demand."""

    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: List[Dict[str, Any]] = []  # [{id, content, metadata, tokens}]
        self.df: Counter = Counter()
        self.avg_dl: float = 1.0
        self.idf: Dict[str, float] = {}

    def index(self, docs: List[Dict[str, Any]]) -> None:
        self.docs = []
        self.df.clear()
        total_dl = 0
        for d in docs:
            content = d.get("content", "") or ""
            tokens = _tokenize(content)
            entry = {**d, "tokens": tokens, "_dl": len(tokens),
                       "_tf": Counter(tokens)}
            self.docs.append(entry)
            for t in set(tokens):
                self.df[t] += 1
            total_dl += len(tokens)
        N = len(self.docs)
        self.avg_dl = total_dl / N if N else 1.0
        # Robertson IDF
        self.idf = {t: math.log(1 + (N - df + 0.5) / (df + 0.5))
                    for t, df in self.df.items()}

    def search(self, query: str, top_k: int = 10) -> List[Tuple[Dict[str, Any], float]]:
        if not self.docs:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        scored: List[Tuple[Dict[str, Any], float]] = []
        for d in self.docs:
            tf = d["_tf"]
            dl = d["_dl"]
            score = 0.0
            for t in q_tokens:
                if t not in self.idf:
                    continue
                f = tf.get(t, 0)
                if not f:
                    continue
                num = f * (self.k1 + 1)
                den = f + self.k1 * (1 - self.b + self.b * dl / self.avg_dl)
                score += self.idf[t] * (num / den)
            if score > 0:
                scored.append((d, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


def reciprocal_rank_fusion(*ranked_lists: List[Tuple[Dict[str, Any], float]],
                            k: int = 60, top_k: int = 5) -> List[Dict[str, Any]]:
    """Combine multiple ranked lists via RRF. Items keyed by 'id' (or content hash)."""
    fused: Dict[str, Tuple[float, Dict[str, Any]]] = {}
    for ranking in ranked_lists:
        for rank, (doc, _score) in enumerate(ranking):
            doc_id = doc.get("id")
            if doc_id:
                key = str(doc_id)
            else:
                key = format(hash(doc.get("content", "") or "") & 0xFFFFFFFFFFFF, "x")
            score = 1.0 / (k + rank + 1)
            if key in fused:
                fused[key] = (fused[key][0] + score, doc)
            else:
                fused[key] = (score, doc)
    out = sorted(fused.values(), key=lambda x: -x[0])
    return [doc for _s, doc in out[:top_k]]


class HybridSearch:
    """Wraps an existing RAGEngine + a BM25Index for hybrid retrieval."""

    def __init__(self, rag_engine, *, top_k_each: int = 10):
        self.rag = rag_engine
        self.top_k_each = top_k_each
        self.bm25 = BM25Index()
        self._indexed = False

    def reindex(self) -> None:
        """Rebuild the BM25 index from the underlying vector store's documents."""
        try:
            # Try the most likely access patterns
            store = getattr(self.rag, "vector_store", None) or getattr(self.rag, "_store", None)
            docs: List[Dict[str, Any]] = []
            if store is not None and hasattr(store, "all_documents"):
                docs = store.all_documents()
            elif hasattr(self.rag, "all_documents"):
                docs = self.rag.all_documents()
            else:
                # Best-effort: empty index - fall back to vector-only via fused
                docs = []
            self.bm25.index(docs)
            self._indexed = True
        except Exception:
            logger.exception("BM25 reindex failed; hybrid search will degrade to vector-only")

    async def query(self, query: str, top_k: int = 5,
                     where: Optional[Dict[str, Any]] = None,
                     **kwargs) -> List[Dict[str, Any]]:
        """Hybrid retrieval. Returns merged top_k results."""
        if not self._indexed:
            self.reindex()

        # Vector arm
        vec_hits: List[Dict[str, Any]] = []
        try:
            if hasattr(self.rag, "query"):
                raw_hits = None
                try:
                    raw_hits = await self.rag.query(
                        query=query,
                        n_results=self.top_k_each,
                        filter_dict=where,
                    )
                except TypeError:
                    try:
                        raw_hits = await self.rag.query(
                            query=query,
                            top_k=self.top_k_each,
                            where=where,
                        )
                    except TypeError:
                        raw_hits = await self.rag.query(query, self.top_k_each)

                if isinstance(raw_hits, dict):
                    vec_hits = raw_hits.get("documents", [])
                elif isinstance(raw_hits, list):
                    vec_hits = raw_hits
        except Exception:
            logger.exception("vector arm failed")

        # Filter by `where` (we'll filter post-hoc for BM25)
        def _matches(doc, w):
            if not w:
                return True
            md = doc.get("metadata", {}) or {}
            return all(md.get(k) == v for k, v in w.items())

        if where:
            vec_hits = [doc for doc in vec_hits if _matches(doc, where)]

        # BM25 arm
        bm_hits = self.bm25.search(query, top_k=self.top_k_each)
        if where:
            bm_hits = [(d, s) for (d, s) in bm_hits if _matches(d, where)]

        # Score-tag vector hits to make ranking compatible
        vec_ranked = [(h, h.get("score", 0.0) or (1.0 - i*0.05))
                      for i, h in enumerate(vec_hits)]
        bm_ranked = bm_hits

        merged = reciprocal_rank_fusion(vec_ranked, bm_ranked, top_k=top_k)
        return merged
