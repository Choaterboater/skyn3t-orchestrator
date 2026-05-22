from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional


@dataclass(frozen=True)
class AgentCatalogEntry:
    class_name: str
    runtime_name: str
    tier: str
    label: str
    summary: str
    recommended_backend: Optional[str] = None
    recommended_model: Optional[str] = None


_CATALOG = [
    AgentCatalogEntry(
        class_name="BrainstormAgent",
        runtime_name="brainstorm",
        tier="primary",
        label="Brainstorm",
        summary="Frames the mission and expands the initial brief.",
        # Router-driven. Keep the catalog neutral so stage policy and
        # operator overrides decide the backend/model at runtime.
        recommended_backend=None,
        recommended_model=None,
    ),
    AgentCatalogEntry(
        class_name="ResearchAgent",
        runtime_name="research_agent",
        tier="primary",
        label="Research",
        summary="Finds context, sources, and comparisons that shape the build.",
        # Catalog defaults intentionally left blank — model router
        # picks (cheap tier for research). To pin a backend, set it
        # in data/agent_overrides.json instead.
        recommended_backend=None,
        recommended_model=None,
    ),
    AgentCatalogEntry(
        class_name="ArchitectAgent",
        runtime_name="architect",
        tier="primary",
        label="Architect",
        summary="Turns the brief into a concrete system plan.",
        recommended_backend=None,
        recommended_model=None,
    ),
    AgentCatalogEntry(
        class_name="CodeAgent",
        runtime_name="code_agent",
        tier="primary",
        label="Code",
        summary="Scaffolds and implements new source files for fresh builds.",
        # Router → strong tier (claude_cli/opus).
        recommended_backend=None,
        recommended_model=None,
    ),
    AgentCatalogEntry(
        class_name="WriterAgent",
        runtime_name="writer",
        tier="primary",
        label="Writer",
        summary="Produces polished copy, docs, and supporting prose when needed.",
        # Router → cheap tier (kimi_cli).
        recommended_backend=None,
        recommended_model=None,
    ),
    AgentCatalogEntry(
        class_name="DesignerAgent",
        runtime_name="designer",
        tier="primary",
        label="Designer",
        summary="Shapes visual direction, brand cues, and UI polish.",
        # Router → cheap tier (kimi_cli) when designer runs at all.
        recommended_backend=None,
        recommended_model=None,
    ),
    AgentCatalogEntry(
        class_name="MarketerAgent",
        runtime_name="marketer",
        tier="primary",
        label="Marketer",
        summary="Builds positioning and go-to-market materials.",
        recommended_backend=None,
        recommended_model=None,
    ),
    AgentCatalogEntry(
        class_name="ReviewerAgent",
        runtime_name="reviewer",
        tier="primary",
        label="Reviewer",
        summary="Grades output quality and summarizes whether the mission is ready.",
        # Router → strong tier (claude_cli/opus) since reviewer
        # quality matters more than anywhere else.
        recommended_backend=None,
        recommended_model=None,
    ),
    AgentCatalogEntry(
        class_name="BusinessAnalystAgent",
        runtime_name="business_analyst",
        tier="primary",
        label="Business analyst",
        summary="Covers strategy, audience, pricing, and business framing.",
        recommended_backend=None,
        recommended_model=None,
    ),
    AgentCatalogEntry(
        class_name="CodeImproverAgent",
        runtime_name="code_improver",
        tier="internal",
        label="Code improver",
        summary="Applies targeted repo edits when a mission points at existing files.",
        # Router → strong tier (claude_cli/opus) — fix loops need
        # the same quality as fresh code gen.
        recommended_backend=None,
        recommended_model=None,
    ),
    AgentCatalogEntry(
        class_name="VerifierAgent",
        runtime_name="verifier",
        tier="internal",
        label="Verifier",
        summary="Runs quality-gate verdicts after artifact-producing stages.",
        recommended_backend="claude_cli",
        recommended_model="sonnet",
    ),
    AgentCatalogEntry(
        class_name="ExplorerAgent",
        runtime_name="explorer",
        tier="internal",
        label="Explorer",
        summary="Performs broad repo or knowledge exploration for other agents.",
        recommended_backend="claude_cli",
        recommended_model="haiku",
    ),
    AgentCatalogEntry(
        class_name="FileOpsAgent",
        runtime_name="file_ops_agent",
        tier="internal",
        label="File ops",
        summary="Handles utility file reads, writes, and search tasks.",
        recommended_backend="claude_cli",
        recommended_model="haiku",
    ),
    AgentCatalogEntry(
        class_name="GitHubExplorerAgent",
        runtime_name="github_explorer",
        tier="internal",
        label="GitHub explorer",
        summary="Searches GitHub repos and issues when missions need remote code context.",
        recommended_backend="copilot_cli",
        recommended_model="gpt-5.4-mini",
    ),
    AgentCatalogEntry(
        class_name="GitHubIngestorAgent",
        runtime_name="github_ingestor",
        tier="internal",
        label="GitHub ingestor",
        summary="Pulls remote GitHub knowledge into the swarm memory pipeline.",
        recommended_backend="copilot_cli",
        recommended_model="gpt-5.4-mini",
    ),
    AgentCatalogEntry(
        class_name="SchedulerAgent",
        runtime_name="scheduler_agent",
        tier="internal",
        label="Scheduler",
        summary="Runs cron-style reminders and deferred orchestration tasks.",
        recommended_backend="claude_cli",
        recommended_model="haiku",
    ),
    AgentCatalogEntry(
        class_name="ProjectMemoryAgent",
        runtime_name="project_memory",
        tier="internal",
        label="Project memory",
        summary="Persists mission context and recalls prior project knowledge.",
        recommended_backend="copilot_cli",
        recommended_model="gpt-5.4-mini",
    ),
    AgentCatalogEntry(
        class_name="DocsIngestorAgent",
        runtime_name="docs_ingestor",
        tier="internal",
        label="Docs ingestor",
        summary="Feeds documentation into the knowledge layer for downstream agents.",
        recommended_backend="copilot_cli",
        recommended_model="gpt-5.4-mini",
    ),
]

_BY_CLASS = {entry.class_name: entry for entry in _CATALOG}
_BY_NAME = {entry.runtime_name: entry for entry in _CATALOG}


def get_agent_catalog_entry(
    class_name: Optional[str] = None,
    runtime_name: Optional[str] = None,
) -> Optional[AgentCatalogEntry]:
    if runtime_name:
        entry = _BY_NAME.get(runtime_name)
        if entry is not None:
            return entry
    if class_name:
        return _BY_CLASS.get(class_name)
    return None


def build_agent_override(
    *,
    class_name: Optional[str] = None,
    runtime_name: Optional[str] = None,
    class_patch: Optional[Mapping[str, Any]] = None,
    name_patch: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    # Catalog metadata is descriptive only. Runtime config should come
    # from explicit operator settings/overrides, not hidden code-level
    # defaults tied to one vendor or model family.
    merged: Dict[str, Any] = {}
    merged.update(dict(class_patch or {}))
    merged.update(dict(name_patch or {}))
    return merged


def get_agent_catalog_metadata(
    class_name: Optional[str] = None,
    runtime_name: Optional[str] = None,
) -> Dict[str, Any]:
    entry = get_agent_catalog_entry(class_name=class_name, runtime_name=runtime_name)
    if entry is None:
        return {
            "tier": "primary",
            "label": runtime_name or class_name or "",
            "summary": "",
            "recommended_backend": None,
            "recommended_model": None,
        }
    data = asdict(entry)
    data.pop("class_name", None)
    data.pop("runtime_name", None)
    return data
