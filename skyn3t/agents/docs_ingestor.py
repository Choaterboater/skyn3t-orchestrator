"""Docs Ingestor Agent — fetch public LLM provider docs and push them into RAG.

This agent reads ``data/llm_docs_seeds.yaml`` (top-level ``providers`` keyed by
backend identifier), fetches each public documentation URL via httpx (no auth),
strips HTML to plain text, truncates to a sane size, and ingests the result
into the RAG knowledge base with ``{kind: "llm-docs", provider, url}`` metadata
so downstream consumers (notably ``skyn3t.adapters.prompt_builder``) can
retrieve provider-aware augmentation snippets.
"""

from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.docs_ingestor")

DEFAULT_SEEDS = "data/llm_docs_seeds.yaml"
MAX_CHARS = 60_000

# Rough HTML -> text via regex; good enough for plain documentation pages.
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


class DocsIngestorAgent(BaseAgent):
    """Ingest public LLM provider documentation into RAG."""

    def __init__(
        self,
        name: str = "docs_ingestor",
        *,
        event_bus: Optional[EventBus] = None,
        rag: Optional[Any] = None,
        seeds_path: str = DEFAULT_SEEDS,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="docs_ingestor",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="docs_ingest",
                description="ingest provider documentation into RAG",
                parameters={},
            )
        )
        self.rag = rag
        self.seeds_path = seeds_path
        self._client: Any = None

    async def initialize(self) -> None:
        try:
            import httpx  # type: ignore

            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (skyn3t docs ingestor)"},
            )
        except Exception:
            self._client = None
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return self._client is not None

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        if hasattr(self, "think"):
            try:
                await self.think("docs_ingestor: loading seeds")
            except Exception:
                pass

        try:
            seeds = self._load_seeds()
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"seeds: {e}")

        # Optional filter via task.input_data
        only_provider = (task.input_data or {}).get("provider")
        ingested: List[Dict[str, Any]] = []
        skipped: List[Dict[str, str]] = []
        for provider, spec in seeds.items():
            if only_provider and provider != only_provider:
                continue
            base = spec.get("base", "")
            for path in spec.get("pages") or []:
                url = base.rstrip("/") + "/" + path.lstrip("/")
                try:
                    text = await self._fetch_clean(url)
                except Exception as e:
                    skipped.append({"url": url, "reason": str(e)[:120]})
                    continue
                if not text or len(text) < 200:
                    skipped.append({"url": url, "reason": "too short"})
                    continue
                eid = await self._ingest_doc(provider=provider, url=url, text=text)
                ingested.append(
                    {
                        "provider": provider,
                        "url": url,
                        "embedding_id": eid,
                        "chars": len(text),
                    }
                )
                if hasattr(self, "think"):
                    try:
                        await self.think(f"ingested {provider} · {url[-60:]}")
                    except Exception:
                        pass

        summary = (
            f"Ingested {len(ingested)} doc pages across "
            f"{len(set(d['provider'] for d in ingested))} providers; "
            f"{len(skipped)} skipped."
        )
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"ingested": ingested, "skipped": skipped, "summary": summary},
        )

    # ------------------------------------------------------------------ helpers

    def _load_seeds(self) -> Dict[str, Dict[str, Any]]:
        try:
            import yaml  # type: ignore
        except ImportError:
            raise RuntimeError("PyYAML required")
        text = Path(self.seeds_path).read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            return {}
        providers = data.get("providers", {})
        if not isinstance(providers, dict):
            return {}
        return {
            str(name): dict(spec)
            for name, spec in providers.items()
            if isinstance(spec, dict)
        }

    async def _fetch_clean(self, url: str) -> str:
        if self._client is None:
            await self.initialize()
        if self._client is None:
            raise RuntimeError("httpx not available")
        r = await self._client.get(url)
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}")
        return self._html_to_text(r.text)

    @staticmethod
    def _html_to_text(s: str) -> str:
        # Strip script/style blocks before stripping all tags so embedded JS/CSS
        # text doesn't leak into the cleaned output.
        s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
        s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
        s = _TAG.sub(" ", s)
        s = html.unescape(s)
        s = _WS.sub(" ", s).strip()
        if len(s) > MAX_CHARS:
            s = s[:MAX_CHARS]
        return s

    async def _ingest_doc(
        self, *, provider: str, url: str, text: str
    ) -> Optional[str]:
        if self.rag is None:
            return None
        meta = {
            "kind": "llm-docs",
            "provider": provider,
            "url": url,
            "ingested_at": int(time.time()),
        }
        try:
            if hasattr(self.rag, "add_knowledge_one"):
                embedding_id = await self.rag.add_knowledge_one(
                    content=text,
                    title=f"{provider}: {url.rsplit('/', 1)[-1]}",
                    source=url,
                    metadata=meta,
                )
                return embedding_id if isinstance(embedding_id, str) else None
            if hasattr(self.rag, "add_knowledge"):
                ids = await self.rag.add_knowledge(content=text, metadata=meta)
                return str(ids[0]) if isinstance(ids, list) and ids else None
        except Exception:
            logger.exception("rag add failed for %s", url)
        return None
