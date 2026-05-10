"""Business Analyst Agent - market scan, business model, pitch outline.

Tries the configured LLM first to produce brief-aware market analysis,
business model recommendations, and a pitch outline. Falls back to the
deterministic templates below (revenue model picked from brief keywords,
seeded competitor table, generic 10-slide pitch) when the LLM is unavailable
or returns a stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

_REVENUE_MODELS = {
    "subscription": {
        "label": "Subscription",
        "rationale": "Recurring value + predictable usage. Pricing scales with seats or workspaces.",
    },
    "usage": {
        "label": "Usage-based",
        "rationale": "Cost-of-goods scales with volume; customers prefer pay-as-you-go.",
    },
    "freemium": {
        "label": "Freemium",
        "rationale": "Low CAC at the top of funnel; conversion gated on collaboration or limits.",
    },
}


def _pick_revenue_model(brief: str) -> str:
    b = brief.lower()
    if any(k in b for k in ("api", "credits", "tokens", "minutes", "requests", "compute")):
        return "usage"
    if any(k in b for k in ("free", "community", "open source", "viral", "share")):
        return "freemium"
    return "subscription"


def _seed_competitors(brief: str) -> List[Dict[str, str]]:
    """Generate plausible-sounding competitor placeholders from brief keywords."""
    words = [w.strip(",.") for w in brief.split() if len(w) > 3]
    seeds = (words[:3] + ["Flow", "Bridge", "Atlas", "Lumen", "Pilot"])[:8]
    names = [
        f"{seeds[0].capitalize()}Hub",
        f"{seeds[1 % len(seeds)].capitalize()}Stack",
        f"Open{seeds[2 % len(seeds)].capitalize()}",
        f"{seeds[3 % len(seeds)].capitalize()}Studio",
        f"{seeds[4 % len(seeds)].capitalize()}Cloud",
    ]
    descriptions = [
        "Established incumbent; broad surface area, slow shipping cadence.",
        "Mid-market favorite; strong integrations, weaker UX polish.",
        "Open-source upstart; great for tinkerers, no managed offering.",
        "Design-led entrant; beautiful product, missing enterprise features.",
        "Platform play; integrates with hyperscalers, complex pricing.",
    ]
    return [{"name": n, "desc": d} for n, d in zip(names, descriptions)]


class BusinessAnalystAgent(BaseAgent):
    """Produces market_scan.md, business_model.md, and pitch_outline.md."""

    def __init__(
        self,
        name: str = "business_analyst",
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="business_analyst",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(AgentCapability(
            name="business_analysis",
            description="Market scan, business model selection, pitch outline.",
            parameters={"brief": "str", "idea": "str"},
        ))
        self.add_capability(AgentCapability(
            name="strategy",
            description="Pick revenue model + tier structure + back-of-envelope unit economics.",
            parameters={"brief": "str"},
        ))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")

        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip() or "Untitled project"
        idea: str = (data.get("idea") or brief).strip() or brief
        artifact_dir = Path(data.get("artifact_dir") or ".")
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        comps = _seed_competitors(brief)
        revenue_key = _pick_revenue_model(brief)
        revenue = _REVENUE_MODELS[revenue_key]

        # STEP 0: try LLM for market_scan.md, fall back to deterministic template.
        market_role_prompt = (
            "You are a startup market analyst. Research and identify 5 specific "
            "competitor products (use real names if the space is obvious; otherwise "
            "use plausible named placeholders). Produce a markdown market scan with "
            "these sections (## headings):\n"
            "- TAM / SAM / SOM with rough numeric estimates and how you got there\n"
            "- ICP (ideal customer profile) description\n"
            "- 5 named competitors, each with a one-line gap analysis\n"
            "- Feature comparison table (markdown table) with us vs each competitor\n"
            "- Why now (2-3 specific tailwinds)\n"
            "Be specific and grounded in the brief."
        )
        fallback_market = self._render_market_scan(idea, comps)
        market_md = await self._llm_generate(
            role_prompt=market_role_prompt,
            brief=brief,
            fallback=fallback_market,
        )
        market_path = artifact_dir / "market_scan.md"
        market_path.write_text(market_md, encoding="utf-8")
        await self.think(f"wrote {market_path.name}")

        # STEP 0: try LLM for business_model.md, fall back to deterministic template.
        model_role_prompt = (
            "You are a SaaS pricing strategist. Recommend a revenue model "
            "(be specific - subscription / usage-based / freemium / marketplace / "
            "transactional / etc.) that fits the brief. Then produce a markdown "
            "document with these sections (## headings):\n"
            "- Revenue model (named, with rationale)\n"
            "- 3 pricing tiers with concrete prices and what each includes "
            "(use a markdown table)\n"
            "- Key unit economics (CAC, ACV, gross margin, payback, NRR target) "
            "with concrete numbers\n"
            "- Risks to the model (3-5 specific risks)\n"
            "Match the brief's actual scope and audience."
        )
        fallback_model = self._render_business_model(idea, revenue_key, revenue)
        model_md = await self._llm_generate(
            role_prompt=model_role_prompt,
            brief=brief,
            fallback=fallback_model,
        )
        model_path = artifact_dir / "business_model.md"
        model_path.write_text(model_md, encoding="utf-8")
        await self.think(f"wrote {model_path.name}")

        # STEP 0: try LLM for pitch_outline.md, fall back to deterministic template.
        pitch_role_prompt = (
            "You are an investor-pitch coach. Produce a 10-slide pitch deck outline "
            "in markdown. Use one ## heading per slide (numbered 1-10) with 2-3 "
            "concise bullets each. Suggested slides: Title, Problem, Solution, "
            "Why now, Market size, Product / demo, Business model, Competition, "
            "Team, Ask. Make every slide specific to the brief - no generic filler."
        )
        fallback_pitch = self._render_pitch(idea, brief, revenue["label"], comps)
        pitch_md = await self._llm_generate(
            role_prompt=pitch_role_prompt,
            brief=brief,
            fallback=fallback_pitch,
        )
        pitch_path = artifact_dir / "pitch_outline.md"
        pitch_path.write_text(pitch_md, encoding="utf-8")
        await self.think(f"wrote {pitch_path.name}")

        files = [str(market_path), str(model_path), str(pitch_path)]

        if next_agent:
            await self.send_message(
                to=next_agent,
                kind="info",
                content=f"{self.name} done; artifacts in {artifact_dir}",
                payload={"files": files, "revenue_model": revenue_key},
            )

        await self.share_learning(
            f"BA chose revenue model='{revenue_key}' for idea snippet.",
            scope="global",
            revenue_model=revenue_key,
        )

        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "files": files,
                "revenue_model": revenue_key,
                "competitors": [c["name"] for c in comps],
                "summary": f"BA pack: {len(files)} artifacts, model={revenue_key}.",
            },
        )

    def _render_market_scan(self, idea: str, comps: List[Dict[str, str]]) -> str:
        out = [
            f"# Market scan - {idea[:60]}",
            "",
            "## TAM / SAM / SOM (placeholders)",
            "",
            "- **TAM (Total Addressable Market)**: $X B - sized via top-down (industry reports).",
            "- **SAM (Serviceable Addressable)**: $Y M - segments we can realistically serve.",
            "- **SOM (Serviceable Obtainable)**: $Z M - 3-year capture target.",
            "",
            "_Replace placeholders once we have at least one independent benchmark._",
            "",
            "## Comparable products",
            "",
        ]
        for c in comps:
            out.append(f"- **{c['name']}** - {c['desc']}")
        out.append("")
        out.append("## Feature comparison")
        out.append("")
        out.append("| Feature | Us | " + " | ".join(c["name"] for c in comps) + " |")
        out.append("| --- | --- | " + " | ".join("---" for _ in comps) + " |")
        rows = [
            ("Time-to-first-value", "<5 min"),
            ("Two-way sync", "Yes"),
            ("Self-host option", "Roadmap"),
            ("Usage-based pricing", "Yes"),
            ("Open API", "Yes"),
        ]
        for label, ours in rows:
            cells = " | ".join("Partial" for _ in comps)
            out.append(f"| {label} | {ours} | {cells} |")
        out.append("")
        out.append("## Why now")
        out.append("")
        out.append(
            "- Buyer expectation has shifted from \"works\" to \"works in 5 minutes\".\n"
            "- LLM tooling collapses the cost of building integrations.\n"
            "- Incumbents are weighed down by 10-year-old data models."
        )
        out.append("")
        return "\n".join(out)

    def _render_business_model(
        self,
        idea: str,
        revenue_key: str,
        revenue: Dict[str, str],
    ) -> str:
        tiers = self._tiers_for(revenue_key)
        out = [
            f"# Business model - {idea[:60]}",
            "",
            f"## Revenue model: **{revenue['label']}**",
            "",
            revenue["rationale"],
            "",
            "## Pricing tiers",
            "",
            "| Tier | Price | For | Limits |",
            "| --- | --- | --- | --- |",
        ]
        for t in tiers:
            out.append(f"| **{t['name']}** | {t['price']} | {t['for']} | {t['limits']} |")
        out.append("")
        out.append("## Back-of-envelope unit economics")
        out.append("")
        out.append("- **CAC** (blended): $150 (mostly content + community).")
        out.append("- **ACV** (blended): $1,200/year on the standard tier.")
        out.append("- **Gross margin**: 80% (cloud + LLM costs are the main variable).")
        out.append("- **Payback**: ~6 months on standard, ~3 months on team plans.")
        out.append("- **Net retention**: target 110% via expansion within accounts.")
        out.append("")
        out.append("## Risks to model")
        out.append("")
        out.append(
            "- LLM costs spike if usage is misforecasted; we cap with rate limits.\n"
            "- Competitor goes free; we differentiate on integrations + reliability.\n"
            "- Sales cycles lengthen; we pre-empt with self-serve top of funnel."
        )
        out.append("")
        return "\n".join(out)

    def _tiers_for(self, revenue_key: str) -> List[Dict[str, str]]:
        if revenue_key == "usage":
            return [
                {"name": "Free", "price": "$0", "for": "Tinkerers", "limits": "1k events/mo"},
                {"name": "Pro", "price": "$0.001/event", "for": "Indie + small teams", "limits": "fair-use rate limits"},
                {"name": "Scale", "price": "Custom", "for": "Mid-market", "limits": "SLA + SSO + audit log"},
            ]
        if revenue_key == "freemium":
            return [
                {"name": "Free", "price": "$0", "for": "Solo users", "limits": "1 workspace, 3 collaborators"},
                {"name": "Team", "price": "$15/user/mo", "for": "5-50 person teams", "limits": "Unlimited workspaces, audit log"},
                {"name": "Enterprise", "price": "Custom", "for": "Regulated industries", "limits": "SAML, SCIM, residency"},
            ]
        return [
            {"name": "Starter", "price": "$29/mo", "for": "Solo + small teams", "limits": "1 workspace, 5 seats"},
            {"name": "Standard", "price": "$99/mo", "for": "Growing teams", "limits": "Unlimited workspaces, integrations"},
            {"name": "Business", "price": "$499/mo", "for": "Mid-market", "limits": "SSO, audit log, priority support"},
        ]

    def _render_pitch(self, idea: str, brief: str, model_label: str, comps: List[Dict[str, str]]) -> str:
        comp_names = ", ".join(c["name"] for c in comps[:3])
        return "\n".join([
            f"# Pitch outline - {idea[:60]}",
            "",
            "## 1. Title",
            "",
            f"{idea[:60]} - the {model_label.lower()} layer for {brief.split('.')[0][:40] or 'modern teams'}.",
            "",
            "## 2. Problem",
            "",
            "Teams stitch 5+ tools together, lose context, and ship slower. Existing point",
            "solutions optimize for one tool; nobody owns the seam.",
            "",
            "## 3. Solution",
            "",
            f"_{brief}_  We unify the seam. One workspace, two-way sync, owners on both sides.",
            "",
            "## 4. Why now",
            "",
            "LLMs make integration cheap; buyer patience for setup is at an all-time low;",
            "infra costs have collapsed for the workloads we depend on.",
            "",
            "## 5. Market size",
            "",
            "TAM $X B / SAM $Y M / SOM $Z M (placeholders, see market_scan.md).",
            "",
            "## 6. Product demo",
            "",
            "60-second screencast: signup -> first integration -> first value moment.",
            "",
            "## 7. Business model",
            "",
            f"{model_label}. Pricing tiers in business_model.md.",
            "",
            "## 8. Competition",
            "",
            f"Closest comps: {comp_names}. We win on time-to-value and two-way sync.",
            "",
            "## 9. Team",
            "",
            "Founders bring [domain experience] + [shipping experience]. Advisors cover",
            "[gap A] and [gap B]. Hiring plan covers next 6 months.",
            "",
            "## 10. Ask",
            "",
            "Raising $A on $B post to fund 12 months runway: 3 engineers, 1 designer,",
            "1 GTM hire. Use of funds: 70% R&D, 20% GTM, 10% operations.",
            "",
        ])

    async def _llm_generate(self, *, role_prompt: str, brief: str, fallback: str) -> str:
        """Ask the configured LLM for a markdown artifact.

        Returns the LLM output, or the deterministic ``fallback`` if the LLM is
        unavailable / returned a stub.
        """
        try:
            client = self.get_llm() if hasattr(self, "get_llm") else None
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(
                    default_model=self.config.get("model"),
                    backend=self.config.get("backend"),
                    event_bus=self.event_bus,
                    caller_name=self.name,
                )
            prompt = (
                f"{role_prompt}\n\nBrief from user:\n{brief}\n\n"
                "Produce ONLY the markdown content for the artifact. "
                "No code fences, no preamble, no commentary."
            )
            out = await client.complete(prompt, max_tokens=2500, temperature=0.6)
            if out and "[deterministic-stub]" not in out and len(out.strip()) > 80:
                return out.strip()
        except Exception:
            pass
        return fallback
