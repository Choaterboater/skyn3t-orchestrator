from __future__ import annotations

import pytest

from skyn3t.cortex.external_pattern_synthesizer import ExternalPatternSynthesizer


class _FakeMemoryStore:
    def __init__(self, docs):
        self.docs = [dict(doc) for doc in docs]
        self.saved = []
        self.updated = []

    async def get_knowledge_doc(self, doc_id):
        for doc in self.docs:
            if doc["id"] == doc_id:
                return dict(doc)
        return None

    async def get_lessons(self, doc_type=None, limit=500):
        docs = [doc for doc in self.docs if doc_type is None or doc["doc_type"] == doc_type]
        return [dict(doc) for doc in docs[:limit]]

    async def save_knowledge_doc(self, **kwargs):
        doc_id = f"doc-{len(self.docs)+1}"
        saved = {
            "id": doc_id,
            "title": kwargs["title"],
            "content": kwargs["content"],
            "source": kwargs["source"],
            "doc_type": kwargs["doc_type"],
            "meta": kwargs["meta"],
            "embedding_id": kwargs.get("embedding_id"),
        }
        self.docs.append(saved)
        self.saved.append(saved)
        return doc_id

    async def find_knowledge_doc_by_meta(self, *, meta_key, meta_value, doc_type=None, review_status=None):
        for doc in self.docs:
            if doc_type is not None and doc["doc_type"] != doc_type:
                continue
            meta = dict(doc.get("meta") or {})
            if meta.get(meta_key) != meta_value:
                continue
            if review_status is not None and meta.get("review_status") != review_status:
                continue
            return dict(doc)
        return None

    async def update_knowledge_doc(self, doc_id, *, title=None, content=None, meta=None, embedding_id=None):
        for doc in self.docs:
            if doc["id"] != doc_id:
                continue
            if title is not None:
                doc["title"] = title
            if content is not None:
                doc["content"] = content
            if meta is not None:
                doc["meta"] = meta
            self.updated.append(dict(doc))
            return dict(doc)
        return None


def _external_doc(doc_id: str, repo: str, *, query: str, topics: list[str], lane: str = "fit", language: str = "Python"):
    return {
        "id": doc_id,
        "title": f"External learning {repo}",
        "content": f"summary for {repo}",
        "source": "repo_scout:gitlab",
        "doc_type": "external_learning",
        "meta": {
            "review_status": "approved",
            "reusable": True,
            "external_doc_ingest_status": "docs_ingested",
            "external_doc_paths_ingested": ["README.md"],
            "lane": lane,
            "language": language,
            "query": query,
            "topics": topics,
            "repo": repo,
            "repo_key": repo,
            "source_platform": "gitlab",
        },
    }


@pytest.mark.asyncio
async def test_synthesizer_creates_draft_pattern_from_related_external_docs():
    store = _FakeMemoryStore(
        [
            _external_doc("mem-1", "org/cortex-a", query="cortex autonomy review", topics=["cortex", "autonomy"]),
            _external_doc("mem-2", "org/cortex-b", query="cortex autonomy memory", topics=["cortex", "memory"]),
        ]
    )
    synth = ExternalPatternSynthesizer(store)

    result = await synth.synthesize_for_doc("mem-1")

    assert result == {
        "status": "created",
        "doc_id": "doc-3",
        "consensus_count": 2,
        "signals": ["cortex", "autonomy"],
        "lesson": {"status": "created", "doc_id": "doc-4"},
        "eval": {"status": "created", "doc_id": "doc-5"},
    }
    assert len(store.saved) == 3
    pattern, lesson, eval_asset = store.saved
    assert pattern["doc_type"] == "pattern"
    assert pattern["meta"]["review_status"] == "draft"
    assert pattern["meta"]["external_pattern"] is True
    assert pattern["meta"]["confidence"] == 0.62
    assert pattern["meta"]["source_repos"] == ["org/cortex-a", "org/cortex-b"]
    assert "Evaluation Ideas:" in pattern["content"]
    assert "org/cortex-a" in pattern["content"]
    assert lesson["doc_type"] == "lesson"
    assert lesson["meta"]["external_lesson"] is True
    assert lesson["meta"]["confidence"] == 0.64
    assert "Suggestions:" in lesson["content"]
    assert eval_asset["doc_type"] == "evaluation"
    assert eval_asset["meta"]["external_eval"] is True
    assert eval_asset["meta"]["reusable"] is False
    assert eval_asset["meta"]["checks"]
    assert "Checks:" in eval_asset["content"]


@pytest.mark.asyncio
async def test_synthesizer_updates_existing_draft_pattern():
    existing_pattern = {
        "id": "pattern-1",
        "title": "Old pattern",
        "content": "old",
        "source": "external_pattern_synthesizer",
        "doc_type": "pattern",
        "meta": {
            "review_status": "draft",
            "synthesis_key": "external-pattern:fit:python",
        },
    }
    store = _FakeMemoryStore(
        [
            _external_doc("mem-1", "org/cortex-a", query="cortex autonomy review", topics=["cortex", "autonomy"]),
            _external_doc("mem-2", "org/cortex-b", query="cortex autonomy memory", topics=["cortex", "memory"]),
            existing_pattern,
        ]
    )
    synth = ExternalPatternSynthesizer(store)

    result = await synth.synthesize_for_doc("mem-1")

    assert result == {
        "status": "updated",
        "doc_id": "pattern-1",
        "consensus_count": 2,
        "signals": ["cortex", "autonomy"],
        "lesson": {"status": "created", "doc_id": "doc-4"},
        "eval": {"status": "created", "doc_id": "doc-5"},
    }
    assert store.updated
    assert store.updated[0]["title"].startswith("External pattern:")
    assert store.updated[0]["meta"]["consensus_count"] == 2


@pytest.mark.asyncio
async def test_synthesizer_skips_locked_non_draft_pattern():
    existing_pattern = {
        "id": "pattern-1",
        "title": "Approved pattern",
        "content": "approved",
        "source": "external_pattern_synthesizer",
        "doc_type": "pattern",
        "meta": {
            "review_status": "approved",
            "synthesis_key": "external-pattern:fit:python",
        },
    }
    store = _FakeMemoryStore(
        [
            _external_doc("mem-1", "org/cortex-a", query="cortex autonomy review", topics=["cortex", "autonomy"]),
            _external_doc("mem-2", "org/cortex-b", query="cortex autonomy memory", topics=["cortex", "memory"]),
            existing_pattern,
        ]
    )
    synth = ExternalPatternSynthesizer(store)

    result = await synth.synthesize_for_doc("mem-1")

    assert result == {
        "status": "locked",
        "doc_id": "pattern-1",
        "consensus_count": 2,
        "signals": ["cortex", "autonomy"],
    }
    assert not store.updated
    assert not store.saved
