from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from skyn3t.cortex import get_store

logger = logging.getLogger("skyn3t.cortex.repo_scout")

# Process-age reference for the scout boot grace (module import ≈ boot).
_MODULE_T0 = time.monotonic()


def _env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default

_DEFAULT_FIT_QUERIES = [
    "multi agent orchestrator cli memory rag",
    "cortex autonomy self-healing proposal review agent learning",
    "design system ui components app builder",
    "game framework rendering ui workflow",
    "developer workflow automation testing packaging",
]
_DEFAULT_PLATFORMS = ["github", "gitlab", "bitbucket"]


def default_fit_queries() -> List[str]:
    """Configured via SKYN3T_CORTEX_SCOUT_FIT_QUERIES; falls back to built-ins."""
    try:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        configured = [
            str(item).strip()
            for item in (settings.cortex_scout_fit_queries or [])
            if str(item).strip()
        ]
        base = configured if configured else list(_DEFAULT_FIT_QUERIES)
        if getattr(settings, "cortex_scout_include_competitive_queries", True):
            from skyn3t.cortex.competitive_intel import merge_scout_fit_queries

            return merge_scout_fit_queries(base)
        return base
    except Exception:
        pass
    return list(_DEFAULT_FIT_QUERIES)
_PERMISSIVE_LICENSE_MARKERS = ("mit", "apache", "bsd", "isc", "unlicense", "zlib", "mpl-2.0")
_RESTRICTIVE_LICENSE_MARKERS = ("gpl", "agpl", "lgpl", "sspl", "commons clause")
# Boost repos whose metadata looks relevant to SkyN3t's mission (agents, RAG, cortex).
_RELEVANCE_TERMS = (
    "agent",
    "orchestrat",
    "multi-agent",
    "rag",
    "retriev",
    "cortex",
    "llm",
    "mcp",
    "self-heal",
    "proposal",
    "memory",
    "workflow",
    "autonom",
)


@dataclass
class _ScoutCandidate:
    platform: str
    lane: str
    query: str
    full_name: str
    description: str
    stars: int
    language: str
    url: str
    license_name: str = ""
    topics: List[str] = field(default_factory=list)


class MultiSourceRepoScout:
    """Scout external repos and file review-gated proposals."""

    def __init__(self, orchestrator, event_bus, *, max_per_run: int = 4):
        self.orchestrator = orchestrator
        self.event_bus = event_bus
        self.max_per_run = max_per_run
        self._wired = False
        self._last_result: Dict[str, Any] = {}
        self._run_task: Optional[asyncio.Task] = None
        self._run_state: str = "idle"

    @property
    def is_running(self) -> bool:
        task = self._run_task
        return task is not None and not task.done()

    def get_run_status(self) -> Dict[str, Any]:
        return {
            "state": "running" if self.is_running else self._run_state,
            "last_result": dict(self._last_result or {}),
        }

    def start_background(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Schedule ``run_once`` without blocking the caller."""
        if self.is_running:
            return {
                "ok": False,
                "started": False,
                "error": "repo scout already running",
                "status": self.get_run_status(),
            }
        # Several cortex schedulers (bootstrap, continuous improvement,
        # scheduled job) each request a run at boot — three back-to-back
        # ~70s scout runs blocked the event loop for minutes. One run per
        # min-interval is plenty.
        min_interval = _env_seconds("SKYN3T_SCOUT_MIN_INTERVAL_S", 1800.0)
        last_done = getattr(self, "_last_run_finished_at", None)
        if last_done is not None and (time.monotonic() - last_done) < min_interval:
            return {
                "ok": False,
                "started": False,
                "error": (
                    f"repo scout ran {int(time.monotonic() - last_done)}s ago "
                    f"(min interval {int(min_interval)}s)"
                ),
                "status": self.get_run_status(),
            }
        cfg = dict(config or {})
        self._run_state = "running"
        self._run_task = asyncio.create_task(self._run_once_guarded(cfg))
        self._run_task.add_done_callback(self._clear_run_task)
        return {"ok": True, "started": True, "state": "running"}

    def _clear_run_task(self, task: asyncio.Task) -> None:
        self._run_task = None
        self._last_run_finished_at = time.monotonic()
        if self._run_state == "running":
            self._run_state = "completed" if not task.cancelled() else "cancelled"

    async def _run_once_guarded(self, config: Dict[str, Any]) -> Dict[str, Any]:
        # Boot grace: a scout run blocks the loop in long synchronous
        # stretches (ingest/embedding). Never let one start before the
        # server has had time to bind and serve.
        grace_left = _env_seconds("SKYN3T_SCOUT_BOOT_GRACE_S", 120.0) - (
            time.monotonic() - _MODULE_T0
        )
        if grace_left > 0:
            logger.info("repo scout deferring %.0fs (boot grace)", grace_left)
            await asyncio.sleep(grace_left)
        timeout = 300
        try:
            from skyn3t.config.settings import get_settings

            timeout = max(
                30,
                int(getattr(get_settings(), "cortex_scout_run_timeout_seconds", 300)),
            )
        except Exception:
            pass
        try:
            result = await asyncio.wait_for(self.run_once(config), timeout=timeout)
            self._run_state = "completed" if result.get("ok") else "failed"
            return result
        except asyncio.TimeoutError:
            logger.warning("repo scout run timed out after %ss", timeout)
            result = {
                "ok": False,
                "error": f"repo scout timed out after {timeout}s",
                "filed": 0,
                "proposals": [],
            }
            self._last_result = result
            self._run_state = "failed"
            return result
        except Exception as exc:
            logger.exception("repo scout background run failed")
            result = {"ok": False, "error": str(exc), "filed": 0, "proposals": []}
            self._last_result = result
            self._run_state = "failed"
            return result

    def start(self) -> None:
        if self._wired:
            return
        self._wired = True
        try:
            from skyn3t.core.events import EventType

            self.event_bus.subscribe(self._on_event, EventType.SYSTEM_ALERT)
        except Exception:
            logger.exception("repo scout subscription failed")

    def stop(self) -> None:
        if not self._wired:
            return
        self._wired = False
        try:
            from skyn3t.core.events import EventType

            self.event_bus.unsubscribe(self._on_event, EventType.SYSTEM_ALERT)
        except Exception:
            logger.debug("repo scout unsubscribe failed", exc_info=True)

    def get_status(self) -> Dict[str, Any]:
        return {
            "wired": self._wired,
            "last_result": self._last_result,
            "running": self.is_running,
            "state": "running" if self.is_running else self._run_state,
        }

    def _on_event(self, event) -> None:
        payload = getattr(event, "payload", {}) or {}
        if payload.get("kind") != "scheduled_job_triggered":
            return
        scheduled = payload.get("payload") or {}
        if scheduled.get("agent_name") not in {"github_repo_scout", "repo_scout"}:
            return
        if self.is_running:
            logger.info("repo scout skipped scheduled run — already running")
            return
        if self._system_too_busy_for_orchestrator(self.orchestrator):
            logger.info("repo scout skipped scheduled run — system busy")
            return
        prompt = str(scheduled.get("prompt") or "").strip()
        config = self._parse_prompt_config(prompt)
        self.start_background(config)

    @staticmethod
    def _system_too_busy_for_orchestrator(orchestrator) -> bool:
        return (
            MultiSourceRepoScout.busy_reason(orchestrator, studio_active=0) is not None
        )

    @classmethod
    def busy_reason(cls, orchestrator, *, studio_active: int = 0) -> Optional[str]:
        """Return a human-readable reason when scout should not start."""
        try:
            from skyn3t.config.settings import get_settings

            if not getattr(get_settings(), "cortex_scout_skip_when_busy", True):
                return None
        except Exception:
            return None
        if studio_active > 0:
            return f"{studio_active} studio project(s) running or queued"
        running = len(getattr(orchestrator, "running_tasks", {}) or {})
        if running > 0:
            return f"{running} orchestrator task(s) in flight"
        return None

    @staticmethod
    def _parse_prompt_config(prompt: str) -> Dict[str, Any]:
        if not prompt:
            return {}
        try:
            data = json.loads(prompt)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    async def run_once(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = dict(config or {})
        explorer = self.orchestrator.agents.get("github_explorer")
        limit = max(1, min(int(cfg.get("limit", self.max_per_run)), 10))
        cadence = str(cfg.get("cadence") or "daily").strip() or "daily"
        fit_queries = [
            str(item).strip()
            for item in (cfg.get("queries") or default_fit_queries())
            if str(item).strip()
        ]
        platforms = self._normalize_platforms(cfg.get("platforms"))
        candidates, warnings = await self._collect_candidates(
            explorer,
            cadence=cadence,
            fit_queries=fit_queries,
            platforms=platforms,
        )
        if not candidates:
            result = {
                "ok": False,
                "cadence": cadence,
                "platforms": platforms,
                "queries": fit_queries,
                "candidates_seen": 0,
                "filed": 0,
                "proposals": [],
                "warnings": warnings or ["no scout candidates found"],
                "error": "no scout candidates found",
            }
            self._last_result = result
            return result
        proposals = await self._file_proposals(explorer, candidates, limit=limit)
        result = {
            "ok": True,
            "cadence": cadence,
            "platforms": platforms,
            "queries": fit_queries,
            "candidates_seen": len(candidates),
            "filed": len(proposals),
            "proposals": proposals,
            "warnings": warnings,
        }
        self._last_result = result
        return result

    @staticmethod
    def _normalize_platforms(raw: Any) -> List[str]:
        if raw is None:
            return ["github"]
        if isinstance(raw, str):
            parts = [part.strip().lower() for part in raw.split(",")]
        elif isinstance(raw, Sequence):
            parts = [str(part).strip().lower() for part in raw]
        else:
            parts = []
        allowed = {"github", "gitlab", "bitbucket"}
        out: List[str] = []
        for part in parts:
            if part in allowed and part not in out:
                out.append(part)
        return out or ["github"]

    async def _collect_candidates(
        self,
        explorer,
        *,
        cadence: str,
        fit_queries: List[str],
        platforms: List[str],
    ) -> Tuple[List[_ScoutCandidate], List[str]]:
        seen: set[str] = set()
        out: List[_ScoutCandidate] = []
        warnings: List[str] = []

        for platform in platforms:
            try:
                if platform == "github":
                    if explorer is None:
                        warnings.append("github_explorer agent not registered; skipping github")
                        continue
                    platform_candidates = await self._collect_github_candidates(
                        explorer,
                        cadence=cadence,
                        fit_queries=fit_queries,
                    )
                elif platform == "gitlab":
                    platform_candidates = await self._collect_gitlab_candidates(
                        cadence=cadence,
                        fit_queries=fit_queries,
                    )
                else:
                    platform_candidates = await self._collect_bitbucket_candidates(
                        cadence=cadence,
                        fit_queries=fit_queries,
                    )
            except httpx.HTTPError as exc:
                logger.warning("repo scout %s collection failed: %s", platform, exc)
                warnings.append(f"{platform} scout failed: {exc}")
                continue
            except Exception as exc:
                logger.exception("repo scout %s collection failed", platform)
                warnings.append(f"{platform} scout failed: {exc}")
                continue

            for candidate in platform_candidates:
                candidate_key = self._candidate_key(candidate)
                if candidate_key in seen:
                    continue
                seen.add(candidate_key)
                out.append(candidate)
        return out, warnings

    async def _collect_github_candidates(
        self,
        explorer,
        *,
        cadence: str,
        fit_queries: List[str],
    ) -> List[_ScoutCandidate]:
        from skyn3t.core.agent import TaskRequest

        out: List[_ScoutCandidate] = []
        trending = await explorer.execute(
            TaskRequest(
                title="github scout trending",
                input_data={"task_type": "trending_repos", "since": cadence},
            )
        )
        for repo in ((getattr(trending, "output", {}) or {}).get("repositories") or [])[:6]:
            full_name = str(repo.get("full_name") or "").strip()
            if not full_name:
                continue
            out.append(
                _ScoutCandidate(
                    platform="github",
                    lane="popularity",
                    query=f"trending:{cadence}",
                    full_name=full_name,
                    description=str(repo.get("description") or ""),
                    stars=int(repo.get("stars") or 0),
                    language=str(repo.get("language") or ""),
                    url=str(repo.get("url") or ""),
                )
            )

        for query in fit_queries:
            result = await explorer.execute(
                TaskRequest(
                    title=f"github scout search: {query}",
                    input_data={"task_type": "code_search", "query": query, "sort": "updated"},
                )
            )
            for repo in ((getattr(result, "output", {}) or {}).get("repositories") or [])[:4]:
                full_name = str(repo.get("full_name") or "").strip()
                if not full_name:
                    continue
                out.append(
                    _ScoutCandidate(
                        platform="github",
                        lane="fit",
                        query=query,
                        full_name=full_name,
                        description=str(repo.get("description") or ""),
                        stars=int(repo.get("stars") or 0),
                        language=str(repo.get("language") or ""),
                        url=str(repo.get("url") or ""),
                    )
                )
        return out

    async def _collect_gitlab_candidates(
        self,
        *,
        cadence: str,
        fit_queries: List[str],
    ) -> List[_ScoutCandidate]:
        del cadence  # GitLab API does not expose a cadence-aligned trending endpoint.

        out: List[_ScoutCandidate] = []
        headers = {"User-Agent": "SkyN3t-RepoScout/1.0"}
        async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
            popularity = await self._fetch_gitlab_projects(
                client,
                {"simple": "true", "order_by": "star_count", "sort": "desc", "per_page": 6},
            )
            for repo in popularity:
                candidate = self._gitlab_candidate_from_project(repo, lane="popularity", query="stars")
                if candidate is not None:
                    out.append(candidate)

            for query in fit_queries:
                matches = await self._fetch_gitlab_projects(
                    client,
                    {
                        "simple": "true",
                        "search": query,
                        "order_by": "last_activity_at",
                        "sort": "desc",
                        "per_page": 4,
                    },
                )
                for repo in matches:
                    candidate = self._gitlab_candidate_from_project(repo, lane="fit", query=query)
                    if candidate is not None:
                        out.append(candidate)
        return out

    async def _collect_bitbucket_candidates(
        self,
        *,
        cadence: str,
        fit_queries: List[str],
    ) -> List[_ScoutCandidate]:
        del cadence  # Bitbucket API exposes recency better than trend slices.

        out: List[_ScoutCandidate] = []
        headers = {"User-Agent": "SkyN3t-RepoScout/1.0"}
        async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
            recent = await self._fetch_bitbucket_repositories(
                client,
                {"sort": "-updated_on", "pagelen": 6},
            )
            for repo in recent:
                candidate = self._bitbucket_candidate_from_repo(repo, lane="activity", query="updated")
                if candidate is not None:
                    out.append(candidate)

            for query in fit_queries:
                matches = await self._fetch_bitbucket_repositories(
                    client,
                    {
                        "q": self._bitbucket_query_expression(query),
                        "sort": "-updated_on",
                        "pagelen": 4,
                    },
                )
                for repo in matches:
                    candidate = self._bitbucket_candidate_from_repo(repo, lane="fit", query=query)
                    if candidate is not None:
                        out.append(candidate)
        return out

    async def _fetch_gitlab_projects(
        self,
        client: httpx.AsyncClient,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        response = await client.get("https://gitlab.com/api/v4/projects", params=params)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []

    async def _fetch_bitbucket_repositories(
        self,
        client: httpx.AsyncClient,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        response = await client.get("https://api.bitbucket.org/2.0/repositories", params=params)
        response.raise_for_status()
        data = response.json()
        values = data.get("values") if isinstance(data, dict) else []
        return values if isinstance(values, list) else []

    @staticmethod
    def _gitlab_candidate_from_project(
        project: Dict[str, Any],
        *,
        lane: str,
        query: str,
    ) -> Optional[_ScoutCandidate]:
        full_name = str(project.get("path_with_namespace") or "").strip()
        if not full_name:
            return None
        license_name = MultiSourceRepoScout._extract_license_name(project.get("license"))
        topics = [str(item).strip() for item in (project.get("topics") or []) if str(item).strip()]
        return _ScoutCandidate(
            platform="gitlab",
            lane=lane,
            query=query,
            full_name=full_name,
            description=str(project.get("description") or ""),
            stars=int(project.get("star_count") or 0),
            language=str(project.get("programming_language") or ""),
            url=str(project.get("web_url") or ""),
            license_name=license_name,
            topics=topics,
        )

    @staticmethod
    def _bitbucket_candidate_from_repo(
        repo: Dict[str, Any],
        *,
        lane: str,
        query: str,
    ) -> Optional[_ScoutCandidate]:
        full_name = str(repo.get("full_name") or "").strip()
        if not full_name:
            return None
        links = repo.get("links") if isinstance(repo.get("links"), dict) else {}
        html_link = links.get("html") if isinstance(links, dict) else {}
        url = str((html_link or {}).get("href") or "")
        language = str(repo.get("language") or "")
        return _ScoutCandidate(
            platform="bitbucket",
            lane=lane,
            query=query,
            full_name=full_name,
            description=str(repo.get("description") or ""),
            stars=0,
            language=language,
            url=url,
            license_name=MultiSourceRepoScout._extract_license_name(repo.get("license")),
            topics=[],
        )

    async def _file_proposals(self, explorer, candidates: List[_ScoutCandidate], *, limit: int) -> List[Dict[str, Any]]:
        from skyn3t.core.agent import TaskRequest

        store = get_store()
        filed: List[Dict[str, Any]] = []

        ranked = sorted(
            candidates,
            key=lambda item: (self._candidate_score(item), item.stars),
            reverse=True,
        )
        for candidate in ranked:
            if len(filed) >= limit:
                break
            if self._has_active_proposal(store, candidate):
                continue

            details: Dict[str, Any] = {}
            license_name = candidate.license_name or "unknown"
            topics = list(candidate.topics or [])
            repo_url = candidate.url

            if candidate.platform == "github" and explorer is not None:
                owner, repo = candidate.full_name.split("/", 1)
                analysis = await explorer.execute(
                    TaskRequest(
                        title=f"github scout analyze {candidate.full_name}",
                        input_data={
                            "task_type": "repo_analysis",
                            "owner": owner,
                            "repo": repo,
                        },
                    )
                )
                details = getattr(analysis, "output", {}) or {}
                if details:
                    license_name = str(details.get("license") or license_name or "unknown")
                    topics = list(details.get("topics") or topics)
                    repo_url = str(details.get("url") or repo_url or "")

            selection_reason = (
                f"{candidate.lane} lane via '{candidate.query}'"
                if candidate.query
                else candidate.lane
            )
            reuse_risk = self._license_reuse_risk(license_name)
            detail = (
                f"{candidate.platform.title()} scout candidate for Cortex review.\n\n"
                f"- Repo: `{candidate.full_name}`\n"
                f"- URL: {repo_url or 'unknown'}\n"
                f"- Platform: `{candidate.platform}`\n"
                f"- Lane: `{candidate.lane}`\n"
                f"- Query: `{candidate.query}`\n"
                f"- Stars: {candidate.stars}\n"
                f"- Language: {candidate.language or 'unknown'}\n"
                f"- License: {license_name}\n"
                f"- Reuse risk: {reuse_risk}\n"
                f"- Topics: {', '.join(topics) if topics else 'none'}\n"
                f"- Why selected: {selection_reason}\n\n"
                f"Description:\n{candidate.description or details.get('description') or 'No description'}\n\n"
            )
            if candidate.platform == "github":
                detail += (
                    "Ingest path: GitHub docs are auto-ingested into RAG when this proposal is "
                    "filed (no manual ingest approval). A separate feature proposal will appear "
                    "if ingestion succeeds — approve that to adapt the pattern into SkyN3t code."
                )
                kind = "ingest"
            else:
                detail += (
                    "Approval path: this stores an attributed external-learning memory record only. "
                    "It does not ingest repository code or trust implementation details automatically."
                )
                kind = "external_learning"

            proposal = store.create(
                kind=kind,
                title=f"Scout {candidate.platform.title()} repo: {candidate.full_name}",
                summary=(
                    f"{selection_reason}; stars={candidate.stars}; "
                    f"license={license_name}; risk={reuse_risk}; "
                    f"language={candidate.language or 'unknown'}"
                )[:200],
                detail=detail,
                payload={
                    "repo": candidate.full_name,
                    "repo_key": self._candidate_key(candidate),
                    "topic": candidate.query,
                    "query": candidate.query,
                    "limit": 8,
                    "lane": candidate.lane,
                    "source_platform": candidate.platform,
                    "repo_url": repo_url,
                    "license": license_name,
                    "topics": topics,
                    "selection_reason": selection_reason,
                    "reuse_risk": reuse_risk,
                    "description": candidate.description or details.get("description") or "",
                    "language": candidate.language or "",
                    "stars": candidate.stars,
                },
                source=f"repo_scout:{candidate.platform}",
                force_requires_approval=(kind != "ingest"),
            )
            filed.append(
                {
                    "proposal_id": proposal.id,
                    "kind": kind,
                    "platform": candidate.platform,
                    "repo": candidate.full_name,
                    "lane": candidate.lane,
                    "license": license_name,
                    "reuse_risk": reuse_risk,
                }
            )
        return filed

    @staticmethod
    def _relevance_boost(candidate: _ScoutCandidate) -> float:
        """How well repo metadata matches SkyN3t learning goals (not raw stars)."""
        haystack = " ".join(
            [
                candidate.description or "",
                candidate.language or "",
                " ".join(candidate.topics or []),
                candidate.query or "",
            ]
        ).lower()
        hits = sum(1 for term in _RELEVANCE_TERMS if term in haystack)
        return min(hits * 0.35, 2.0)

    @staticmethod
    def _candidate_score(candidate: _ScoutCandidate) -> float:
        score = 0.0
        if candidate.lane == "fit":
            score += 2.5
        elif candidate.lane == "activity":
            score += 1.4
        else:
            score += 0.8

        score += MultiSourceRepoScout._relevance_boost(candidate)

        if candidate.platform == "github":
            score += min(candidate.stars / 8000.0, 2.0)
        elif candidate.platform == "gitlab":
            score += min(candidate.stars / 2000.0, 1.8)
        else:
            score += 0.5

        if candidate.language:
            score += 0.2
        if candidate.description:
            score += 0.15
        risk = MultiSourceRepoScout._license_reuse_risk(candidate.license_name)
        if risk == "low":
            score += 0.4
        elif risk == "high":
            score -= 1.0
        elif risk == "unknown":
            score -= 0.15
        return score

    @staticmethod
    def _candidate_key(candidate: _ScoutCandidate) -> str:
        repo_url = MultiSourceRepoScout._normalize_repo_url(candidate.url)
        if repo_url:
            return repo_url
        return f"{candidate.platform}:{candidate.full_name.strip().lower()}"

    @staticmethod
    def _normalize_repo_url(url: Any) -> str:
        text = str(url or "").strip().lower()
        while text.endswith("/"):
            text = text[:-1]
        return text

    @staticmethod
    def _extract_license_name(raw: Any) -> str:
        if isinstance(raw, dict):
            return str(raw.get("name") or raw.get("key") or raw.get("nickname") or "").strip()
        return str(raw or "").strip()

    @staticmethod
    def _license_reuse_risk(license_name: str) -> str:
        normalized = str(license_name or "").strip().lower()
        if not normalized or normalized == "unknown":
            return "unknown"
        if any(marker in normalized for marker in _RESTRICTIVE_LICENSE_MARKERS):
            return "high"
        if any(marker in normalized for marker in _PERMISSIVE_LICENSE_MARKERS):
            return "low"
        return "medium"

    @staticmethod
    def _bitbucket_query_expression(query: str) -> str:
        terms = [term for term in query.split() if term]
        if not terms:
            return 'name ~ ""'
        clauses = [f'name ~ "{term}" OR description ~ "{term}"' for term in terms[:4]]
        return "(" + ") OR (".join(clauses) + ")"

    @staticmethod
    def _has_active_proposal(store, candidate: _ScoutCandidate) -> bool:
        candidate_key = MultiSourceRepoScout._candidate_key(candidate)
        candidate_url = MultiSourceRepoScout._normalize_repo_url(candidate.url)
        for proposal in store.list(origin="system"):
            if proposal.kind not in {"ingest", "external_learning"}:
                continue
            if proposal.status not in {"pending", "approved", "applying"}:
                continue
            payload = proposal.payload or {}
            proposal_url = MultiSourceRepoScout._normalize_repo_url(payload.get("repo_url"))
            proposal_key = str(payload.get("repo_key") or "").strip().lower()
            proposal_platform = str(payload.get("source_platform") or "").strip().lower()
            proposal_repo = str(payload.get("repo") or "").strip().lower()
            if candidate_url and proposal_url == candidate_url:
                return True
            if proposal_key and proposal_key == candidate_key:
                return True
            if proposal_platform == candidate.platform and proposal_repo == candidate.full_name.strip().lower():
                return True
        return False


GitHubRepoScout = MultiSourceRepoScout
