"""Project Studio templates.

Each :class:`Template` describes an ordered pipeline of specialist agents
that collaborate to assemble a ready-to-use project folder.

Stage specs reference agents by class name (a string) so they can be
resolved lazily via :mod:`skyn3t.studio.registry`.  This keeps the
templates module decoupled from the actual agent implementations -
useful while specialist agents are being authored in parallel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StageSpec:
    """A single stage in a project template pipeline."""

    name: str
    agent: str  # agent class name; resolved lazily
    capability: str
    input_extra: Dict[str, Any] = field(default_factory=dict)
    handoff_to: Optional[str] = None  # name of next stage's agent


@dataclass
class Template:
    """An ordered project template made up of stage specs."""

    key: str
    title: str
    description: str
    stages: List[StageSpec]
    artifacts_root: str = "projects"


def _wire_handoffs(stages: List[StageSpec]) -> List[StageSpec]:
    """Set ``handoff_to`` on each stage to point at the next stage's agent."""
    for i, stage in enumerate(stages):
        stage.handoff_to = stages[i + 1].agent if i + 1 < len(stages) else None
    return stages


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES: List[Template] = [
    Template(
        key="app_saas",
        title="App/SaaS scaffold",
        description=(
            "Research the problem space, design an architecture, scaffold "
            "the codebase, write a README, polish the design, and review."
        ),
        stages=_wire_handoffs(
            [
                StageSpec(name="brainstorm", agent="BrainstormAgent", capability="brainstorm"),
                StageSpec(name="research", agent="ResearchAgent", capability="research"),
                StageSpec(name="architecture", agent="ArchitectAgent", capability="architecture"),
                StageSpec(name="designer", agent="DesignerAgent", capability="design"),
                StageSpec(
                    name="code",
                    agent="CodeAgent",
                    capability="code",
                    input_extra={"kind": "scaffold"},
                ),
                StageSpec(
                    name="writer",
                    agent="WriterAgent",
                    capability="writing",
                    input_extra={"kind": "readme"},
                ),
                StageSpec(name="reviewer", agent="ReviewerAgent", capability="review"),
            ]
        ),
    ),
    Template(
        key="marketing",
        title="Marketing campaign",
        description=(
            "Run audience research, develop positioning, draft a campaign, "
            "produce landing copy, and review the result."
        ),
        stages=_wire_handoffs(
            [
                StageSpec(name="brainstorm", agent="BrainstormAgent", capability="brainstorm"),
                StageSpec(name="research", agent="ResearchAgent", capability="research"),
                StageSpec(
                    name="business_analyst",
                    agent="BusinessAnalystAgent",
                    capability="business_analysis",
                    input_extra={"kind": "positioning"},
                ),
                StageSpec(name="marketer", agent="MarketerAgent", capability="marketing"),
                StageSpec(
                    name="writer",
                    agent="WriterAgent",
                    capability="writing",
                    input_extra={"kind": "landing_copy"},
                ),
                StageSpec(name="reviewer", agent="ReviewerAgent", capability="review"),
            ]
        ),
    ),
    Template(
        key="business_site",
        title="Business website",
        description=(
            "Research the business, define a minimal visual mood, plan the "
            "site architecture, write copy, build a static site, and review."
        ),
        stages=_wire_handoffs(
            [
                StageSpec(name="brainstorm", agent="BrainstormAgent", capability="brainstorm"),
                StageSpec(name="research", agent="ResearchAgent", capability="research"),
                StageSpec(
                    name="designer",
                    agent="DesignerAgent",
                    capability="design",
                    input_extra={"mood": "minimal"},
                ),
                StageSpec(
                    name="architect",
                    agent="ArchitectAgent",
                    capability="architecture",
                    input_extra={"target": "site"},
                ),
                StageSpec(
                    name="writer",
                    agent="WriterAgent",
                    capability="writing",
                    input_extra={"kind": "landing_copy"},
                ),
                StageSpec(
                    name="code",
                    agent="CodeAgent",
                    capability="code",
                    input_extra={"kind": "static_site"},
                ),
                StageSpec(name="reviewer", agent="ReviewerAgent", capability="review"),
            ]
        ),
    ),
    Template(
        key="brand_kit",
        title="Brand & design kit",
        description=(
            "Research the brand context, design a visual identity, write a "
            "brand voice guide, and review."
        ),
        stages=_wire_handoffs(
            [
                StageSpec(name="brainstorm", agent="BrainstormAgent", capability="brainstorm"),
                StageSpec(name="research", agent="ResearchAgent", capability="research"),
                StageSpec(name="designer", agent="DesignerAgent", capability="design"),
                StageSpec(
                    name="writer",
                    agent="WriterAgent",
                    capability="writing",
                    input_extra={"kind": "brand_voice_guide"},
                ),
                StageSpec(name="reviewer", agent="ReviewerAgent", capability="review"),
            ]
        ),
    ),
    Template(
        key="business_plan",
        title="Business plan",
        description=(
            "Research the market, build a full business plan, write an "
            "executive summary, draft go-to-market notes, and review."
        ),
        stages=_wire_handoffs(
            [
                StageSpec(name="brainstorm", agent="BrainstormAgent", capability="brainstorm"),
                StageSpec(name="research", agent="ResearchAgent", capability="research"),
                StageSpec(
                    name="business_analyst",
                    agent="BusinessAnalystAgent",
                    capability="business_analysis",
                    input_extra={"kind": "full_plan"},
                ),
                StageSpec(
                    name="writer",
                    agent="WriterAgent",
                    capability="writing",
                    input_extra={"kind": "executive_summary"},
                ),
                StageSpec(name="marketer", agent="MarketerAgent", capability="marketing"),
                StageSpec(name="reviewer", agent="ReviewerAgent", capability="review"),
            ]
        ),
    ),
    Template(
        key="product_idea",
        title="Product idea spec",
        description=(
            "Research a product idea, scan the market, sketch the "
            "architecture, write a spec, and review."
        ),
        stages=_wire_handoffs(
            [
                StageSpec(name="brainstorm", agent="BrainstormAgent", capability="brainstorm"),
                StageSpec(name="research", agent="ResearchAgent", capability="research"),
                StageSpec(
                    name="business_analyst",
                    agent="BusinessAnalystAgent",
                    capability="business_analysis",
                    input_extra={"kind": "market_scan"},
                ),
                StageSpec(name="architect", agent="ArchitectAgent", capability="architecture"),
                StageSpec(
                    name="writer",
                    agent="WriterAgent",
                    capability="writing",
                    input_extra={"kind": "spec"},
                ),
                StageSpec(name="reviewer", agent="ReviewerAgent", capability="review"),
            ]
        ),
    ),
    Template(
        key="frontend_redesign",
        title="Frontend redesign",
        description=(
            "Targeted redesign of an existing front-end file. Brainstorm "
            "goals, audit the current code, design IA, ship a unified diff "
            "for review."
        ),
        stages=_wire_handoffs(
            [
                StageSpec(name="brainstorm", agent="BrainstormAgent", capability="brainstorm"),
                StageSpec(name="research", agent="ResearchAgent", capability="research"),
                StageSpec(name="architect", agent="ArchitectAgent", capability="architecture"),
                StageSpec(name="designer", agent="DesignerAgent", capability="design"),
                StageSpec(
                    name="code",
                    agent="CodeImproverAgent",
                    capability="code_improvement",
                    input_extra={"intent": "frontend_redesign"},
                ),
                StageSpec(name="reviewer", agent="ReviewerAgent", capability="review"),
            ]
        ),
    ),
]


TEMPLATES.append(Template(
    key="auto",
    title="Auto (planner picks agents)",
    description=(
        "The planner reads your brief and chooses which agents to run. "
        "Use this for any free-form project that doesn't fit a fixed template."
    ),
    stages=[],   # empty signals dynamic mode
))


def get_template(key: str) -> Template:
    """Return the template registered under ``key`` or raise ``KeyError``."""
    for t in TEMPLATES:
        if t.key == key:
            return t
    raise KeyError(key)


def list_templates() -> List[Dict[str, Any]]:
    """Return a JSON-friendly summary of all available templates."""
    return [
        {
            "key": t.key,
            "title": t.title,
            "description": t.description,
            "stages": [s.name for s in t.stages],
        }
        for t in TEMPLATES
    ]
