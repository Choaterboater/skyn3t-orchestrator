from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from skyn3t.rag.rag_engine import RAGEngine

logger = logging.getLogger("skyn3t.cortex.external_repo_ingest")

_DEFAULT_DOC_PATHS = (
    "README.md",
    "README.rst",
    "docs/index.md",
    "docs/README.md",
)
_DEFAULT_SEEN_HASHES_PATH = Path("data/.external_repo_ingest_seen.json")
_MAX_DOC_BYTES = 200_000
_MIN_TEXT_CHARS = 40
_SEEN_LOCK = asyncio.Lock()


class ExternalRepoDocIngestor:
    """Best-effort public docs ingest for approved external repo proposals."""

    def __init__(
        self,
        *,
        memory_store: Any,
        rag_engine: Optional[RAGEngine] = None,
        seen_hashes_path: Optional[Path] = None,
        doc_paths: Optional[List[str]] = None,
        max_bytes_per_file: int = _MAX_DOC_BYTES,
    ):
        self.memory_store = memory_store
        self.rag = rag_engine
        self.seen_hashes_path = seen_hashes_path or _DEFAULT_SEEN_HASHES_PATH
        self.doc_paths = tuple(doc_paths or _DEFAULT_DOC_PATHS)
        self.max_bytes_per_file = max_bytes_per_file

    async def ingest_repo_approval(
        self,
        *,
        platform: str,
        repo: str,
        repo_url: str,
        lane: str,
        query: str,
        description: str,
        language: str,
        license_name: str,
        reuse_risk: str,
        selection_reason: str,
        topics: List[str],
        stars: int,
    ) -> Dict[str, Any]:
        file_result = await self._ingest_public_docs(
            platform=platform,
            repo=repo,
            repo_url=repo_url,
            lane=lane,
            query=query,
            language=language,
            license_name=license_name,
            reuse_risk=reuse_risk,
            selection_reason=selection_reason,
            topics=topics,
            stars=stars,
        )
        summary_content = self._build_summary_content(
            platform=platform,
            repo=repo,
            repo_url=repo_url,
            lane=lane,
            query=query,
            description=description,
            language=language,
            license_name=license_name,
            reuse_risk=reuse_risk,
            selection_reason=selection_reason,
            topics=topics,
            stars=stars,
            ingested_paths=[item["path"] for item in file_result["ingested"]],
            warnings=list(file_result["warnings"]),
        )
        summary_embedding_id = await self._ingest_summary_doc(
            platform=platform,
            repo=repo,
            summary_content=summary_content,
            repo_url=repo_url,
            lane=lane,
            query=query,
            language=language,
            license_name=license_name,
            reuse_risk=reuse_risk,
            selection_reason=selection_reason,
            topics=topics,
        )

        confidence = 0.82 if file_result["ingested"] else (0.78 if reuse_risk == "low" else 0.68)
        ingest_status = "docs_ingested" if file_result["ingested"] else "summary_only"
        reviewed_at = datetime.now(timezone.utc).isoformat()
        meta = {
            "memory_layer": "project",
            "review_status": "approved",
            "reviewed_by": "cortex:proposal",
            "reviewed_at": reviewed_at,
            "approved_at": reviewed_at,
            "provenance_status": "approved",
            "source_platform": platform,
            "repo": repo,
            "repo_url": repo_url,
            "repo_key": self._repo_key(platform=platform, repo=repo, repo_url=repo_url),
            "lane": lane,
            "query": query,
            "language": language,
            "license": license_name,
            "reuse_risk": reuse_risk,
            "topics": topics,
            "selection_reason": selection_reason,
            "reusable": True,
            "confidence": confidence,
            "external_doc_ingest_status": ingest_status,
            "external_doc_attempted_paths": list(self.doc_paths),
            "external_doc_paths_ingested": [item["path"] for item in file_result["ingested"]],
            "external_doc_warnings": list(file_result["warnings"]),
        }
        doc_id = await self.memory_store.save_knowledge_doc(
            title=f"External learning: {platform.title()} repo {repo or repo_url or 'candidate'}",
            content=summary_content,
            source=f"repo_scout:{platform}",
            doc_type="external_learning",
            embedding_id=summary_embedding_id,
            meta=meta,
        )
        return {
            "doc_id": doc_id,
            "summary_embedding_id": summary_embedding_id,
            "ingested_count": len(file_result["ingested"]),
            "ingested_paths": [item["path"] for item in file_result["ingested"]],
            "warnings": list(file_result["warnings"]),
        }

    async def _ingest_public_docs(
        self,
        *,
        platform: str,
        repo: str,
        repo_url: str,
        lane: str,
        query: str,
        language: str,
        license_name: str,
        reuse_risk: str,
        selection_reason: str,
        topics: List[str],
        stars: int,
    ) -> Dict[str, Any]:
        if platform not in {"github", "gitlab", "bitbucket"}:
            return {"ingested": [], "warnings": [f"docs ingest not supported for {platform}"]}
        rag = await self._ensure_rag()
        if rag is None:
            return {"ingested": [], "warnings": ["rag unavailable; stored summary only"]}

        seen_hashes = await self._load_seen_hashes()
        ingested: List[Dict[str, Any]] = []
        warnings: List[str] = []

        async with httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": "SkyN3t-ExternalRepoIngest/1.0"},
        ) as client:
            for path in self.doc_paths:
                raw_url = self._raw_url(platform=platform, repo=repo, path=path)
                text, warning = await self._fetch_path_text(
                    client,
                    url=raw_url,
                    max_bytes=self.max_bytes_per_file,
                )
                if text is None:
                    if warning:
                        warnings.append(f"{path}: {warning}")
                    continue
                content_hash = self._hash(f"{platform}:{repo}:{path}:{text}")
                if content_hash in seen_hashes:
                    warnings.append(f"{path}: duplicate content skipped")
                    continue
                try:
                    embedding_id = await rag.add_knowledge_one(
                        content=text,
                        title=f"{platform}:{repo}/{path}",
                        source=f"{platform}:{repo}/{path}",
                        doc_type=self._doc_type_for(path),
                        metadata={
                            "source_platform": platform,
                            "repo": repo,
                            "repo_url": repo_url,
                            "path": path,
                            "raw_url": raw_url,
                            "lane": lane,
                            "query": query,
                            "language": language,
                            "license": license_name,
                            "reuse_risk": reuse_risk,
                            "selection_reason": selection_reason,
                            "topics": topics,
                            "stars": stars,
                            "provenance_status": "approved",
                            "kind": "external-repo-doc",
                        },
                    )
                except Exception as exc:
                    logger.warning("external repo ingest failed for %s %s: %s", repo, path, exc)
                    warnings.append(f"{path}: rag_failed")
                    continue
                seen_hashes.add(content_hash)
                ingested.append(
                    {
                        "path": path,
                        "embedding_id": embedding_id,
                        "chars": len(text),
                    }
                )
        await self._persist_seen_hashes(seen_hashes)
        return {"ingested": ingested, "warnings": warnings}

    async def _ingest_summary_doc(
        self,
        *,
        platform: str,
        repo: str,
        summary_content: str,
        repo_url: str,
        lane: str,
        query: str,
        language: str,
        license_name: str,
        reuse_risk: str,
        selection_reason: str,
        topics: List[str],
    ) -> Optional[str]:
        rag = await self._ensure_rag()
        if rag is None:
            return None
        seen_hashes = await self._load_seen_hashes()
        summary_hash = self._hash(f"summary:{platform}:{repo}:{summary_content}")
        if summary_hash in seen_hashes:
            return None
        try:
            embedding_id = await rag.add_knowledge_one(
                content=summary_content,
                title=f"External learning summary: {platform}:{repo}",
                source=f"repo_scout:{platform}:{repo}",
                doc_type="text",
                metadata={
                    "source_platform": platform,
                    "repo": repo,
                    "repo_url": repo_url,
                    "lane": lane,
                    "query": query,
                    "language": language,
                    "license": license_name,
                    "reuse_risk": reuse_risk,
                    "selection_reason": selection_reason,
                    "topics": topics,
                    "kind": "external-learning-summary",
                    "provenance_status": "approved",
                },
            )
        except Exception as exc:
            logger.warning("external learning summary ingest failed for %s: %s", repo, exc)
            return None
        seen_hashes.add(summary_hash)
        await self._persist_seen_hashes(seen_hashes)
        return embedding_id

    async def _ensure_rag(self) -> Optional[RAGEngine]:
        if self.rag is not None:
            try:
                await self.rag.initialize()
            except Exception:
                logger.warning("existing rag initialize failed", exc_info=True)
                return None
            return self.rag
        try:
            self.rag = RAGEngine()
            await self.rag.initialize()
            return self.rag
        except Exception:
            logger.warning("could not initialize rag for external repo ingest", exc_info=True)
            return None

    async def _fetch_path_text(
        self,
        client: httpx.AsyncClient,
        *,
        url: str,
        max_bytes: int,
    ) -> Tuple[Optional[str], str]:
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            return None, f"fetch_failed:{type(exc).__name__}"
        if response.status_code == 404:
            return None, "not_found"
        if response.status_code != 200:
            return None, f"http_{response.status_code}"
        content_type = str(response.headers.get("content-type") or "").lower()
        if any(marker in content_type for marker in ("text/html", "application/json", "application/xml", "text/xml")):
            return None, "unexpected_content_type"
        raw = response.content[:max_bytes]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "decode_failed"
        if "\x00" in text:
            return None, "binary"
        text = text.strip()
        if len(text) < _MIN_TEXT_CHARS:
            return None, "too_short"
        return text, ""

    @staticmethod
    def _raw_url(*, platform: str, repo: str, path: str) -> str:
        if platform == "github":
            return f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
        if platform == "gitlab":
            return f"https://gitlab.com/{repo}/-/raw/HEAD/{path}"
        if platform == "bitbucket":
            return f"https://bitbucket.org/{repo}/raw/HEAD/{path}"
        raise ValueError(f"unsupported platform for raw docs: {platform}")

    @staticmethod
    def _build_summary_content(
        *,
        platform: str,
        repo: str,
        repo_url: str,
        lane: str,
        query: str,
        description: str,
        language: str,
        license_name: str,
        reuse_risk: str,
        selection_reason: str,
        topics: List[str],
        stars: int,
        ingested_paths: List[str],
        warnings: List[str],
    ) -> str:
        lines = [
            f"Source platform: {platform}",
            f"Repository: {repo or 'unknown'}",
            f"URL: {repo_url or 'unknown'}",
            f"Lane: {lane}",
            f"Query: {query or 'none'}",
            f"Language: {language or 'unknown'}",
            f"Stars: {stars}",
            f"License: {license_name}",
            f"Reuse risk: {reuse_risk}",
            f"Topics: {', '.join(topics) if topics else 'none'}",
            f"Why selected: {selection_reason}",
            "",
            "Description:",
            description or "No description",
            "",
        ]
        if ingested_paths:
            lines.extend(["Approved docs ingested:", *[f"- {path}" for path in ingested_paths], ""])
        else:
            lines.extend(["Approved docs ingested: none", ""])
        if warnings:
            lines.extend(["Ingest warnings:", *[f"- {warning}" for warning in warnings], ""])
        lines.append(
            "This record captures external inspiration with attribution. "
            "It does not import repository code automatically."
        )
        return "\n".join(lines)

    @staticmethod
    def _doc_type_for(path: str) -> str:
        lower = path.lower()
        if lower.endswith((".md", ".markdown", ".rst")):
            return "markdown"
        return "text"

    @staticmethod
    def _repo_key(*, platform: str, repo: str, repo_url: str) -> str:
        normalized = repo_url.strip().lower().rstrip("/")
        if normalized:
            return normalized
        return f"{platform}:{repo.strip().lower()}"

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    async def _load_seen_hashes(self) -> set[str]:
        async with _SEEN_LOCK:
            try:
                if self.seen_hashes_path.exists():
                    data = json.loads(self.seen_hashes_path.read_text())
                    if isinstance(data, list):
                        return {str(item) for item in data}
            except Exception:
                logger.warning("external repo ingest seen-hash load failed", exc_info=True)
            return set()

    async def _persist_seen_hashes(self, seen_hashes: set[str]) -> None:
        async with _SEEN_LOCK:
            try:
                self.seen_hashes_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.seen_hashes_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(sorted(seen_hashes)))
                os.replace(tmp, self.seen_hashes_path)
            except Exception:
                logger.warning("external repo ingest seen-hash persist failed", exc_info=True)
