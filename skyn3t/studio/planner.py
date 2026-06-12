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
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from skyn3t.studio.clarification import apply_user_intent_plan, skip_force_code_for_intent

# Consumes ReflectionPlannerHook (intelligence.reflection.RetryDirective). The
# import is defensive: M3_reflection owns reflection.py and may add
# RetryDirective concurrently. We never want a half-applied edit there to break
# the planner's import (and thus the whole suite), so we degrade to a runtime
# fallback when the symbol isn't importable yet. plan_pipeline accepts the
# directive structurally (duck-typed) so behavior is identical either way.
if TYPE_CHECKING:  # pragma: no cover - typing only
    from skyn3t.intelligence.reflection import RetryDirective
else:  # pragma: no cover - import guard
    try:
        from skyn3t.intelligence.reflection import RetryDirective  # noqa: F401
    except Exception:  # reflection.py mid-edit or symbol not yet present
        RetryDirective = None  # type: ignore[assignment,misc]

logger = logging.getLogger("skyn3t.studio.planner")
_TARGET_FILE_PATTERN = re.compile(
    r"\b([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.\w+|target_file\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
# v44: Only match EXPLICIT user directives like "target_file: src/app.py".
# Build-error hints often contain file paths (e.g. "server/adapters/sonos.js")
# but those are NOT user requests to patch a specific file.  Using the broad
# _TARGET_FILE_PATTERN for those was causing retries to skip CodeAgent and
# use CodeImproverAgent on a non-existent scaffold.
_EXPLICIT_TARGET_FILE_PATTERN = re.compile(
    r"target_file\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_CODE_TECH_SIGNAL_PATTERN = re.compile(
    r"\b(?:source\s+code|source\s+files?|frontend|backend|api|endpoint|function|class|component|schema|migration|html|css|javascript|typescript|python|fastapi|react|next(?:\.js)?|node(?:\.js)?|cli)\b",
    re.IGNORECASE,
)
_CODE_BUILD_VERB_PATTERN = re.compile(
    # Build/create/etc near the start of the brief — treat as code build
    # unless explicitly contradicted by docs-only signals (checked
    # separately). The old version required a known software noun like
    # "app" / "tool" / "dashboard" which broke on natural phrasings like
    # "build a homelab uploader" (uploader isn't a known noun) or
    # "build me a budget tracker" (tracker isn't either). The fix: the
    # verb itself is enough signal. Docs detection elsewhere catches
    # genuine "write a README" cases.
    r"\b(?:build|create|make|ship|launch|scaffold|generate|prototype|develop|implement|spin\s+up|kick\s+off)"
    r"\s+(?:me\s+)?(?:a|an|the|some|my|us|something)\b",
    re.IGNORECASE,
)
_CODE_BUILD_PATTERNS = (_CODE_TECH_SIGNAL_PATTERN, _CODE_BUILD_VERB_PATTERN)
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


async def plan_pipeline(
    *,
    brief: str,
    llm_client=None,
    user_intent: Optional[Dict[str, Any]] = None,
    reflection: "RetryDirective | None" = None,
) -> List[PlannedStage]:
    """Pick stages relevant to this brief. Brainstorm first + Reviewer last
    are always included; in-between is dynamic.

    ``reflection`` (ReflectionPlannerHook) is an optional ``RetryDirective``
    produced by ``intelligence.reflection.build_retry_directive`` on a retry.
    When supplied it makes the plan *reason about WHY the prior attempt
    failed* instead of blindly replanning:

      * ``augmented_brief`` is merged into the brief used for stage selection
        (so failure-aware cues influence which agents are chosen).
      * Failure ``signatures`` that indicate stub/entrypoint problems force a
        ``CodeAgent`` stage and bias toward a ``ReviewerAgent`` quality gate.
      * ``prompt_patches`` are threaded into the affected stages' rationales
        and expected artifacts so downstream agents actually see them.

    When ``reflection`` is ``None`` (the default, and the non-retry path) the
    behavior is byte-for-byte identical to before — this is a pure superset.
    """
    # Reflection-aware brief: stage selection (heuristic + LLM) keys off the
    # brief text, so merging the directive's augmented brief here is what lets
    # the WHY of the failure bias agent choice. The original brief is kept for
    # rationale/reporting clarity.
    planning_brief = _merge_reflection_brief(brief, reflection)

    chosen_agents: List[str] = []
    expected_artifacts: List[str] = []
    rationales: Dict[str, str] = {}

    if llm_client is not None:
        try:
            chosen_agents, expected_artifacts, rationales = await _llm_plan(planning_brief, llm_client)
        except Exception:
            logger.exception("LLM planner failed; falling back to heuristic")

    if not chosen_agents:
        chosen_agents, expected_artifacts, rationales = _heuristic_plan(planning_brief)

    # Safety net: even if the LLM planner ignored the integration cue,
    # post-process the plan to add ResearchAgent for briefs that name
    # third-party APIs/services the code must talk to. Without research,
    # CodeAgent will fabricate fake demo data for those integrations.
    chosen_agents, expected_artifacts, rationales = _ensure_research_for_integrations(
        planning_brief, chosen_agents, expected_artifacts, rationales,
    )

    chosen_agents, expected_artifacts, rationales = _ensure_code_stage(
        planning_brief,
        chosen_agents,
        expected_artifacts,
        rationales,
        user_intent=user_intent,
    )

    # ReflectionPlannerHook: bias agent selection from the retry directive
    # (force code stage / stronger reviewer for stub/entrypoint failures,
    # thread prompt patches into rationales + expected artifacts). Inert
    # no-op when reflection is None.
    chosen_agents, expected_artifacts, rationales = _apply_reflection_bias(
        reflection, chosen_agents, expected_artifacts, rationales,
    )

    # Strip DesignerAgent when the brief ALREADY locks the visual
    # direction (UI library + reference app or aesthetic descriptor).
    # The LLM planner reliably includes Designer for any visual-sounding
    # brief, but when the brief itself specifies Tailwind + Homarr +
    # rounded-cards + dark-theme, designer just rephrases the brief in
    # JSON tokens — costing 3-5 min for redundant output.
    chosen_agents, expected_artifacts, rationales = _strip_redundant_designer(
        planning_brief, chosen_agents, expected_artifacts, rationales,
    )

    chosen_agents, expected_artifacts, rationales = apply_user_intent_plan(
        user_intent,
        chosen_agents,
        expected_artifacts,
        rationales,
    )

    chosen_agents, expected_artifacts, rationales = _strip_optional_marketing_agents(
        planning_brief,
        chosen_agents,
        expected_artifacts,
        rationales,
        user_intent=user_intent,
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
    # Map agent_name -> planner-provided artifact. The planner output
    # (LLM or heuristic) gives us a *positional* list aligned with the
    # pre-dedupe chosen_agents; after dedupe we look back by agent
    # name. If the planner specified an artifact for an agent, prefer
    # it over the catalog default — otherwise BR-001: artifact-specific
    # outputs like Dockerfile/Makefile get silently dropped.
    artifact_overrides: Dict[str, str] = {}
    if expected_artifacts:
        for agent_name, override in zip(chosen_agents, expected_artifacts):
            if override and agent_name not in artifact_overrides:
                artifact_overrides[agent_name] = override
    for i, agent_name in enumerate(chosen_agents):
        catalog_entry = by_agent.get(agent_name, {})
        cap = (catalog_entry.get("capabilities") or ["general"])[0]
        expected = artifact_overrides.get(agent_name) or catalog_entry.get("artifact", "")
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


# --- ReflectionPlannerHook integration -----------------------------------
# Signature fragments that mean "the build produced an empty/stub module or a
# missing/broken entrypoint." When the prior failure carries one of these, the
# right move is to (re)run a real CodeAgent build and tighten the review gate —
# NOT to settle for a docs/spec plan. Matched case-insensitively against the
# directive's signatures + rationale.
_STUB_ENTRYPOINT_SIGNATURE_PATTERN = re.compile(
    r"stub|placeholder|not.?implemented|todo|"
    r"empty\s+(?:file|module|scaffold)|no\s+(?:source|code|files?)|"
    r"entry.?point|entrypoint|missing\s+main|"
    r"importerror|modulenotfound|cannot\s+import|no\s+module\s+named|"
    r"won'?t\s+(?:start|run|launch)|fail(?:ed|s)?\s+to\s+(?:start|launch|boot)",
    re.IGNORECASE,
)


def _reflection_str_list(value: Any) -> List[str]:
    """Coerce a directive list-ish field into a clean list of strings.

    Duck-typed: works whether RetryDirective is the real dataclass or any
    stand-in object, and tolerates None / scalars / mixed iterables.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    try:
        items = list(value)
    except TypeError:
        return [str(value)]
    return [str(item) for item in items if str(item).strip()]


def _merge_reflection_brief(brief: str, reflection: Any) -> str:
    """Return the brief to plan against, merging the directive's augmented brief.

    No-op (returns the original brief) when reflection is None or carries no
    augmented brief. The augmented brief from build_retry_directive already
    encodes the original brief plus failure-aware guidance, so when present we
    plan against it directly; we only fall back to concatenation if it somehow
    doesn't contain the original text (defensive).
    """
    if reflection is None:
        return brief
    augmented = getattr(reflection, "augmented_brief", None)
    if not augmented or not str(augmented).strip():
        return brief
    augmented = str(augmented)
    base = str(brief or "")
    if base and base.strip() and base.strip() not in augmented:
        return f"{base}\n\n{augmented}"
    return augmented


def _apply_reflection_bias(
    reflection: Any,
    chosen_agents: List[str],
    expected_artifacts: List[str],
    rationales: Dict[str, str],
) -> tuple[List[str], List[str], Dict[str, str]]:
    """Bias the plan from a RetryDirective. Inert no-op when reflection is None.

    1. Stub/entrypoint failure signatures -> force a CodeAgent stage (a real
       build, not docs) and guarantee a ReviewerAgent quality gate (it is
       re-appended at the end of plan_pipeline anyway, but we seed a stronger
       rationale so the dashboard explains the tightened review).
    2. forced_stage_tier -> recorded in the relevant stage rationale so the
       downstream selector/runner can honor the tier hint.
    3. prompt_patches -> threaded into the rationales of the build/review
       stages AND appended to their expected_artifacts, so downstream agents
       (and the dashboard) actually see the corrective guidance.
    """
    if reflection is None:
        return chosen_agents, expected_artifacts, rationales

    signatures = _reflection_str_list(getattr(reflection, "signatures", None))
    rationale_text = str(getattr(reflection, "rationale", "") or "")
    prompt_patches = _reflection_str_list(getattr(reflection, "prompt_patches", None))
    forced_tier = getattr(reflection, "forced_stage_tier", None)
    if not isinstance(forced_tier, dict):
        forced_tier = {}

    haystack = " \n ".join(signatures + [rationale_text])
    stub_or_entrypoint = bool(
        haystack.strip() and _STUB_ENTRYPOINT_SIGNATURE_PATTERN.search(haystack)
    )

    # 1. Stub/entrypoint failures: force a real build + stronger review.
    if stub_or_entrypoint:
        has_code = "CodeAgent" in chosen_agents or "CodeImproverAgent" in chosen_agents
        if not has_code:
            _insert_code_stage(chosen_agents, "CodeAgent")
            _insert_expected_artifact(expected_artifacts, "(source files)")
        # Build a precise rationale; signatures (if any) make the WHY explicit.
        sig_note = f" (signatures: {', '.join(signatures[:3])})" if signatures else ""
        code_target = "CodeAgent" if "CodeAgent" in chosen_agents else "CodeImproverAgent"
        rationales[code_target] = (
            "reflective retry: prior attempt produced a stub/empty scaffold or a "
            f"missing entrypoint, so the plan forces a real code build{sig_note}"
        ).strip()
        rationales["ReviewerAgent"] = (
            "reflective retry: stronger review gate — prior build failed on a "
            "stub/entrypoint issue, so reviewer must confirm a runnable entrypoint "
            "and non-empty source before passing"
        )

    # 2. Surface forced tier hints in the affected stages' rationales.
    for stage_name, tier in forced_tier.items():
        agent_name = _reflection_stage_to_agent(str(stage_name), chosen_agents)
        if not agent_name:
            continue
        note = f"reflective retry: forced tier '{tier}' for stage '{stage_name}'"
        existing = rationales.get(agent_name, "")
        rationales[agent_name] = f"{existing} | {note}".strip(" |") if existing else note

    # 3. Thread prompt patches into the build/review stages so downstream
    #    agents see them. We write into the ``rationales`` dict keyed by agent
    #    name — that dict is consulted when stages are materialized later in
    #    plan_pipeline, so targeting an agent that isn't in chosen_agents yet
    #    (notably ReviewerAgent, which is *always* re-appended at the end)
    #    still surfaces the patch on its stage.
    if prompt_patches:
        patch_blob = "; ".join(prompt_patches)
        patch_note = f"reflection patches: {patch_blob}"
        # ReviewerAgent is always present in the final plan; the code agents
        # are present whenever this build produces code. Target the canonical
        # downstream consumers regardless of current membership.
        targets = ["CodeAgent", "CodeImproverAgent", "ReviewerAgent"]
        present_code = {a for a in ("CodeAgent", "CodeImproverAgent") if a in chosen_agents}
        for agent_name in targets:
            # Skip a code agent that isn't (and won't be) in this plan, so we
            # don't seed an orphan rationale for a stage that never runs.
            if agent_name in ("CodeAgent", "CodeImproverAgent") and agent_name not in present_code:
                continue
            existing = rationales.get(agent_name, "")
            rationales[agent_name] = (
                f"{existing} | {patch_note}".strip(" |") if existing else patch_note
            )
        # Record the patches once on a private channel too, so they are
        # visible even when no canonical target ran.
        rationales.setdefault("_reflection_prompt_patches", patch_blob)

    return chosen_agents, expected_artifacts, rationales


def _reflection_stage_to_agent(
    stage_name: str, chosen_agents: List[str]
) -> Optional[str]:
    """Map a directive stage key (e.g. 'code', 'reviewer', 'architect') to a
    chosen agent class name. Returns None when no chosen agent matches."""
    key = re.sub(r"agent$", "", stage_name.strip().lower())
    if not key:
        return None
    # ReviewerAgent is always re-appended to the final plan, so resolve tier
    # hints for it even before it appears in chosen_agents (its rationale is
    # keyed by name and picked up at stage-build time).
    always_present = {"ReviewerAgent"}
    # Direct: 'codeagent' style or exact agent-name match.
    for agent in chosen_agents:
        if re.sub(r"agent$", "", agent.lower()) == key:
            return agent
    # Aliases for common stage keys -> agent class names.
    aliases = {
        "code": "CodeAgent",
        "codegen": "CodeAgent",
        "build": "CodeAgent",
        "improver": "CodeImproverAgent",
        "patch": "CodeImproverAgent",
        "review": "ReviewerAgent",
        "reviewer": "ReviewerAgent",
        "qa": "ReviewerAgent",
        "architect": "ArchitectAgent",
        "design": "DesignerAgent",
        "designer": "DesignerAgent",
        "research": "ResearchAgent",
        "writer": "WriterAgent",
    }
    candidate = aliases.get(key)
    if candidate and (candidate in chosen_agents or candidate in always_present):
        return candidate
    return None


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

    # Architect: include for any software build OR for briefs that
    # explicitly mention architecture/system concepts.
    needs_software_build = _should_force_code_agent(brief)
    needs_arch = needs_software_build or _mentions_any(
        b,
        _SOFTWARE_ARCHITECTURE_KEYWORDS,
    )
    if needs_arch:
        chosen.append("ArchitectAgent")
        arts.append("architecture.md")
        why["ArchitectAgent"] = (
            "every software build needs a design doc before code"
            if needs_software_build
            else "brief implies a software/system component"
        )

    # Designer: include for any UI-bearing software build OR explicit
    # visual/brand cues. A "build a habit tracker" brief without "design"
    # in it still ships UI, so it needs a brand/palette/typography pass
    # — otherwise CodeAgent generates raw HTML with no design system.
    needs_design = needs_software_build or _mentions_any(
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
    design_already_specified = _mentions_any(
        b, ["tailwind", "shadcn", "chakra", "material ui", "material-ui",
            "mui", "ant design", "bootstrap", "tokens.css", "design tokens"]
    ) and _mentions_any(
        b, [
            # Reference apps with known aesthetic
            "homarr", "heimdall", "dashy", "linear-style", "linear style",
            "vercel-style", "vercel style", "stripe-style", "notion-style",
            "supabase-style",
            # Concrete aesthetic descriptors that lock the look
            "rounded cards", "soft shadows", "dark theme", "light theme",
            "glassmorphism", "neumorphism", "brutalist", "minimalist",
            "monospace",
        ]
    )
    if needs_design and (needs_software_build or not design_already_specified):
        chosen.append("DesignerAgent")
        arts.append("brand.md")
        why["DesignerAgent"] = "brief asks for visual/brand work"
    elif needs_design and design_already_specified:
        # The brief locked the aesthetic itself; record why we skipped so
        # the dashboard's stage rationale tells the truth.
        why["DesignerAgent_skipped"] = (
            "brief already specifies UI library + aesthetic — designer "
            "output would just rephrase what the brief said. Saves ~3-5 min."
        )

    needs_marketing = _explicit_marketing_brief(b)
    if needs_marketing:
        chosen.append("MarketerAgent")
        arts.append("positioning.md")
        why["MarketerAgent"] = "brief asks for GTM/marketing deliverables"

    needs_biz = _mentions_any(
        b,
        ["business", "revenue", "pricing", "model", "monetiz", "icp", "tam", "competitors", "investor", "pitch"],
    )
    if needs_biz:
        chosen.append("BusinessAnalystAgent")
        arts.append("market_scan.md")
        why["BusinessAnalystAgent"] = "brief mentions business model/pricing/strategy"

    target_match = _EXPLICIT_TARGET_FILE_PATTERN.search(brief or "")
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
        "Include DesignerAgent for any software brief that builds UI "
        "(app/site/dashboard/page/frontend), even if style is already specified; "
        "this stage improves polish and interaction consistency. "
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
    raw_agents = data.get("agents") or []
    agents: List[str] = []
    artifacts: List[str] = []
    rationale: Dict[str, str] = {}
    # Kimi sometimes returns [{"name": "BrainstormAgent", "reason": "...", "produces": "..."}]
    # instead of the requested ["BrainstormAgent", ...]. Normalize either shape.
    for entry in raw_agents:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("agent")
            if not name:
                continue
            name = str(name)
            agents.append(name)
            produced = entry.get("produces") or entry.get("artifact") or entry.get("expected_artifact")
            if produced:
                artifacts.append(str(produced))
            reason = entry.get("reason") or entry.get("rationale") or entry.get("why")
            if reason:
                rationale[name] = str(reason)
        else:
            agents.append(str(entry))
    # Top-level expected_artifacts/rationale (the originally-requested shape)
    # only kicks in when the per-agent dicts didn't carry them.
    for a in (data.get("expected_artifacts") or []):
        if not isinstance(a, dict):
            artifacts.append(str(a))
    for k, v in (data.get("rationale") or {}).items():
        rationale.setdefault(str(k), str(v))
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
    # Hard "I want docs, only docs" signal — short brief leading with
    # write/draft/produce <docs-noun>. Don't force code in that case.
    if _PURE_DOCS_INTENT_PATTERN.search(text):
        return False
    # Explicit non-software domain words override every code-build signal
    # below — "build a launch campaign", "build a marketing plan" are NOT
    # code builds even though they start with "build a".
    _NON_SOFTWARE_OBJECTS = re.compile(
        r"\b(?:campaign|marketing\s+plan|launch\s+plan|gtm|strategy|"
        r"playbook|deck|pitch|proposal|roadmap\s+document|case\s+study|"
        r"copy(?:writing)?|content\s+plan|brand\s+strategy)\b",
        re.IGNORECASE,
    )
    if _NON_SOFTWARE_OBJECTS.search(text):
        return False
    # Strongest signal: explicit code-build phrase ("build an app", "create
    # an API", etc). Keep this ahead of path checks so retry hints like
    # "add dependency to server/package.json" don't accidentally suppress a
    # real build brief.
    if _CODE_BUILD_VERB_PATTERN.search(text):
        return True
    # Skip force-code-agent when the brief names a specific file path and
    # there wasn't a direct build phrase — that's usually patch/improver
    # territory, not a fresh scaffold request.
    if _TARGET_FILE_PATTERN.search(text):
        return False
    # Strongest signal: explicit code-build phrase ("build an app", "create
    # an API", etc) OR strong technology signal words.
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


def _user_brief_portion(brief: str) -> str:
    """Strip auto-appended mission/clarification blocks before keyword scans."""
    text = str(brief or "")
    for marker in ("\n## Mission setup", "\n## User clarifications", "\n---"):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
    return text.strip()


_MARKETING_BRIEF_KEYWORDS: List[str] = [
    "go-to-market",
    "go to market",
    "gtm",
    "marketing plan",
    "marketing strategy",
    "marketing campaign",
    "marketing site",
    "product hunt",
    "channel plan",
    "growth marketing",
    "launch campaign",
    "launch plan",
    "landing page copy",
    "positioning doc",
    "positioning statement",
]


def _explicit_marketing_brief(brief: str) -> bool:
    portion = _user_brief_portion(brief).lower()
    if _mentions_any(portion, _MARKETING_BRIEF_KEYWORDS):
        return True
    return _mentions_any(portion, ["marketing", "positioning", "gtm"])


def _strip_optional_marketing_agents(
    brief: str,
    chosen_agents: List[str],
    expected_artifacts: List[str],
    rationales: Dict[str, str],
    *,
    user_intent: Optional[Dict[str, Any]] = None,
) -> tuple[List[str], List[str], Dict[str, str]]:
    """Drop GTM agents from code-first builds unless the user explicitly asked."""
    deliverable = str((user_intent or {}).get("deliverable_kind") or "").strip().lower()
    if deliverable == "content" or _explicit_marketing_brief(brief):
        return chosen_agents, expected_artifacts, rationales
    has_code = "CodeAgent" in chosen_agents or "CodeImproverAgent" in chosen_agents
    if not has_code:
        return chosen_agents, expected_artifacts, rationales
    for agent in ("MarketerAgent", "BusinessAnalystAgent"):
        if agent not in chosen_agents:
            continue
        chosen_agents = [name for name in chosen_agents if name != agent]
        rationales.pop(agent, None)
        rationales[f"{agent}_skipped"] = (
            "code-first build without an explicit GTM/marketing ask"
        )
    drop_artifacts = {"positioning.md", "channel_plan.md", "market_scan.md", "launch_checklist.md"}
    expected_artifacts = [
        artifact
        for artifact in expected_artifacts
        if str(artifact).strip().lower() not in drop_artifacts
    ]
    return chosen_agents, expected_artifacts, rationales


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
    *,
    user_intent: Optional[Dict[str, Any]] = None,
) -> tuple[List[str], List[str], Dict[str, str]]:
    target_match = _EXPLICIT_TARGET_FILE_PATTERN.search(brief or "")
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

    if skip_force_code_for_intent(user_intent):
        return chosen_agents, expected_artifacts, rationales

    if _should_force_code_agent(brief):
        _insert_code_stage(chosen_agents, "CodeAgent")
        _insert_expected_artifact(expected_artifacts, "(source files)")
        rationales.setdefault(
            "CodeAgent",
            "brief asks SkyN3t to build working software, so the plan must include code output",
        )
    return chosen_agents, expected_artifacts, rationales


# Strong "the brief itself locks the visual direction" signals. When
# both groups match, the designer stage produces 7+ files (palette,
# brand, components, tokens, logo, etc.) that just rephrase what the
# brief already specified. ~3-5 min of wall-time savings per matching
# brief, no quality loss.

_UI_LIBRARY_KEYWORDS: List[str] = [
    "tailwind", "shadcn", "chakra", "material ui", "material-ui",
    "mui", "ant design", "bootstrap", "tokens.css", "design tokens",
    "headlessui", "headless ui", "radix", "mantine",
    # Frameworks that already constrain the UI shape enough that
    # designer-stage output is mostly redundant when paired with an
    # aesthetic descriptor in the brief.
    "vite + react", "vite+react", "react + vite", "react+vite",
    "next.js", "nextjs", "remix", "svelte", "sveltekit", "solid",
    "qwik", "astro",
]

_AESTHETIC_LOCK_KEYWORDS: List[str] = [
    # Reference apps with a known aesthetic
    "homarr", "heimdall", "dashy", "linear-style", "linear style",
    "vercel-style", "vercel style", "stripe-style", "notion-style",
    "supabase-style",
    # Concrete aesthetic descriptors
    "rounded cards", "soft shadows", "dark theme", "light theme",
    "glassmorphism", "neumorphism", "brutalist", "minimalist",
    "monospace", "cyberpunk", "skeuomorphic",
]


def _strip_redundant_designer(
    brief: str,
    chosen_agents: List[str],
    expected_artifacts: List[str],
    rationales: Dict[str, str],
) -> tuple[List[str], List[str], Dict[str, str]]:
    """Remove DesignerAgent from the plan when the brief itself already
    fully specifies the visual direction.

    Behavior preserved when:
      - Designer wasn't in the plan to begin with (no-op).
      - Brief lacks an explicit UI library OR a concrete aesthetic
        signal (designer's output is genuinely useful).
    """
    if "DesignerAgent" not in chosen_agents:
        return chosen_agents, expected_artifacts, rationales
    b = (brief or "").lower()
    has_ui_lib = _mentions_any(b, _UI_LIBRARY_KEYWORDS)
    has_aesthetic = _mentions_any(b, _AESTHETIC_LOCK_KEYWORDS)
    if not (has_ui_lib and has_aesthetic):
        return chosen_agents, expected_artifacts, rationales
    # Do not strip designer for software builds: these briefs need
    # dedicated visual pass for state handling and component consistency.
    if _should_force_code_agent(brief):
        return chosen_agents, expected_artifacts, rationales
    # Strip designer + its brand artifact entries. Artifacts can be
    # listed multiple ways (brand.md, palette.json, components.md);
    # drop the standard set if present.
    designer_arts = {
        "brand.md", "palette.json", "components.md",
        "tokens.css", "tokens.json", "logo.svg",
    }
    chosen_agents = [a for a in chosen_agents if a != "DesignerAgent"]
    expected_artifacts = [a for a in expected_artifacts if a not in designer_arts]
    rationales.pop("DesignerAgent", None)
    rationales["DesignerAgent_skipped"] = (
        "brief locks UI library + aesthetic — designer would only "
        "rephrase the brief's own visual instructions. Saves ~3-5 min."
    )
    return chosen_agents, expected_artifacts, rationales
