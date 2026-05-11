"""Dynamic pipeline planner.

Given a free-form brief and a list of registered agents (with capabilities),
ask the LLM to choose which agents are relevant, in what order, and what
artifacts each is expected to produce. Returns a synthesized list of
StageSpec-compatible objects.

Falls back to a heuristic keyword match if the LLM is unavailable.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.studio.planner")

_TARGET_FILE_PATTERN = re.compile(
    r"\b([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.\w+|target_file\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_CODE_BUILD_PATTERNS = (
    re.compile(
        r"\b(?:source\s+code|source\s+files?|frontend|backend|api|endpoint|function|class|component|schema|migration|html|css|javascript|typescript|python|fastapi|react|next(?:\.js)?|node(?:\.js)?|cli)\b",
        re.IGNORECASE,
    ),
    re.compile(
        # build/create/etc followed by up to ~6 intervening words (qualifiers,
        # me, a, an, "tic-tac-toe", etc.) then a software-y noun. The old
        # version required a fixed adjective vocabulary which made it brittle
        # for natural briefs like "build me a tic-tac-toe game".
        r"\b(?:build|create|make|ship|launch|scaffold|generate|prototype|develop|implement)"
        r"(?:\s+\S+){0,6}\s+"
        r"(?:app|site|website|api|backend|frontend|service|tool|script|cli|bot|dashboard|extension|game)\b",
        re.IGNORECASE,
    ),
)
_DOCS_ONLY_PATTERN = re.compile(
    r"\b(?:readme|docs?|documentation|spec|brief|plan|proposal|roadmap|analysis|research|copy|content|blog|email|summary)\b",
    re.IGNORECASE,
)
_CODE_FOLLOWUP_AGENTS = {"WriterAgent", "MarketerAgent", "ReviewerAgent", "VerifierAgent"}
_SOFTWARE_ARCHITECTURE_KEYWORDS = [
    "app",
    "saas",
    "platform",
    "service",
    "api",
    "backend",
    "frontend",
    "system",
    "tool",
    "dashboard",
    "site",
    "website",
    "script",
    "cli",
    "bot",
    "extension",
    "game",
]

# Map agent class names to (capability hint, typical artifact). Used both as
# the menu we show the LLM and as the heuristic-fallback knowledge.
AGENT_CATALOG = [
    {"agent": "BrainstormAgent",       "capabilities": ["framing", "ideation"],
     "artifact": "brainstorm.md",
     "good_for": "every project; runs first to expand the brief"},
    {"agent": "ResearchAgent",         "capabilities": ["research", "synthesis"],
     "artifact": "research.md",
     "good_for": "any brief that benefits from external context or competitive lookup"},
    {"agent": "ArchitectAgent",        "capabilities": ["system_design"],
     "artifact": "architecture.md",
     "good_for": "software/web/saas/app projects with technical components"},
    {"agent": "DesignerAgent",         "capabilities": ["visual", "branding"],
     "artifact": "brand.md, palette.json, components.md",
     "good_for": "anything with a visual brand, UI, or design system"},
    {"agent": "WriterAgent",           "capabilities": ["copywriting"],
     "artifact": "varies (readme/landing/spec/blog/email)",
     "good_for": "any project that needs prose, docs, or copy"},
    {"agent": "MarketerAgent",         "capabilities": ["positioning", "gtm"],
     "artifact": "positioning.md, channel_plan.md, launch_checklist.md",
     "good_for": "products with a launch, audience, or campaign component"},
    {"agent": "BusinessAnalystAgent",  "capabilities": ["strategy", "market"],
     "artifact": "market_scan.md, business_model.md, pitch_outline.md",
     "good_for": "ventures with revenue, ICP, competitor, or pricing concerns"},
    {"agent": "CodeImproverAgent",     "capabilities": ["code_patch"],
     "artifact": "git branch with applied diff",
     "good_for": "modifications to existing repo files (target_file in brief)"},
    {"agent": "CodeAgent",             "capabilities": ["code_generation"],
     "artifact": "scaffolded source files",
     "good_for": "new code from scratch — apps/services/scripts"},
    {"agent": "ReviewerAgent",         "capabilities": ["review", "qa"],
     "artifact": "review.md",
     "good_for": "every project; runs last to grade the produced artifacts"},
    {"agent": "VerifierAgent",         "capabilities": ["verification"],
     "artifact": "(in-memory verdict)",
     "good_for": "after any artifact-producing stage as a quality gate"},
]


@dataclass
class PlannedStage:
    name: str
    agent: str
    capability: str
    expected_artifact: str = ""
    rationale: str = ""
    handoff_to: Optional[str] = None
    input_extra: Dict[str, Any] = field(default_factory=dict)


async def plan_pipeline(*, brief: str, llm_client=None) -> List[PlannedStage]:
    """Pick stages relevant to this brief. Brainstorm first + Reviewer last
    are always included; in-between is dynamic."""
    chosen_agents: List[str] = []
    expected_artifacts: List[str] = []
    rationales: Dict[str, str] = {}

    if llm_client is not None:
        try:
            chosen_agents, expected_artifacts, rationales = await _llm_plan(brief, llm_client)
        except Exception:
            logger.exception("LLM planner failed; falling back to heuristic")

    if not chosen_agents:
        chosen_agents, expected_artifacts, rationales = _heuristic_plan(brief)

    # Safety net: even if the LLM planner ignored the integration cue,
    # post-process the plan to add ResearchAgent for briefs that name
    # third-party APIs/services the code must talk to. Without research,
    # CodeAgent will fabricate fake demo data for those integrations.
    chosen_agents, expected_artifacts, rationales = _ensure_research_for_integrations(
        brief, chosen_agents, expected_artifacts, rationales,
    )

    chosen_agents, expected_artifacts, rationales = _ensure_code_stage(
        brief,
        chosen_agents,
        expected_artifacts,
        rationales,
    )

    # Brainstorm first (if not chosen) + Reviewer last (always)
    if "BrainstormAgent" not in chosen_agents:
        chosen_agents.insert(0, "BrainstormAgent")
        expected_artifacts.insert(0, "brainstorm.md")
    chosen_agents = [a for a in chosen_agents if a != "ReviewerAgent"]
    expected_artifacts = [a for a in expected_artifacts if a != "review.md"]
    chosen_agents.append("ReviewerAgent")
    expected_artifacts.append("review.md")

    # De-dup keeping order
    seen = set()
    deduped = []
    for a in chosen_agents:
        if a not in seen:
            seen.add(a)
            deduped.append(a)
    chosen_agents = deduped

    # Build PlannedStages
    stages: List[PlannedStage] = []
    by_agent = {e["agent"]: e for e in AGENT_CATALOG}
    for i, agent_name in enumerate(chosen_agents):
        catalog_entry = by_agent.get(agent_name, {})
        cap = (catalog_entry.get("capabilities") or ["general"])[0]
        expected = catalog_entry.get("artifact", "")
        expected_artifact = (
            expected
            if isinstance(expected, str)
            else ", ".join(str(item) for item in expected)
        )
        # synthesize a stage name (lowercased agent without 'Agent' suffix)
        stage_name = re.sub(r"Agent$", "", agent_name).lower()
        stages.append(PlannedStage(
            name=stage_name, agent=agent_name, capability=cap,
            expected_artifact=expected_artifact,
            rationale=rationales.get(agent_name, ""),
        ))
    # Wire handoffs
    for i in range(len(stages) - 1):
        stages[i].handoff_to = stages[i + 1].agent
    return stages


def _heuristic_plan(brief: str) -> tuple[List[str], List[str], Dict[str, str]]:
    """Keyword-based fallback when no LLM."""
    b = (brief or "").lower()
    chosen: List[str] = []
    arts: List[str] = []
    why: Dict[str, str] = {}

    chosen.append("BrainstormAgent")
    arts.append("brainstorm.md")

    needs_research = _mentions_any(
        b,
        ["research", "explore", "competitor", "market", "what's out", "find"],
    )
    # Integration-heavy briefs also need research even when the brief
    # doesn't say "research." If the user names a third-party product,
    # service, API, or device the program should talk to, we want the
    # ResearchAgent to fetch real API specs first — otherwise CodeAgent
    # invents plausible-looking demo data instead of wiring the real
    # integration. This was the root cause of fake homelab dashboards.
    needs_research_for_integrations = _mentions_any(
        b,
        [
            # Streaming / media stack
            "emby", "jellyfin", "plex", "sonarr", "radarr", "lidarr", "readarr",
            "prowlarr", "qbittorrent", "transmission", "deluge", "sabnzbd",
            "nzbget", "overseerr", "tautulli",
            # Smart home / audio
            "sonos", "home assistant", "homeassistant", "hassio", "philips hue",
            "lifx", "nest", "ecobee", "smartthings", "ifttt",
            # Network gear
            "unifi", "ubiquiti", "mikrotik", "openwrt", "pfsense", "opnsense",
            "tailscale", "wireguard",
            # Container / infra
            "docker socket", "docker api", "portainer", "kubernetes api",
            "k8s api", "proxmox", "truenas", "unraid",
            # SaaS APIs
            "stripe api", "twilio api", "sendgrid api", "github api",
            "slack api", "discord api", "spotify api", "openweather",
            # Generic integration signals
            "rest api", "graphql endpoint", "third-party api", "webhook from",
            "integrate with", "pull from", "talk to the", "query the",
        ],
    )
    if needs_research or needs_research_for_integrations:
        chosen.append("ResearchAgent")
        arts.append("research.md")
        why["ResearchAgent"] = (
            "brief names third-party APIs/services to integrate with"
            if needs_research_for_integrations and not needs_research
            else "brief mentions research/competitor/market"
        )

    needs_arch = _mentions_any(
        b,
        _SOFTWARE_ARCHITECTURE_KEYWORDS,
    )
    if needs_arch:
        chosen.append("ArchitectAgent")
        arts.append("architecture.md")
        why["ArchitectAgent"] = "brief implies a software/system component"

    needs_design = _mentions_any(
        b,
        [
            "design",
            "brand",
            "ui",
            "ux",
            "color",
            "logo",
            "aesthetic",
            "frontend",
            "front end",
            "dashboard",
            "site",
            "website",
            "page",
            "landing",
        ],
    )
    if needs_design:
        chosen.append("DesignerAgent")
        arts.append("brand.md")
        why["DesignerAgent"] = "brief asks for visual/brand work"

    needs_marketing = _mentions_any(
        b,
        ["launch", "campaign", "marketing", "audience", "positioning", "channels", "growth", "go-to-market", "gtm"],
    )
    if needs_marketing:
        chosen.append("MarketerAgent")
        arts.append("positioning.md")
        why["MarketerAgent"] = "brief mentions launch/marketing/positioning"

    needs_biz = _mentions_any(
        b,
        ["business", "revenue", "pricing", "model", "monetiz", "icp", "tam", "competitors", "investor", "pitch"],
    )
    if needs_biz:
        chosen.append("BusinessAnalystAgent")
        arts.append("market_scan.md")
        why["BusinessAnalystAgent"] = "brief mentions business model/pricing/strategy"

    target_match = _TARGET_FILE_PATTERN.search(brief or "")
    if target_match:
        chosen.append("CodeImproverAgent")
        arts.append("(branch+commit)")
        why["CodeImproverAgent"] = f"brief specifies target_file: {target_match.group(0)[:60]}"
    elif _should_force_code_agent(brief):
        chosen.append("CodeAgent")
        arts.append("(source files)")
        why["CodeAgent"] = "brief asks SkyN3t to build working software"

    needs_writer = _mentions_any(
        b,
        ["readme", "docs", "documentation", "blog", "email", "spec", "copy", "content", "writeup", "summary"],
    ) or needs_marketing
    if needs_writer:
        chosen.append("WriterAgent")
        arts.append("(prose)")
        why["WriterAgent"] = "brief asks for copy/docs/content"

    return chosen, arts, why


async def _llm_plan(brief: str, llm_client) -> tuple[List[str], List[str], Dict[str, str]]:
    """Ask the LLM to plan the pipeline."""
    catalog_lines = []
    for entry in AGENT_CATALOG:
        catalog_lines.append(
            f"- {entry['agent']}: {entry['good_for']} → produces {entry['artifact']}"
        )
    system = (
        "You are a planner. Given a user's brief, choose which agents from the catalog "
        "should run, in order, to fulfill it. BrainstormAgent always runs first and "
        "ReviewerAgent always runs last (don't include them in your answer). "
        "Pick MINIMAL agents — don't include MarketerAgent for a pure code change, "
        "don't include ArchitectAgent for a brand kit. "
        "Only include CodeAgent when the brief explicitly asks for runnable software "
        "or source code (for example an app, site, api, script, cli, dashboard, or game), "
        "unless the user explicitly asks for a plan, spec, docs, or copy only. "
        "ONLY include CodeImproverAgent when the brief mentions a SPECIFIC FILE PATH "
        "(like 'src/app.py' or 'target_file: ...'). For brand kits, "
        "marketing, copy, or strategy work with no code, omit code agents entirely. "
        "Only include DesignerAgent when the brief explicitly asks for UI, UX, brand, "
        "landing page, or visual direction. "
        "**ALWAYS include ResearchAgent** when the brief names third-party "
        "products, services, APIs, or devices the program must talk to "
        "(examples: sonarr, radarr, sonos, emby, plex, docker, home assistant, "
        "unifi, stripe, twilio, github api, slack api, spotify api, REST API, "
        "GraphQL endpoint, 'integrate with X', 'pull from Y'). For these "
        "briefs CodeAgent CANNOT write real integrations without API specs "
        "from ResearchAgent. Place ResearchAgent BEFORE ArchitectAgent so "
        "the architecture decisions can be informed by the API surface. "
        "Reply ONLY with valid JSON of the form: "
        '{"agents": ["AgentA", "AgentB"], "expected_artifacts": ["a.md", "b.md"], '
        '"rationale": {"AgentA": "why...", "AgentB": "why..."}}'
    )
    prompt = (
        f"Brief: {brief}\n\nAgent catalog:\n" + "\n".join(catalog_lines)
        + "\n\nReply with the JSON plan."
    )
    out = await llm_client.complete(prompt, system=system, max_tokens=600, temperature=0.2)
    if not out or "[deterministic-stub]" in out:
        return [], [], {}
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        return [], [], {}
    data = json.loads(m.group(0))
    agents = [str(a) for a in (data.get("agents") or [])]
    artifacts = [str(a) for a in (data.get("expected_artifacts") or [])]
    rationale = {str(k): str(v) for k, v in (data.get("rationale") or {}).items()}
    return agents, artifacts, rationale


# Verbs that are *only* used about software (you don't "refactor" a campaign
# or "wire up" an essay). Catching these as standalone signals lets briefs
# like "fix the dashboard" / "redesign the UI" trigger a code stage without
# having to mention "app" or "api" explicitly.
_SOFTWARE_VERB_PATTERN = re.compile(
    r"\b(?:fix|refactor|debug|wire|hook|patch|migrate|deploy|reindex)\b",
    re.IGNORECASE,
)
# Verbs that mean "software work" only when they're applied to a software-y
# object. "improve the planner", "add a settings page", "redesign the UI" =
# code work. "improve the copy" or "redesign the brand" = not code.
_IMPROVE_VERB_PATTERN = re.compile(
    r"\b(?:improve|enhance|redesign|add|update)\s+"
    r"(?:an?\s+|the\s+)?"
    r"(?:ui|dashboard|app|page|interface|frontend|backend|api|cli|"
    r"orchestrator|planner|scheduler|service|module|component|widget|"
    r"settings|config|endpoint|route|webhook|integration|"
    r"agent|loop|engine|store|cache|queue|worker|"
    r"layout|form|button|menu)\b",
    re.IGNORECASE,
)
_PURE_DOCS_INTENT_PATTERN = re.compile(
    r"^\s*(?:write|draft|produce|prepare|compose)\s+(?:an?\s+|the\s+)?"
    r"(?:readme|spec|specification|brief|plan|proposal|roadmap|analysis|"
    r"research|blog\s+post|email|summary|report|writeup)\b",
    re.IGNORECASE,
)


def _should_force_code_agent(brief: str) -> bool:
    text = (brief or "").strip()
    if not text:
        # Empty brief — let the LLM planner decide; don't force a code stage.
        return False
    if _TARGET_FILE_PATTERN.search(text):
        return False
    # Hard "I want docs, only docs" signal — short brief leading with
    # write/draft/produce <docs-noun>. Don't force code in that case.
    if _PURE_DOCS_INTENT_PATTERN.search(text):
        return False
    # Strongest signal: explicit code-build phrase ("build an app", "create
    # an API", etc). Original behavior — preserved.
    if any(pattern.search(text) for pattern in _CODE_BUILD_PATTERNS):
        return True
    # Software-specific verbs that are essentially never used about prose.
    # "fix the dashboard" / "refactor the planner" / "redesign the UI" all
    # land here — previously they fell through to a docs-only shape because
    # they lack an explicit software noun like "app".
    if _SOFTWARE_VERB_PATTERN.search(text):
        if _DOCS_ONLY_PATTERN.search(text):
            return False
        return True
    # Ambiguous verbs ("improve", "add", "redesign", "update", "enhance")
    # paired with a software-y object.
    if _IMPROVE_VERB_PATTERN.search(text):
        if _DOCS_ONLY_PATTERN.search(text):
            return False
        return True
    return False


def _mentions_any(text: str, keywords: List[str]) -> bool:
    return any(
        re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text, re.IGNORECASE)
        for keyword in keywords
    )


def _insert_code_stage(chosen_agents: List[str], agent_name: str) -> None:
    if agent_name in chosen_agents:
        return
    insert_at = next(
        (idx for idx, existing in enumerate(chosen_agents) if existing in _CODE_FOLLOWUP_AGENTS),
        len(chosen_agents),
    )
    chosen_agents.insert(insert_at, agent_name)


def _insert_expected_artifact(expected_artifacts: List[str], artifact: str) -> None:
    if artifact in expected_artifacts:
        return
    insert_at = next(
        (
            idx
            for idx, existing in enumerate(expected_artifacts)
            if "review" in str(existing).lower() or "readme" in str(existing).lower()
        ),
        len(expected_artifacts),
    )
    expected_artifacts.insert(insert_at, artifact)


_INTEGRATION_TARGETS = (
    "emby", "jellyfin", "plex", "sonarr", "radarr", "lidarr", "readarr",
    "prowlarr", "qbittorrent", "transmission", "deluge", "sabnzbd",
    "nzbget", "overseerr", "tautulli",
    "sonos", "home assistant", "homeassistant", "hassio", "philips hue",
    "lifx", "nest", "ecobee", "smartthings", "ifttt",
    "unifi", "ubiquiti", "mikrotik", "openwrt", "pfsense", "opnsense",
    "tailscale", "wireguard",
    "docker socket", "docker api", "portainer", "kubernetes api",
    "k8s api", "proxmox", "truenas", "unraid",
    "stripe api", "twilio api", "sendgrid api", "github api",
    "slack api", "discord api", "spotify api", "openweather",
    "rest api", "graphql endpoint", "third-party api", "webhook from",
    "integrate with", "pull from", "talk to the", "query the",
)


def _ensure_research_for_integrations(
    brief: str,
    chosen_agents: List[str],
    expected_artifacts: List[str],
    rationales: Dict[str, str],
) -> tuple[List[str], List[str], Dict[str, str]]:
    """Post-process safety net for integration-heavy briefs.

    If the LLM planner skipped ResearchAgent but the brief names third-
    party services the code must talk to, inject ResearchAgent BEFORE
    ArchitectAgent (or BrainstormAgent if no architect). Without API
    specs from research, CodeAgent fabricates demo data instead of
    real integrations.
    """
    if "ResearchAgent" in chosen_agents:
        return chosen_agents, expected_artifacts, rationales
    b = (brief or "").lower()
    if not any(t in b for t in _INTEGRATION_TARGETS):
        return chosen_agents, expected_artifacts, rationales
    # Find the right spot: before architect if present, else before brainstorm,
    # else at the start.
    insert_at = 0
    for anchor in ("ArchitectAgent", "BrainstormAgent"):
        if anchor in chosen_agents:
            insert_at = chosen_agents.index(anchor) + (0 if anchor == "ArchitectAgent" else 1)
            break
    chosen_agents.insert(insert_at, "ResearchAgent")
    # Mirror the insertion into expected_artifacts so positions line up.
    if insert_at < len(expected_artifacts):
        expected_artifacts.insert(insert_at, "research.md")
    else:
        expected_artifacts.append("research.md")
    rationales.setdefault(
        "ResearchAgent",
        "brief names third-party APIs/services; injected by safety net to "
        "produce integration specs CodeAgent needs",
    )
    return chosen_agents, expected_artifacts, rationales


def _ensure_code_stage(
    brief: str,
    chosen_agents: List[str],
    expected_artifacts: List[str],
    rationales: Dict[str, str],
) -> tuple[List[str], List[str], Dict[str, str]]:
    target_match = _TARGET_FILE_PATTERN.search(brief or "")
    if target_match:
        _insert_code_stage(chosen_agents, "CodeImproverAgent")
        _insert_expected_artifact(expected_artifacts, "(branch+commit)")
        rationales.setdefault(
            "CodeImproverAgent",
            f"brief specifies target_file: {target_match.group(0)[:60]}",
        )
        return chosen_agents, expected_artifacts, rationales

    if "CodeImproverAgent" in chosen_agents or "CodeAgent" in chosen_agents:
        return chosen_agents, expected_artifacts, rationales

    if _should_force_code_agent(brief):
        _insert_code_stage(chosen_agents, "CodeAgent")
        _insert_expected_artifact(expected_artifacts, "(source files)")
        rationales.setdefault(
            "CodeAgent",
            "brief asks SkyN3t to build working software, so the plan must include code output",
        )
    return chosen_agents, expected_artifacts, rationales
