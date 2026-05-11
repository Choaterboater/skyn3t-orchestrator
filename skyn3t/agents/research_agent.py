"""Research Agent - searches, summarizes, compares, and fact-checks information."""

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.research_agent")


class ResearchAgent(BaseAgent):
    """Agent for web research, summarization, comparison, and fact-checking."""

    def __init__(
        self,
        name: str = "research_agent",
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="research",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="web_search",
                description="Search the web for information on a given topic",
                parameters={"query": "str", "num_results": "int"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="summarization",
                description="Summarize documents or text content",
                parameters={"text": "str", "summary_length": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="comparison",
                description="Compare multiple pieces of information or options",
                parameters={"items": "list", "criteria": "list"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="corroborate_against_sources",
                description=(
                    "Heuristic substring corroboration of a claim against the "
                    "supplied sources. Not a real fact-checker — does not query "
                    "knowledge bases or verify truth."
                ),
                parameters={"claim": "str", "sources": "list"},
            )
        )
        self._search_history: List[Dict[str, Any]] = []
        self._max_history = self.config.get("max_history", 100)

    async def initialize(self) -> None:
        """Initialize the research agent."""
        self.metadata["initialized"] = True
        self.metadata["search_history_size"] = 0

    async def health_check(self) -> bool:
        """Check if the research agent is operational."""
        try:
            # Test basic text processing
            test_text = "This is a test sentence for health check."
            words = test_text.split()
            return len(words) > 0
        except Exception:
            return False

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        """Execute a research-related task."""
        task_type = task.input_data.get("task_type", "web_search")

        handlers = {
            "web_search": self._web_search,
            "summarization": self._summarize,
            "comparison": self._compare,
            "corroborate_against_sources": self._fact_check,
            # Back-compat alias for callers still passing the old name.
            "fact_check": self._fact_check,
        }

        handler = handlers.get(task_type)
        if not handler:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Unknown task type: {task_type}",
            )

        try:
            result = await handler(task)
            return TaskResult(
                task_id=task.task_id,
                success=result.get("success", True),
                output=result,
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=str(e),
            )

    async def _web_search(self, task: TaskRequest) -> Dict[str, Any]:
        """LLM-grounded research; falls back to placeholder if no backend."""
        query = (task.input_data.get("query")
                 or task.input_data.get("brief")
                 or task.input_data.get("idea")
                 or task.input_data.get("description")
                 or "")
        num_results = task.input_data.get("num_results", 5)
        artifact_dir = task.input_data.get("artifact_dir")

        if not query:
            return {"success": False, "error": "No query provided"}

        # ── MEMORY RECALL ─────────────────────────────────────────
        # Before spending 3-5 min on MCP web search, check the skill
        # library for cached integration specs for each service named
        # in the brief. If we researched Sonarr/Radarr/etc. before,
        # the spec is already saved as a skill — read it back instead
        # of re-fetching. This is what makes the SECOND build of any
        # given integration sub-5min.
        cached_specs = self._recall_cached_specs(query)
        if cached_specs:
            await self._log_recall_hits(cached_specs)
        # ──────────────────────────────────────────────────────────

        # Try LLM-grounded research; falls back to placeholder if backend is deterministic
        results = []
        notes = ""
        try:
            client = self.get_llm() if hasattr(self, "get_llm") else None
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(default_model=self.config.get("model"),
                                    backend=self.config.get("backend"))
            # Detect integration-heavy briefs. When the user names third-
            # party products/APIs the program should talk to, the research
            # we need is the actual API surface — endpoints, auth, response
            # shapes — not market commentary. CodeAgent reads research.md
            # in the next stage, so spec-style output is what unblocks
            # real integrations vs. fake demo data.
            q_lower = query.lower()
            integration_hints = (
                "emby", "jellyfin", "plex", "sonarr", "radarr", "lidarr",
                "prowlarr", "qbittorrent", "transmission", "sabnzbd",
                "sonos", "home assistant", "philips hue", "lifx",
                "unifi", "mikrotik", "pfsense", "opnsense", "tailscale",
                "docker socket", "docker api", "portainer", "proxmox",
                "stripe api", "twilio api", "github api", "slack api",
                "discord api", "spotify api", "rest api", "graphql endpoint",
                "integrate with", "talk to the", "query the",
            )
            is_integration_brief = any(h in q_lower for h in integration_hints)

            if is_integration_brief:
                # Build the per-service "skip these" hint from cache.
                cached_services_str = ""
                if cached_specs:
                    names = ", ".join(sorted(cached_specs.keys()))
                    cached_services_str = (
                        f"\n\nIMPORTANT: skip these services entirely — their "
                        f"specs are already in memory and will be appended "
                        f"after your output: **{names}**. Do NOT include "
                        f"`## {names.split(', ')[0]}` etc. in your response.\n"
                    )
                prompt = (
                    f"Brief:\n{query}\n{cached_services_str}\n"
                    "This brief names third-party products, services, or APIs. "
                    "Produce an INTEGRATION SPEC the code-writing agent can use "
                    "to wire real API calls (not mock data).\n\n"
                    "For each remaining product/service, output a markdown section:\n\n"
                    "## <Product name>\n"
                    "- **Base URL**: typical default (e.g. http://localhost:8989)\n"
                    "- **Auth**: header/query-param scheme (e.g. `X-Api-Key`, "
                    "Bearer token, basic auth, none on local socket)\n"
                    "- **Key endpoints**: 3-6 most useful for a dashboard, each "
                    "with method + path + one-line of what it returns. Format: "
                    "`GET /api/v3/queue → current download queue with progress`\n"
                    "- **Response shape**: critical field names the UI will read "
                    "(e.g. `series.title`, `episode.airDate`, `sizeleft`)\n"
                    "- **Env var convention**: e.g. SONARR_URL, SONARR_API_KEY\n"
                    "- **Gotchas**: rate limits, CORS, websocket vs REST, etc.\n\n"
                    "Be concrete. These are real, well-documented APIs — name "
                    "the actual endpoints, not placeholders. If you don't know "
                    "an endpoint for sure, say so explicitly rather than "
                    "inventing one."
                )
                out = ""
                try:
                    out = await client.complete(prompt, max_tokens=4000, temperature=0.2)
                except Exception as e:
                    logger.warning(f"research primary backend failed: {e}; retrying on fallback")
                    out = ""
                # Cross-model retry: if the configured backend timed out
                # or returned empty, build a fresh LLMClient that skips
                # the failing backend and try a different one. Skip-list
                # is a constructor-time arg on LLMClient, not a per-call
                # kwarg — earlier attempt to pass it inline raised TypeError.
                if not out or "[deterministic-stub]" in out:
                    primary = self.config.get("backend") or ""
                    try:
                        from skyn3t.adapters import LLMClient as _LLMClient
                        retry_client = _LLMClient(
                            default_model=None,
                            backend=None,  # "auto" picks next available
                            skip_backends=[primary] if primary else [],
                        )
                        out = await retry_client.complete(
                            prompt, max_tokens=4000, temperature=0.2,
                        )
                    except Exception as e:
                        logger.warning(f"research fallback backend also failed: {e}")
                        out = ""
            else:
                prompt = (
                    f"Research brief:\n{query}\n\n"
                    f"Produce {num_results} concise findings or considerations relevant to this brief. "
                    f"Format as a markdown bullet list. Each bullet must be one sentence, "
                    f"actionable, and grounded in known facts/tools/patterns. No preamble."
                )
                out = await client.complete(prompt, max_tokens=900, temperature=0.3)
            if out and "[deterministic-stub]" not in out:
                notes = out.strip()
                if is_integration_brief:
                    # Splice cached specs back in. The LLM only wrote
                    # sections for services NOT in cache; we append the
                    # remembered ones so research.md is complete.
                    if cached_specs:
                        cache_blocks = []
                        for svc, spec in sorted(cached_specs.items()):
                            cache_blocks.append(
                                f"## {svc.title()}\n\n"
                                f"_From memory ({time.strftime('%Y-%m-%d', time.gmtime(spec['cached_at']))})._\n\n"
                                f"{spec['body']}"
                            )
                        notes = notes + "\n\n" + "\n\n".join(cache_blocks)
                    results.append({
                        "title": f"Integration spec for {query[:80]}",
                        "snippet": notes[:200],
                        "source": "llm-integration-spec",
                    })
                else:
                    # parse bullets into results
                    for line in notes.splitlines():
                        line = line.strip()
                        if line.startswith(("- ", "* ", "• ")):
                            results.append({"title": line[2:].strip()[:100], "snippet": line[2:].strip(),
                                            "source": "llm"})
                        if len(results) >= num_results:
                            break
        except Exception:
            logger.exception("research LLM call failed")

        # Placeholder fallback if LLM gave nothing real to record. NOTE:
        # for integration briefs, `notes` is the spec text — preserve it
        # even if we're entering the fallback for `results`. The previous
        # code overwrote `notes` with bullet placeholders here, wiping
        # the spec.
        if not results:
            for i in range(min(num_results, 5)):
                results.append({"title": f"Consider angle {i+1} for '{query[:40]}'",
                                "snippet": f"Placeholder finding {i+1}.",
                                "source": "placeholder"})
            if not notes:
                notes = "\n".join(f"- {r['snippet']}" for r in results)

        # Write research.md to artifact_dir for downstream stages
        files_written = []
        if artifact_dir:
            try:
                ad = Path(artifact_dir)
                ad.mkdir(parents=True, exist_ok=True)
                md_path = ad / "research.md"
                md_path.write_text(
                    f"# Research\n\n## Query\n{query}\n\n## Findings\n{notes}\n",
                    encoding="utf-8")
                files_written.append(str(md_path))
                if hasattr(self, "think"):
                    try:
                        await self.think(f"wrote {md_path.name} ({len(results)} findings)")
                    except Exception:
                        logger.debug("think() failed after research write", exc_info=True)
                # Promote LLM-produced integration sections to skills
                # so the next build can recall them from memory. Skip
                # if the LLM call failed (notes is empty / placeholders).
                if (
                    is_integration_brief
                    and notes
                    and "Placeholder finding" not in notes
                ):
                    try:
                        self._promote_specs_to_skills(notes, cached_specs)
                    except Exception:
                        logger.exception("skill promotion failed")
            except Exception:
                pass

        entry = {
            "query": query,
            "num_results": len(results),
            "timestamp": self._now_iso(),
        }
        self._search_history.append(entry)
        if len(self._search_history) > self._max_history:
            self._search_history = self._search_history[-self._max_history :]
        self.metadata["search_history_size"] = len(self._search_history)

        return {
            "success": True,
            "query": query,
            "results": results,
            "total_results": len(results),
            "files": files_written,
            "summary": f"Researched '{query[:60]}': {len(results)} findings, "
                       f"{len(files_written)} file(s) written.",
        }

    # ─────────────────────────────────────────────────────────────────
    # Memory recall + promotion
    # ─────────────────────────────────────────────────────────────────
    # The second time SkyN3t researches Sonarr, the spec is already
    # in the skill library — read it back instead of doing 3min of
    # MCP web search. This is the self-learning loop closing for
    # integration research.

    # Same list as planner._INTEGRATION_TARGETS — kept here so the
    # research agent doesn't depend on planner internals.
    _RECALL_TARGETS = (
        "emby", "jellyfin", "plex", "sonarr", "radarr", "lidarr",
        "prowlarr", "qbittorrent", "transmission", "sabnzbd",
        "sonos", "home assistant", "philips hue", "lifx",
        "unifi", "mikrotik", "pfsense", "opnsense", "tailscale",
        "docker socket", "docker api", "portainer", "proxmox",
        "stripe api", "twilio api", "github api", "slack api",
        "discord api", "spotify api",
    )

    # How fresh a cached spec must be to skip re-fetching. APIs evolve;
    # we re-validate older specs by letting the LLM produce a fresh one.
    _CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days

    def _recall_cached_specs(self, query: str) -> Dict[str, Dict[str, Any]]:
        """For each service named in the query, look up a cached
        `integration-spec-{service}` skill. Return a dict
        ``{service: {body, cached_at}}`` for fresh-enough hits.
        """
        q = (query or "").lower()
        named = [t for t in self._RECALL_TARGETS if t in q]
        if not named:
            return {}
        cached: Dict[str, Dict[str, Any]] = {}
        try:
            from skyn3t.intelligence.skill_library import get_default_library
            lib = get_default_library()
        except Exception:
            return {}
        now = time.time()
        for svc in named:
            # Slug used at write-time: integration-spec-<service slugged>
            tag = f"integration-spec-{re.sub(r'[^a-z0-9]+', '-', svc).strip('-')}"
            try:
                hits = lib.find(tag=tag, min_score=0.0, limit=1)
            except Exception:
                continue
            if not hits:
                continue
            skill = hits[0]
            age = now - (skill.last_used_at or skill.created_at or 0)
            if age > self._CACHE_TTL_SECONDS:
                continue
            cached[svc] = {
                "body": skill.body,
                "cached_at": skill.last_used_at or skill.created_at,
            }
        return cached

    async def _log_recall_hits(self, cached: Dict[str, Dict[str, Any]]) -> None:
        """Announce recall hits to the dashboard so the user sees the
        self-learning loop firing."""
        if not cached:
            return
        names = ", ".join(sorted(cached.keys()))
        msg = f"recalled {len(cached)} cached integration spec(s) from memory: {names}"
        try:
            await self.think(msg)
        except Exception:
            pass

    def _promote_specs_to_skills(
        self, notes: str, already_cached: Dict[str, Dict[str, Any]],
    ) -> None:
        """Parse research.md sections (## ServiceName) and save each
        as a skill tagged ``integration-spec-{service}``. Skips
        services already in the cache so we don't double-write.
        """
        try:
            from skyn3t.intelligence.skill_library import (
                Skill,
                get_default_library,
            )
        except Exception:
            return
        # Split on lines that start with "## " (a service section header).
        sections = re.split(r"\n## ", "\n" + (notes or ""))
        # First chunk is anything before the first ## — discard.
        sections = sections[1:]
        lib = get_default_library()
        for sec in sections:
            # First line is the service name; the rest is the body.
            head, _, body = sec.partition("\n")
            service = head.strip().lower()
            if not service or not body.strip():
                continue
            # Don't bother re-promoting what we just spliced from cache.
            if any(c in service for c in already_cached.keys()):
                continue
            # Only promote sections that look like real specs — they
            # should have at least one "Base URL" or "Auth" line and
            # at least one method/path mention.
            body_lc = body.lower()
            if not (
                ("base url" in body_lc or "auth" in body_lc)
                and any(
                    f"{m} /" in body_lc or f"{m}/" in body_lc
                    for m in ("get", "post", "put", "delete", "patch")
                )
            ):
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", service).strip("-")
            tag = f"integration-spec-{slug}"
            skill = Skill(
                name=f"integration-spec-{slug}",
                body=body.strip(),
                tags=[tag, "integration-spec", "research-cache"],
                success_count=1,
                failure_count=0,
                source="research_agent:auto-promote",
            )
            try:
                lib.upsert(skill)
                logger.info(f"promoted spec to skill: {slug}")
            except Exception:
                logger.exception(f"promote skill {slug} failed")

    async def _summarize(self, task: TaskRequest) -> Dict[str, Any]:
        """Summarize a document or text."""
        text = task.input_data.get("text", "")
        summary_length = task.input_data.get("summary_length", "medium")

        if not text:
            return {"success": False, "error": "No text provided"}

        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        total_sentences = len(sentences)

        if total_sentences == 0:
            return {"success": False, "error": "Empty text provided"}

        # Determine summary length
        length_map = {"short": 0.15, "medium": 0.30, "long": 0.50}
        ratio = length_map.get(summary_length, 0.30)
        num_summary_sentences = max(1, int(total_sentences * ratio))

        # Simple extractive summarization: take first N sentences
        # In production, this would use more sophisticated algorithms
        summary_sentences = sentences[:num_summary_sentences]
        summary = " ".join(summary_sentences)

        return {
            "success": True,
            "original_length": len(text),
            "summary_length": len(summary),
            "original_sentences": total_sentences,
            "summary_sentences": len(summary_sentences),
            "summary": summary,
            "length_type": summary_length,
        }

    async def _compare(self, task: TaskRequest) -> Dict[str, Any]:
        """Compare multiple items based on criteria."""
        items = task.input_data.get("items", [])
        criteria = task.input_data.get("criteria", [])

        if len(items) < 2:
            return {"success": False, "error": "At least 2 items required for comparison"}

        comparison_results = []
        similarities = []
        differences = []

        # Compare each pair
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                item_a = items[i]
                item_b = items[j]

                pair_result = {
                    "item_a": item_a.get("name", f"Item {i + 1}"),
                    "item_b": item_b.get("name", f"Item {j + 1}"),
                }

                # Compare by criteria if provided
                if criteria:
                    criterion_results = {}
                    for criterion in criteria:
                        val_a = item_a.get(criterion)
                        val_b = item_b.get(criterion)
                        criterion_results[criterion] = {
                            "item_a": val_a,
                            "item_b": val_b,
                            "same": val_a == val_b,
                        }
                        if val_a == val_b:
                            similarities.append(f"Both have same {criterion}: {val_a}")
                        else:
                            differences.append(
                                f"{pair_result['item_a']} {criterion}={val_a} vs "
                                f"{pair_result['item_b']} {criterion}={val_b}"
                            )
                    pair_result["criteria"] = criterion_results
                else:
                    # Simple string comparison of JSON representation
                    str_a = str(item_a)
                    str_b = str(item_b)
                    if str_a == str_b:
                        similarities.append(f"{pair_result['item_a']} == {pair_result['item_b']}")
                    else:
                        differences.append(f"{pair_result['item_a']} != {pair_result['item_b']}")

                comparison_results.append(pair_result)

        return {
            "success": True,
            "items_compared": len(items),
            "comparisons": comparison_results,
            "similarities": list(set(similarities)),
            "differences": list(set(differences)),
            "criteria_used": criteria,
        }

    async def _fact_check(self, task: TaskRequest) -> Dict[str, Any]:
        """Corroborate a claim by substring-matching against provided sources.

        Despite the historical name, this is **not** a fact checker: it does
        not consult knowledge bases or verify truth. It only reports whether
        the claim text (or its words) appear in the supplied source strings.
        """
        claim = task.input_data.get("claim", "")
        sources = task.input_data.get("sources", [])

        if not claim:
            return {"success": False, "error": "No claim provided"}

        # Placeholder fact-checking logic
        # In production, this would query knowledge bases or APIs
        verification_status = "unverified"
        confidence = 0.0
        evidence = []

        claim_lower = claim.lower()

        # Simple heuristic checks
        if sources:
            for source in sources:
                source_text = str(source).lower()
                if claim_lower in source_text:
                    verification_status = "supported"
                    confidence = 0.8
                    evidence.append({
                        "source": source,
                        "relevance": "high",
                        "match_type": "exact",
                    })
                elif any(word in source_text for word in claim_lower.split() if len(word) > 4):
                    verification_status = "partially_supported"
                    confidence = max(confidence, 0.4)
                    evidence.append({
                        "source": source,
                        "relevance": "medium",
                        "match_type": "partial",
                    })

        if not evidence:
            verification_status = "unverified"
            confidence = 0.0
            evidence.append({
                "source": "no sources matched",
                "relevance": "none",
                "match_type": "none",
            })

        return {
            "success": True,
            "claim": claim,
            "verification_status": verification_status,
            "confidence": confidence,
            "evidence": evidence,
            "sources_checked": len(sources),
        }

    def _now_iso(self) -> str:
        """Return current UTC time in ISO format."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
