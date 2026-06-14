"""GitHub Ingestor Agent - fetches READMEs and key files from public repos and ingests them into RAG.

This agent searches GitHub, fetches text content from a curated/searched set of
repositories, chunks the content via the existing DocumentProcessor, and pushes
it into the RAG knowledge base so the swarm can learn from open-source projects.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType

logger = logging.getLogger("skyn3t.agents.github_ingestor")


# Binary / non-text extensions to skip outright
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a",
    ".mp3", ".mp4", ".mov", ".avi", ".wav", ".flac", ".ogg",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pyc", ".pyo", ".class", ".jar", ".war",
    ".db", ".sqlite", ".sqlite3",
    ".npy", ".npz", ".pkl", ".pickle", ".onnx", ".pt", ".pth", ".h5", ".safetensors",
}

# Map file extensions to RAG doc_type for syntax-aware chunking.
_EXT_TO_DOCTYPE = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".rst": "markdown",
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
}


def load_seeds(path: str = "data/seeds.yaml") -> List[Dict[str, Any]]:
    """Parse a seeds YAML file into a list of seed dicts.

    Returns an empty list (and logs a warning) if PyYAML is unavailable or the
    file is missing/malformed.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.warning("PyYAML not installed; load_seeds returning empty list")
        return []

    p = Path(path)
    if not p.exists():
        logger.warning("seeds file not found at %s", p)
        return []

    try:
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("failed to parse seeds yaml at %s: %s", p, e)
        return []

    if isinstance(data, dict):
        seeds = data.get("seeds", [])
    elif isinstance(data, list):
        seeds = data
    else:
        seeds = []

    if not isinstance(seeds, list):
        return []

    return [s for s in seeds if isinstance(s, dict) and s.get("repo")]


def _skip_counts(skipped: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in skipped:
        reason = str(item.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


class GitHubIngestorAgent(BaseAgent):
    """Agent that fetches public-repo content from GitHub and ingests it into RAG."""

    def __init__(
        self,
        name: str = "github_ingestor",
        *,
        github_token: Optional[str] = None,
        rag: Optional[Any] = None,
        seeds_path: str = "data/seeds.yaml",
        event_bus: Optional[EventBus] = None,
        config: Optional[Dict[str, Any]] = None,
        **kw: Any,
    ):
        # AgentCapability has no `from_string` helper in this codebase, so we
        # mirror the pattern used in github_explorer.py / research_agent.py.
        super().__init__(
            name=name,
            agent_type="github_ingestor",
            provider="github",
            event_bus=event_bus or EventBus(),
            config=config,
            **kw,
        )
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN")
        self.rag = rag
        self.seeds_path = seeds_path
        self._github_client: Any = None
        self._http_client: Any = None
        # Persistent SHA cache so re-running ingestion across the same seeds
        # doesn't burn one API call per (repo, path) when nothing has changed.
        # Format: {f"{repo}::{path}": {"sha": str, "content": str|null}}
        self._sha_cache_path = Path("data/.github_cache/sha_index.json")
        self._sha_cache: Dict[str, Dict[str, Any]] = {}
        try:
            self._sha_cache_path.parent.mkdir(parents=True, exist_ok=True)
            if self._sha_cache_path.exists():
                self._sha_cache = json.loads(self._sha_cache_path.read_text())
        except Exception:
            self._sha_cache = {}
        self.add_capability(
            AgentCapability(
                name="github_ingest",
                description="Fetch READMEs and key files from public GitHub repos and ingest them into RAG",
                parameters={
                    "mode": "str",
                    "query": "str",
                    "repo": "str",
                    "paths": "list",
                    "max_files": "int",
                    "max_bytes_per_file": "int",
                },
            )
        )
        self.add_capability(
            AgentCapability(
                name="research",
                description="Discover and surface relevant open-source projects for RAG ingestion",
            )
        )

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        """Initialize a GitHub client.

        For anonymous access we prefer the lightweight HTTP client so rate
        limits fail fast instead of triggering PyGithub's long backoff loop.
        """
        self._github_client = None
        self._http_client = None

        if self.github_token:
            try:
                from github import Github  # type: ignore

                self._github_client = Github(self.github_token)
                self.metadata["client"] = "pygithub"
                self.metadata["authenticated"] = True
            except ImportError:
                self._github_client = None
                logger.info(
                    "github_ingestor: PyGithub unavailable; falling back to httpx client"
                )

        if self._github_client is None:
            try:
                import httpx  # type: ignore

                headers = {"Accept": "application/vnd.github+json"}
                if self.github_token:
                    headers["Authorization"] = f"Bearer {self.github_token}"
                self._http_client = httpx.Client(
                    base_url="https://api.github.com",
                    headers=headers,
                    timeout=30.0,
                )
                self.metadata["client"] = "httpx"
                self.metadata["authenticated"] = bool(self.github_token)
            except ImportError:
                self._http_client = None
                logger.info(
                    "github_ingestor: httpx unavailable; trying anonymous PyGithub client"
                )

        if self._github_client is None and self._http_client is None:
            try:
                from github import Github  # type: ignore

                self._github_client = Github()
                self.metadata["client"] = "pygithub"
                self.metadata["authenticated"] = False
            except ImportError:
                self.metadata["client"] = None
                self.metadata["authenticated"] = False

        self.metadata["client_available"] = self._client_available()
        if not self.metadata["client_available"]:
            logger.warning(
                "github_ingestor: no GitHub client available; install httpx or PyGithub"
            )
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        """Health check: at least one client must be available."""
        return self._github_client is not None or self._http_client is not None

    async def shutdown(self) -> None:
        """Close any underlying HTTP client and shut down the base agent."""
        if self._http_client is not None:
            try:
                self._http_client.close()
            except Exception:
                pass
            self._http_client = None
        await super().shutdown()

    # ------------------------------------------------------------------ helpers

    def _client_available(self) -> bool:
        return self._github_client is not None or self._http_client is not None

    def _emit(self, event_name: str, payload: Dict[str, Any]) -> None:
        """Publish an INGEST_* event if the EventType member exists."""
        if self.event_bus is None:
            return
        et = getattr(EventType, event_name, None)
        if et is None:
            return
        try:
            self.event_bus.publish(
                Event(event_type=et, source=self.name, payload=payload)
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("failed to publish %s: %s", event_name, e)

    async def _think(self, line: str) -> None:
        """Stream a thinking line if BaseAgent provides ``think``."""
        if hasattr(self, "think"):
            try:
                await self.think(line)  # type: ignore[attr-defined]
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("think() failed: %s", e)

    @staticmethod
    def _is_binary_path(path: str) -> bool:
        ext = Path(path).suffix.lower()
        return ext in _BINARY_EXTS

    @staticmethod
    def _doctype_for(path: str) -> str:
        ext = Path(path).suffix.lower()
        return _EXT_TO_DOCTYPE.get(ext, "text")

    @staticmethod
    def _path_matches(path: str, prefixes: List[str]) -> bool:
        """Return True if ``path`` is under any of the configured prefixes."""
        if not prefixes:
            return True
        for prefix in prefixes:
            pfx = prefix.rstrip("/")
            if not pfx:
                continue
            if path == pfx:
                return True
            if pfx.endswith("/") or "/" in pfx or "." not in Path(pfx).name:
                # treat as directory prefix
                if path.startswith(pfx + "/") or path.startswith(pfx):
                    if path == pfx or path.startswith(pfx + "/"):
                        return True
            else:
                if path == pfx:
                    return True
        return False

    # ------------------------------------------------------------------ fetching

    async def _search_repos(self, query: str, limit: int) -> List[str]:
        """Search GitHub for repos matching ``query``; return owner/name strings."""
        limit = max(1, int(limit))
        if self._github_client is not None:
            def _do_search() -> List[str]:
                results = self._github_client.search_repositories(query)
                names: List[str] = []
                for repo in results[:limit]:
                    names.append(repo.full_name)
                return names

            try:
                return await asyncio.to_thread(_do_search)
            except Exception as e:
                logger.warning("PyGithub search failed: %s", e)
                return []

        if self._http_client is not None:
            def _do_http_search() -> List[str]:
                resp = self._http_client.get(
                    "/search/repositories",
                    params={"q": query, "per_page": limit},
                )
                if resp.status_code == 403:
                    raise RuntimeError("github rate limit (403)")
                resp.raise_for_status()
                items = resp.json().get("items", [])
                return [item["full_name"] for item in items[:limit]]

            try:
                return await asyncio.to_thread(_do_http_search)
            except Exception as e:
                logger.warning("httpx search failed: %s", e)
                return []

        return []

    async def _list_repo_files(
        self, repo_full_name: str, path_prefixes: List[str], remaining: int
    ) -> List[Tuple[str, int]]:
        """List candidate files in a repo limited by ``path_prefixes``.

        Returns a list of ``(path, size)`` tuples (size may be 0 if unknown).
        """
        if remaining <= 0:
            return []

        # Treat each prefix independently: an exact filename is fetched
        # directly; a directory prefix triggers a tree walk.
        paths_out: List[Tuple[str, int]] = []
        seen: set = set()

        # Build the list of "roots" to enumerate.
        if not path_prefixes:
            path_prefixes = ["README.md"]

        for prefix in path_prefixes:
            if remaining - len(paths_out) <= 0:
                break
            pfx = prefix.strip().rstrip("/")
            is_dir = pfx == "" or pfx.endswith("/") or "." not in Path(pfx).name

            try:
                if is_dir:
                    found = await self._walk_directory(
                        repo_full_name, pfx, remaining - len(paths_out)
                    )
                else:
                    found = await self._stat_file(repo_full_name, pfx)
            except Exception as e:
                logger.debug("listing failed for %s:%s: %s", repo_full_name, pfx, e)
                continue

            for p, size in found:
                if p in seen:
                    continue
                if self._is_binary_path(p):
                    continue
                seen.add(p)
                paths_out.append((p, size))
                if len(paths_out) >= remaining:
                    break

        return paths_out

    async def _stat_file(
        self, repo_full_name: str, path: str
    ) -> List[Tuple[str, int]]:
        """Confirm a single file exists and return [(path, size)]."""
        if self._github_client is not None:
            def _do_stat() -> List[Tuple[str, int]]:
                repo = self._github_client.get_repo(repo_full_name)
                content = repo.get_contents(path)
                if isinstance(content, list):
                    return []
                return [(content.path, getattr(content, "size", 0) or 0)]

            return await asyncio.to_thread(_do_stat)

        if self._http_client is not None:
            def _do_http_stat() -> List[Tuple[str, int]]:
                resp = self._http_client.get(
                    f"/repos/{repo_full_name}/contents/{path}"
                )
                if resp.status_code == 403:
                    raise RuntimeError("github rate limit (403)")
                if resp.status_code == 404:
                    return []
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    return []
                return [(data.get("path", path), int(data.get("size") or 0))]

            return await asyncio.to_thread(_do_http_stat)

        return []

    async def _walk_directory(
        self, repo_full_name: str, dir_path: str, limit: int
    ) -> List[Tuple[str, int]]:
        """Walk a directory (BFS, max 200 entries scanned) up to ``limit`` files."""
        limit = max(1, int(limit))

        if self._github_client is not None:
            def _do_walk() -> List[Tuple[str, int]]:
                repo = self._github_client.get_repo(repo_full_name)
                out: List[Tuple[str, int]] = []
                queue: List[str] = [dir_path]
                scanned = 0
                while queue and len(out) < limit and scanned < 200:
                    cur = queue.pop(0)
                    try:
                        contents = repo.get_contents(cur)
                    except Exception:
                        continue
                    if not isinstance(contents, list):
                        contents = [contents]
                    for c in contents:
                        scanned += 1
                        if c.type == "dir":
                            queue.append(c.path)
                        else:
                            out.append((c.path, getattr(c, "size", 0) or 0))
                            if len(out) >= limit:
                                break
                return out

            return await asyncio.to_thread(_do_walk)

        if self._http_client is not None:
            def _do_http_walk() -> List[Tuple[str, int]]:
                out: List[Tuple[str, int]] = []
                queue: List[str] = [dir_path]
                scanned = 0
                while queue and len(out) < limit and scanned < 200:
                    cur = queue.pop(0)
                    resp = self._http_client.get(
                        f"/repos/{repo_full_name}/contents/{cur}"
                    )
                    if resp.status_code == 403:
                        raise RuntimeError("github rate limit (403)")
                    if resp.status_code == 404:
                        continue
                    if resp.status_code >= 400:
                        continue
                    data = resp.json()
                    if not isinstance(data, list):
                        data = [data]
                    for c in data:
                        scanned += 1
                        if c.get("type") == "dir":
                            queue.append(c.get("path"))
                        elif c.get("type") == "file":
                            out.append(
                                (c.get("path"), int(c.get("size") or 0))
                            )
                            if len(out) >= limit:
                                break
                return out

            return await asyncio.to_thread(_do_http_walk)

        return []

    def _cache_key(self, repo_full_name: str, path: str) -> str:
        return f"{repo_full_name}::{path}"

    def _cache_get(self, repo_full_name: str, path: str) -> Optional[Tuple[str, Optional[str]]]:
        entry = self._sha_cache.get(self._cache_key(repo_full_name, path))
        if not entry:
            return None
        sha = entry.get("sha")
        if not isinstance(sha, str):
            return None
        content = entry.get("content")
        if content is not None and not isinstance(content, str):
            content = None
        return sha, content

    def _cache_put(
        self, repo_full_name: str, path: str, sha: str, content: Optional[str]
    ) -> None:
        self._sha_cache[self._cache_key(repo_full_name, path)] = {
            "sha": sha, "content": content,
        }
        try:
            tmp = self._sha_cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._sha_cache, indent=2, sort_keys=True))
            os.replace(tmp, self._sha_cache_path)
        except Exception:
            # Losing the SHA cache means next ingest re-fetches every
            # file we've already seen — slow but not incorrect. Worth
            # warning so a permanently-broken disk surfaces.
            logger.warning(
                "github_ingestor: failed to persist SHA cache to %s — "
                "next ingest will re-fetch everything",
                self._sha_cache_path, exc_info=True,
            )

    async def _fetch_file_content(
        self, repo_full_name: str, path: str, max_bytes: int
    ) -> Optional[str]:
        """Fetch and decode a file's text content. Returns None for binary/oversized.

        Uses a (repo, path) → sha SHA cache so unchanged files don't re-download:
        we still pay the contents-API call, but skip the base64 decode + write
        when the sha matches and we already have content for that sha.
        """
        if self._github_client is not None:
            cached = self._cache_get(repo_full_name, path)
            def _do_fetch() -> Optional[str]:
                repo = self._github_client.get_repo(repo_full_name)
                content = repo.get_contents(path)
                if isinstance(content, list):
                    return None
                size = getattr(content, "size", 0) or 0
                if max_bytes and size > max_bytes:
                    return None
                sha = getattr(content, "sha", "") or ""
                if cached and cached[0] == sha and cached[1] is not None:
                    return cached[1]
                raw = content.content or ""
                try:
                    decoded = base64.b64decode(raw)
                except Exception:
                    return None
                if max_bytes and len(decoded) > max_bytes:
                    decoded = decoded[:max_bytes]
                try:
                    text = decoded.decode("utf-8")
                except UnicodeDecodeError:
                    self._cache_put(repo_full_name, path, sha, None)
                    return None
                self._cache_put(repo_full_name, path, sha, text)
                return text

            return await asyncio.to_thread(_do_fetch)

        if self._http_client is not None:
            cached = self._cache_get(repo_full_name, path)
            def _do_http_fetch() -> Optional[str]:
                resp = self._http_client.get(
                    f"/repos/{repo_full_name}/contents/{path}"
                )
                if resp.status_code == 403:
                    raise RuntimeError("github rate limit (403)")
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    return None
                size = int(data.get("size") or 0)
                if max_bytes and size > max_bytes:
                    return None
                sha = str(data.get("sha") or "")
                if cached and cached[0] == sha and cached[1] is not None:
                    return cached[1]
                raw = data.get("content") or ""
                if data.get("encoding") == "base64":
                    try:
                        decoded = base64.b64decode(raw)
                    except Exception:
                        return None
                else:
                    decoded = raw.encode("utf-8")
                if max_bytes and len(decoded) > max_bytes:
                    decoded = decoded[:max_bytes]
                try:
                    text = decoded.decode("utf-8")
                except UnicodeDecodeError:
                    self._cache_put(repo_full_name, path, sha, None)
                    return None
                self._cache_put(repo_full_name, path, sha, text)
                return text

            return await asyncio.to_thread(_do_http_fetch)

        return None

    # ------------------------------------------------------------------ ingest core

    async def _ingest_repo(
        self,
        repo_full_name: str,
        path_prefixes: List[str],
        max_files: int,
        max_bytes_per_file: int,
        ingested: List[Dict[str, Any]],
        skipped: List[Dict[str, Any]],
        why: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> None:
        """Fetch and ingest files from one repo, mutating ingested/skipped in place."""
        remaining = max_files - len(ingested)
        if remaining <= 0:
            return

        await self._think(f"discovering files in {repo_full_name}")
        try:
            candidates = await self._list_repo_files(
                repo_full_name, path_prefixes, remaining
            )
        except RuntimeError as e:
            if "rate limit" in str(e).lower():
                skipped.append(
                    {"repo": repo_full_name, "path": "*", "reason": "rate_limited"}
                )
                raise
            skipped.append(
                {"repo": repo_full_name, "path": "*", "reason": str(e)}
            )
            return
        except Exception as e:
            skipped.append(
                {"repo": repo_full_name, "path": "*", "reason": f"list_failed: {e}"}
            )
            return

        if not candidates:
            skipped.append(
                {"repo": repo_full_name, "path": "*", "reason": "no_files_found"}
            )
            return

        for path, _size in candidates:
            if len(ingested) >= max_files:
                break

            if self._is_binary_path(path):
                skipped.append(
                    {"repo": repo_full_name, "path": path, "reason": "binary"}
                )
                continue

            try:
                text = await self._fetch_file_content(
                    repo_full_name, path, max_bytes_per_file
                )
            except RuntimeError as e:
                if "rate limit" in str(e).lower():
                    skipped.append(
                        {"repo": repo_full_name, "path": path, "reason": "rate_limited"}
                    )
                    raise
                skipped.append(
                    {"repo": repo_full_name, "path": path, "reason": str(e)}
                )
                continue
            except Exception as e:
                skipped.append(
                    {"repo": repo_full_name, "path": path, "reason": f"fetch_failed: {e}"}
                )
                continue

            if text is None:
                skipped.append(
                    {"repo": repo_full_name, "path": path, "reason": "binary_or_oversize"}
                )
                continue

            byte_count = len(text.encode("utf-8", errors="ignore"))
            embedding_id: Optional[str] = None
            if self.rag is not None:
                doc_type = self._doctype_for(path)
                meta = {
                    "repo": repo_full_name,
                    "path": path,
                    "github_url": f"https://github.com/{repo_full_name}/blob/HEAD/{path}",
                    "ingestor": self.name,
                }
                if why:
                    meta["why"] = why
                if kind:
                    meta["kind"] = kind
                try:
                    embedding_id = await self.rag.add_knowledge_one(
                        content=text,
                        title=f"{repo_full_name}/{path}",
                        source=f"github:{repo_full_name}/{path}",
                        doc_type=doc_type,
                        metadata=meta,
                    )
                except Exception as e:
                    skipped.append(
                        {"repo": repo_full_name, "path": path, "reason": f"rag_failed: {e}"}
                    )
                    continue

            entry = {
                "repo": repo_full_name,
                "path": path,
                "embedding_id": embedding_id,
                "bytes": byte_count,
            }
            ingested.append(entry)
            self._emit(
                "INGEST_PROGRESS",
                {
                    "repo": repo_full_name,
                    "path": path,
                    "embedding_id": embedding_id,
                    "bytes": byte_count,
                    "ingested_count": len(ingested),
                    "max_files": max_files,
                },
            )
            await self._think(
                f"ingested {repo_full_name}/{path} ({byte_count} bytes)"
            )

    # ------------------------------------------------------------------ entrypoint

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        """Execute a github_ingest task."""
        # Lazy initialize so the agent works outside of orchestrator.start().
        if not self.metadata.get("initialized"):
            try:
                await self.initialize()
            except Exception as e:
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error=f"initialization failed: {e}",
                )

        if not self._client_available():
            self.metadata["last_status"] = "missing_client"
            logger.warning("github_ingestor: cannot run because no GitHub client is available")
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error="github client not available",
                metadata={"status": "missing_client"},
            )

        data = task.input_data or {}
        mode = (data.get("mode") or "seed_list").lower()
        paths = data.get("paths") or ["README.md"]
        if isinstance(paths, str):
            paths = [paths]
        max_files = int(data.get("max_files", 20))
        max_bytes_per_file = int(data.get("max_bytes_per_file", 200_000))
        rag_available = self.rag is not None
        self.metadata["rag_available"] = rag_available
        if not rag_available:
            self.metadata["last_status"] = "missing_rag"
            logger.warning(
                "github_ingestor: RAG unavailable; fetched GitHub content will not be stored"
            )

        ingested: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        # Build the list of (repo, paths, why) triples to process.
        plan: List[Tuple[str, List[str], Optional[str], Optional[str]]] = []
        if mode == "seed_list":
            seeds = load_seeds(self.seeds_path)
            if not seeds:
                self.metadata["last_status"] = "missing_seeds"
                logger.warning(
                    "github_ingestor: no usable GitHub ingest seeds found at %s",
                    self.seeds_path,
                )
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error=f"no seeds available at {self.seeds_path}",
                    metadata={"status": "missing_seeds", "seeds_path": self.seeds_path},
                )
            for seed in seeds:
                repo = seed.get("repo")
                if not repo:
                    continue
                seed_paths = seed.get("paths") or ["README.md"]
                if isinstance(seed_paths, str):
                    seed_paths = [seed_paths]
                plan.append((repo, list(seed_paths), seed.get("why"), seed.get("kind")))
        elif mode == "single_repo":
            repo = data.get("repo")
            if not repo:
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error="single_repo mode requires 'repo' (owner/name)",
                )
            plan.append((repo, list(paths), data.get("why"), data.get("kind")))
        elif mode == "search":
            query = data.get("query")
            if not query:
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error="search mode requires 'query'",
                )
            search_limit = int(data.get("search_limit", 5))
            try:
                repos = await self._search_repos(query, search_limit)
            except Exception as e:
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error=f"search failed: {e}",
                )
            if not repos:
                status = "no_search_results_missing_rag" if not rag_available else "no_search_results"
                self.metadata["last_status"] = status
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={
                        "ingested": [],
                        "skipped": [],
                        "skip_counts": {},
                        "rate_limited": False,
                        "rag_available": rag_available,
                        "summary": "Ingested 0 files from 0 repos (no search results)",
                    },
                    metadata={"status": status, "rag_available": rag_available},
                )
            for repo in repos:
                plan.append((repo, list(paths), f"search:{query}", data.get("kind")))
        else:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"unknown mode: {mode}",
            )

        self._emit(
            "INGEST_STARTED",
            {
                "mode": mode,
                "repo_count": len(plan),
                "max_files": max_files,
                "max_bytes_per_file": max_bytes_per_file,
            },
        )
        await self._think(
            f"starting github ingest mode={mode} repos={len(plan)} cap={max_files}"
        )

        rate_limited = False
        repos_touched: set = set()
        for repo, repo_paths, why, seed_kind in plan:
            if len(ingested) >= max_files:
                break
            try:
                await self._ingest_repo(
                    repo_full_name=repo,
                    path_prefixes=repo_paths,
                    max_files=max_files,
                    max_bytes_per_file=max_bytes_per_file,
                    ingested=ingested,
                    skipped=skipped,
                    why=why,
                    kind=seed_kind,
                )
                repos_touched.add(repo)
            except RuntimeError as e:
                if "rate limit" in str(e).lower():
                    rate_limited = True
                    logger.warning(
                        "github_ingestor: aborting ingest due to GitHub rate limit at repo=%s",
                        repo,
                    )
                    break
                logger.warning("ingest failed for %s: %s", repo, e)
                skipped.append(
                    {"repo": repo, "path": "*", "reason": str(e)}
                )
            except Exception as e:
                logger.exception("unexpected ingest failure for %s", repo)
                skipped.append(
                    {"repo": repo, "path": "*", "reason": f"unexpected: {e}"}
                )

        summary = (
            f"Ingested {len(ingested)} files from "
            f"{len({e['repo'] for e in ingested})} repos"
        )
        if rate_limited:
            summary += " (rate-limited; partial)"
        if not rag_available:
            summary += " (RAG unavailable; not stored)"

        skip_counts = _skip_counts(skipped)
        if skipped:
            logger.info(
                "github_ingestor: skipped %d files/repo entries: %s",
                len(skipped),
                skip_counts,
            )
        if rate_limited:
            self.metadata["last_status"] = "rate_limited"
        elif not rag_available:
            self.metadata["last_status"] = "completed_missing_rag"
        else:
            self.metadata["last_status"] = "completed"

        self._emit(
            "INGEST_COMPLETE",
            {
                "ingested": len(ingested),
                "skipped": len(skipped),
                "rate_limited": rate_limited,
                "skip_counts": skip_counts,
                "rag_available": rag_available,
                "summary": summary,
            },
        )
        await self._think(summary)

        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "ingested": ingested,
                "skipped": skipped,
                "skip_counts": skip_counts,
                "rate_limited": rate_limited,
                "rag_available": rag_available,
                "summary": summary,
            },
            metadata={
                "status": self.metadata["last_status"],
                "rag_available": rag_available,
                "rate_limited": rate_limited,
            },
        )
