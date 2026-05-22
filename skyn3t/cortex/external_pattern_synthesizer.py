from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.cortex.external_pattern_synthesizer")

_QUERY_STOPWORDS = {
    "agent",
    "agents",
    "and",
    "app",
    "apps",
    "builder",
    "builders",
    "cli",
    "components",
    "design",
    "developer",
    "developers",
    "fit",
    "for",
    "game",
    "games",
    "memory",
    "multi",
    "orchestrator",
    "packaging",
    "project",
    "projects",
    "rag",
    "review",
    "testing",
    "the",
    "ui",
    "workflow",
}


class ExternalPatternSynthesizer:
    """Synthesize governed cross-repo patterns from approved external learning."""

    def __init__(self, memory_store: Any, *, scan_limit: int = 500):
        self.memory_store = memory_store
        self.scan_limit = scan_limit

    async def synthesize_for_doc(self, doc_id: str) -> Optional[Dict[str, Any]]:
        current = await self.memory_store.get_knowledge_doc(doc_id)
        if current is None:
            return None
        if not self._is_eligible_external_doc(current):
            return None

        meta = dict(current.get("meta") or {})
        lane = str(meta.get("lane") or "fit").strip() or "fit"
        language = str(meta.get("language") or "unknown").strip() or "unknown"
        synthesis_key = f"external-pattern:{lane.lower()}:{language.lower()}"

        docs = await self.memory_store.get_lessons(doc_type="external_learning", limit=self.scan_limit)
        grouped = self._group_docs(docs, lane=lane, language=language)
        if len(grouped) < 2:
            return {"status": "insufficient_sources", "doc_id": doc_id, "consensus_count": len(grouped)}

        signals = self._common_signals(grouped)
        if not signals:
            return {"status": "no_common_signals", "doc_id": doc_id, "consensus_count": len(grouped)}

        title = self._title_for(lane=lane, language=language, signals=signals)
        content = self._content_for(grouped, lane=lane, language=language, signals=signals)
        pattern_meta = self._meta_for(grouped, lane=lane, language=language, synthesis_key=synthesis_key, signals=signals)
        lesson_key = f"external-lesson:{lane.lower()}:{language.lower()}"
        eval_key = f"external-eval:{lane.lower()}:{language.lower()}"

        existing = await self.memory_store.find_knowledge_doc_by_meta(
            meta_key="synthesis_key",
            meta_value=synthesis_key,
            doc_type="pattern",
        )
        if existing is not None:
            existing_meta = dict(existing.get("meta") or {})
            if existing_meta.get("review_status") != "draft":
                return {
                    "status": "locked",
                    "doc_id": existing.get("id"),
                    "consensus_count": len(grouped),
                    "signals": signals,
                }
            updated_meta = dict(existing_meta)
            updated_meta.update(pattern_meta)
            updated = await self.memory_store.update_knowledge_doc(
                str(existing.get("id") or ""),
                title=title,
                content=content,
                meta=updated_meta,
            )
            return {
                "status": "updated",
                "doc_id": updated.get("id") if updated else existing.get("id"),
                "consensus_count": len(grouped),
                "signals": signals,
                "lesson": await self._upsert_lesson_asset(
                    grouped, lane=lane, language=language, signals=signals, synthesis_key=lesson_key
                ),
                "eval": await self._upsert_eval_asset(
                    grouped, lane=lane, language=language, signals=signals, synthesis_key=eval_key
                ),
            }

        pattern_doc_id = await self.memory_store.save_knowledge_doc(
            title=title,
            content=content,
            source="external_pattern_synthesizer",
            doc_type="pattern",
            meta=pattern_meta,
        )
        return {
            "status": "created",
            "doc_id": pattern_doc_id,
            "consensus_count": len(grouped),
            "signals": signals,
            "lesson": await self._upsert_lesson_asset(
                grouped, lane=lane, language=language, signals=signals, synthesis_key=lesson_key
            ),
            "eval": await self._upsert_eval_asset(
                grouped, lane=lane, language=language, signals=signals, synthesis_key=eval_key
            ),
        }

    def _group_docs(self, docs: List[Dict[str, Any]], *, lane: str, language: str) -> List[Dict[str, Any]]:
        grouped: List[Dict[str, Any]] = []
        seen_repos: set[str] = set()
        for doc in docs:
            if not self._is_eligible_external_doc(doc):
                continue
            meta = dict(doc.get("meta") or {})
            if (str(meta.get("lane") or "fit").strip() or "fit") != lane:
                continue
            if (str(meta.get("language") or "unknown").strip() or "unknown") != language:
                continue
            repo_key = str(meta.get("repo_key") or meta.get("repo") or doc.get("id") or "").strip().lower()
            if not repo_key or repo_key in seen_repos:
                continue
            seen_repos.add(repo_key)
            grouped.append(doc)
        return grouped

    @staticmethod
    def _is_eligible_external_doc(doc: Dict[str, Any]) -> bool:
        if str(doc.get("doc_type") or "") != "external_learning":
            return False
        meta = dict(doc.get("meta") or {})
        if meta.get("review_status") != "approved":
            return False
        if not meta.get("reusable"):
            return False
        if meta.get("external_doc_ingest_status") != "docs_ingested":
            return False
        if not list(meta.get("external_doc_paths_ingested") or []):
            return False
        return True

    def _common_signals(self, docs: List[Dict[str, Any]]) -> List[str]:
        topic_counts: Counter[str] = Counter()
        query_counts: Counter[str] = Counter()
        for doc in docs:
            meta = dict(doc.get("meta") or {})
            topic_counts.update(
                token for token in self._normalize_terms(meta.get("topics") or []) if token
            )
            query_counts.update(self._query_terms(str(meta.get("query") or "")))
        repeated_topics = [token for token, count in topic_counts.items() if count >= 2]
        repeated_query_terms = [token for token, count in query_counts.items() if count >= 2]
        signals = sorted(repeated_topics) + [term for term in sorted(repeated_query_terms) if term not in repeated_topics]
        return signals[:5]

    @staticmethod
    def _normalize_terms(values: List[Any]) -> List[str]:
        out: List[str] = []
        for value in values:
            text = str(value).strip().lower()
            if text:
                out.append(text)
        return out

    @staticmethod
    def _query_terms(query: str) -> List[str]:
        terms = []
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", query.lower()):
            if token not in _QUERY_STOPWORDS:
                terms.append(token)
        return terms

    @staticmethod
    def _title_for(*, lane: str, language: str, signals: List[str]) -> str:
        signal_label = ", ".join(signals[:2]) if signals else "shared signals"
        return f"External pattern: {lane} / {language} / {signal_label}"

    @staticmethod
    def _content_for(
        docs: List[Dict[str, Any]],
        *,
        lane: str,
        language: str,
        signals: List[str],
    ) -> str:
        repos: List[str] = []
        eval_targets: List[str] = []
        for doc in docs:
            meta = dict(doc.get("meta") or {})
            repo = str(meta.get("repo") or "unknown").strip() or "unknown"
            repos.append(repo)
            for path in list(meta.get("external_doc_paths_ingested") or [])[:2]:
                target = f"{repo}:{path}"
                if target not in eval_targets:
                    eval_targets.append(target)

        signal_text = ", ".join(signals)
        repo_text = ", ".join(repos)
        return (
            f"Pattern Name: external {lane} {language} pattern\n"
            f"Description: Across {len(repos)} approved external repos in the {lane} lane for {language}, "
            f"repeated signals point to {signal_text}. The strongest shared examples came from {repo_text}.\n"
            "Suggested Fix: Adapt the repeated external pattern into SkyN3t's own architecture, especially in "
            "Cortex/autonomy flows when relevant, and keep license/reuse constraints explicit during implementation.\n"
            "Evaluation Ideas:\n"
            f"- Build an eval that checks whether new work covers the repeated signals: {signal_text}.\n"
            f"- Review at least one source doc from each repo before promoting the pattern into a live skill.\n"
            f"- Spot-check these source docs: {', '.join(eval_targets[:6]) if eval_targets else 'none captured yet'}.\n"
            "Source Repos:\n"
            + "\n".join(f"- {repo}" for repo in repos)
        )

    @staticmethod
    def _confidence_for(consensus_count: int) -> float:
        if consensus_count >= 4:
            return 0.82
        if consensus_count == 3:
            return 0.74
        return 0.62

    def _meta_for(
        self,
        docs: List[Dict[str, Any]],
        *,
        lane: str,
        language: str,
        synthesis_key: str,
        signals: List[str],
    ) -> Dict[str, Any]:
        source_doc_ids = [str(doc.get("id") or "") for doc in docs if str(doc.get("id") or "").strip()]
        source_repos = [
            str((doc.get("meta") or {}).get("repo") or "").strip()
            for doc in docs
            if str((doc.get("meta") or {}).get("repo") or "").strip()
        ]
        source_platforms = sorted({
            str((doc.get("meta") or {}).get("source_platform") or "").strip()
            for doc in docs
            if str((doc.get("meta") or {}).get("source_platform") or "").strip()
        })
        return {
            "memory_layer": "project",
            "review_status": "draft",
            "provenance_status": "captured",
            "source_platform": "external",
            "reusable": True,
            "confidence": self._confidence_for(len(source_repos)),
            "external_pattern": True,
            "synthesis_key": synthesis_key,
            "consensus_count": len(source_repos),
            "source_doc_ids": source_doc_ids,
            "source_repos": source_repos,
            "source_platforms": source_platforms,
            "lane": lane,
            "language": language,
            "patterns": signals,
            "evaluation_ideas": [
                f"Check whether new work covers {', '.join(signals)}.",
                "Verify at least one approved source doc from each repo before promotion.",
            ],
        }

    async def _upsert_lesson_asset(
        self,
        docs: List[Dict[str, Any]],
        *,
        lane: str,
        language: str,
        signals: List[str],
        synthesis_key: str,
    ) -> Dict[str, Any]:
        title = f"External lesson: {lane} / {language} / {', '.join(signals[:2]) or 'shared signals'}"
        content = self._lesson_content_for(docs, lane=lane, language=language, signals=signals)
        meta = self._lesson_meta_for(docs, lane=lane, language=language, signals=signals, synthesis_key=synthesis_key)
        return await self._upsert_governed_doc(
            doc_type="lesson",
            synthesis_key=synthesis_key,
            title=title,
            content=content,
            meta=meta,
            source="external_pattern_synthesizer",
        )

    async def _upsert_eval_asset(
        self,
        docs: List[Dict[str, Any]],
        *,
        lane: str,
        language: str,
        signals: List[str],
        synthesis_key: str,
    ) -> Dict[str, Any]:
        title = f"External eval: {lane} / {language} / {', '.join(signals[:2]) or 'shared signals'}"
        content = self._eval_content_for(docs, lane=lane, language=language, signals=signals)
        meta = self._eval_meta_for(docs, lane=lane, language=language, signals=signals, synthesis_key=synthesis_key)
        return await self._upsert_governed_doc(
            doc_type="evaluation",
            synthesis_key=synthesis_key,
            title=title,
            content=content,
            meta=meta,
            source="external_pattern_synthesizer",
        )

    async def _upsert_governed_doc(
        self,
        *,
        doc_type: str,
        synthesis_key: str,
        title: str,
        content: str,
        meta: Dict[str, Any],
        source: str,
    ) -> Dict[str, Any]:
        existing = await self.memory_store.find_knowledge_doc_by_meta(
            meta_key="synthesis_key",
            meta_value=synthesis_key,
            doc_type=doc_type,
        )
        if existing is not None:
            existing_meta = dict(existing.get("meta") or {})
            if existing_meta.get("review_status") != "draft":
                return {
                    "status": "locked",
                    "doc_id": existing.get("id"),
                }
            updated_meta = dict(existing_meta)
            updated_meta.update(meta)
            updated = await self.memory_store.update_knowledge_doc(
                str(existing.get("id") or ""),
                title=title,
                content=content,
                meta=updated_meta,
            )
            return {
                "status": "updated",
                "doc_id": updated.get("id") if updated else existing.get("id"),
            }

        doc_id = await self.memory_store.save_knowledge_doc(
            title=title,
            content=content,
            source=source,
            doc_type=doc_type,
            meta=meta,
        )
        return {"status": "created", "doc_id": doc_id}

    @staticmethod
    def _lesson_content_for(
        docs: List[Dict[str, Any]],
        *,
        lane: str,
        language: str,
        signals: List[str],
    ) -> str:
        repos = [
            str((doc.get("meta") or {}).get("repo") or "unknown").strip() or "unknown"
            for doc in docs
        ]
        signal_text = ", ".join(signals)
        return (
            f"Lesson: external {lane} {language} lesson\n"
            f"Observation: Multiple approved external repos point to the same repeated signals: {signal_text}.\n"
            "Patterns:\n"
            + "\n".join(f"- {signal}" for signal in signals)
            + "\nSuggestions:\n"
            + "\n".join(
                [
                    f"- Reuse the shared {lane}/{language} pattern as inspiration, not implementation.",
                    "- Review at least one approved source doc from each repo before promoting this lesson.",
                    f"- Start with these source repos: {', '.join(repos)}.",
                ]
            )
        )

    def _lesson_meta_for(
        self,
        docs: List[Dict[str, Any]],
        *,
        lane: str,
        language: str,
        signals: List[str],
        synthesis_key: str,
    ) -> Dict[str, Any]:
        source_doc_ids = [str(doc.get("id") or "") for doc in docs if str(doc.get("id") or "").strip()]
        source_repos = [
            str((doc.get("meta") or {}).get("repo") or "").strip()
            for doc in docs
            if str((doc.get("meta") or {}).get("repo") or "").strip()
        ]
        confidence = 0.78 if len(source_repos) >= 4 else (0.71 if len(source_repos) >= 3 else 0.64)
        return {
            "memory_layer": "project",
            "review_status": "draft",
            "provenance_status": "captured",
            "source_platform": "external",
            "reusable": True,
            "confidence": confidence,
            "external_lesson": True,
            "synthesis_key": synthesis_key,
            "consensus_count": len(source_repos),
            "source_doc_ids": source_doc_ids,
            "source_repos": source_repos,
            "lane": lane,
            "language": language,
            "patterns": signals,
        }

    @staticmethod
    def _eval_content_for(
        docs: List[Dict[str, Any]],
        *,
        lane: str,
        language: str,
        signals: List[str],
    ) -> str:
        repo_paths: List[str] = []
        for doc in docs:
            meta = dict(doc.get("meta") or {})
            repo = str(meta.get("repo") or "unknown").strip() or "unknown"
            for path in list(meta.get("external_doc_paths_ingested") or [])[:2]:
                entry = f"{repo}:{path}"
                if entry not in repo_paths:
                    repo_paths.append(entry)
        signal_text = ", ".join(signals)
        return (
            f"Evaluation Asset: external {lane} {language}\n"
            f"Target Signals: {signal_text}\n"
            "Checks:\n"
            f"- Confirm the proposed work addresses these repeated external signals: {signal_text}.\n"
            "- Confirm at least two approved external repos were reviewed before adoption.\n"
            "- Confirm license/reuse-risk notes were surfaced to the operator.\n"
            "Source Docs:\n"
            + "\n".join(f"- {entry}" for entry in repo_paths[:8])
        )

    def _eval_meta_for(
        self,
        docs: List[Dict[str, Any]],
        *,
        lane: str,
        language: str,
        signals: List[str],
        synthesis_key: str,
    ) -> Dict[str, Any]:
        source_doc_ids = [str(doc.get("id") or "") for doc in docs if str(doc.get("id") or "").strip()]
        source_repos = [
            str((doc.get("meta") or {}).get("repo") or "").strip()
            for doc in docs
            if str((doc.get("meta") or {}).get("repo") or "").strip()
        ]
        signal_text = ", ".join(signals)
        checks = [
            f"Confirm the proposed work addresses these repeated external signals: {signal_text}.",
            "Confirm at least two approved external repos were reviewed before adoption.",
            "Confirm license/reuse-risk notes were surfaced to the operator.",
        ]
        return {
            "memory_layer": "project",
            "review_status": "draft",
            "provenance_status": "captured",
            "source_platform": "external",
            "reusable": False,
            "confidence": 0.6,
            "external_eval": True,
            "synthesis_key": synthesis_key,
            "consensus_count": len(source_repos),
            "source_doc_ids": source_doc_ids,
            "source_repos": source_repos,
            "lane": lane,
            "language": language,
            "patterns": signals,
            "checks": checks,
        }
