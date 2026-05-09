"""Marketer Agent - GTM scaffolding.

LLM-free. Produces positioning, channel plan, and a launch checklist from a
brief. Channels default to: twitter, linkedin, hn, producthunt, content, ads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


_DEFAULT_CHANNELS = ["twitter", "linkedin", "hn", "producthunt", "content", "ads"]

_CHANNEL_PLAYBOOK: Dict[str, Dict[str, str]] = {
    "twitter": {
        "cadence": "5x/week",
        "angle": "Build-in-public threads + product GIFs.",
        "kpi": "Profile visits + reply rate",
    },
    "linkedin": {
        "cadence": "2x/week",
        "angle": "Founder POV essays; behind-the-scenes lessons.",
        "kpi": "Inbound connection requests + DM intros",
    },
    "hn": {
        "cadence": "1 launch + 1 Show HN follow-up",
        "angle": "Technical deep-dive; honest trade-offs.",
        "kpi": "Front-page time + sign-ups",
    },
    "producthunt": {
        "cadence": "Single launch day with 7-day pre-tease",
        "angle": "Hunter intro + maker comments + GIF demo.",
        "kpi": "Upvotes + day-of sign-ups",
    },
    "content": {
        "cadence": "1 long-form post / week",
        "angle": "Search-targeted tutorials + integration guides.",
        "kpi": "Organic sessions + email captures",
    },
    "ads": {
        "cadence": "Always-on small budget; weekly creative refresh",
        "angle": "Retargeting site visitors with feature-led creatives.",
        "kpi": "CPA per qualified signup",
    },
    "email": {
        "cadence": "Weekly newsletter + onboarding sequence",
        "angle": "Customer stories + product changelog.",
        "kpi": "Open rate + CTR + activation",
    },
    "youtube": {
        "cadence": "1 video / 2 weeks",
        "angle": "Screen recordings of real workflows.",
        "kpi": "Watch time + sub conversions",
    },
}


class MarketerAgent(BaseAgent):
    """GTM scaffolder: positioning + channel plan + launch checklist."""

    def __init__(
        self,
        name: str = "marketer",
        event_bus: EventBus = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="marketer",
            provider="local",
            event_bus=event_bus,
            config=config,
        )
        self.add_capability(AgentCapability(
            name="marketing",
            description="Produce positioning, channel plan, and launch checklist.",
            parameters={"brief": "str", "audience": "str", "channels": "list"},
        ))
        self.add_capability(AgentCapability(
            name="gtm",
            description="Draft go-to-market collateral.",
            parameters={"brief": "str"},
        ))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")

        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip() or "Untitled project"
        audience: str = (data.get("audience") or "early-stage product teams").strip()
        channels: List[str] = list(data.get("channels") or _DEFAULT_CHANNELS)
        if not channels:
            channels = list(_DEFAULT_CHANNELS)
        artifact_dir = Path(data.get("artifact_dir") or ".")
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        positioning_md = self._render_positioning(brief, audience)
        positioning_path = artifact_dir / "positioning.md"
        positioning_path.write_text(positioning_md, encoding="utf-8")
        await self.think(f"wrote {positioning_path.name}")

        channels_md = self._render_channels(brief, channels)
        channels_path = artifact_dir / "channel_plan.md"
        channels_path.write_text(channels_md, encoding="utf-8")
        await self.think(f"wrote {channels_path.name}")

        checklist_md = self._render_checklist(brief)
        checklist_path = artifact_dir / "launch_checklist.md"
        checklist_path.write_text(checklist_md, encoding="utf-8")
        await self.think(f"wrote {checklist_path.name}")

        files = [str(positioning_path), str(channels_path), str(checklist_path)]

        if next_agent:
            await self.send_message(
                to=next_agent,
                kind="info",
                content=f"{self.name} done; artifacts in {artifact_dir}",
                payload={"files": files, "channels": channels, "audience": audience},
            )

        await self.share_learning(
            f"Marketer plan covers {len(channels)} channels for audience='{audience}'.",
            scope="global",
            channels=channels,
        )

        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "files": files,
                "channels": channels,
                "audience": audience,
                "summary": "GTM artifacts written.",
            },
        )

    def _render_positioning(self, brief: str, audience: str) -> str:
        first = brief.split(".")[0][:80] or "the project"
        out = [
            f"# Positioning - {first}",
            "",
            f"_Brief:_ {brief}",
            "",
            "## Job To Be Done (JTBD)",
            "",
            f"When **{audience}** are trying to make progress, they hire a tool to "
            "**replace fragmented workflows with a single source of truth** so that "
            "they can ship the next milestone without dropped balls.",
            "",
            "## Ideal Customer Profile (ICP)",
            "",
            f"- **Who**: {audience}",
            "- **Team size**: 5-50",
            "- **Pain frequency**: weekly or higher",
            "- **Budget authority**: team lead / head of department",
            "- **Tech sophistication**: comfortable with SaaS + light scripting",
            "",
            "## Value proposition",
            "",
            f"_For_ {audience} _who_ keep losing context across tools, "
            f"**{first}** _is a_ workflow surface _that_ unifies the work, "
            f"_unlike_ stitching Notion + Slack + spreadsheets, "
            f"_our product_ keeps the truth in one place and writes back to where you work.",
            "",
            "## Three differentiators",
            "",
            "1. **Time-to-first-value < 5 minutes** - no setup workshop required.",
            "2. **Two-way sync** - we read AND write to the tools you already pay for.",
            "3. **Pricing that scales with usage, not seats** - so adoption isn't punished.",
            "",
            "## Objections + responses",
            "",
            "- _\"We already have Notion/Linear/etc.\"_ -> Great. We integrate; we don't replace.",
            "- _\"This feels like a feature, not a product.\"_ -> Today, yes. Roadmap is published; see /changelog.",
            "- _\"Can we self-host?\"_ -> Cloud first. Self-host is on the roadmap for the Enterprise tier.",
            "- _\"How is data handled?\"_ -> Encrypted in transit and at rest. SOC2 Type I in flight.",
            "- _\"What if you go away?\"_ -> Full JSON export at any time, no lock-in.",
            "",
        ]
        return "\n".join(out)

    def _render_channels(self, brief: str, channels: List[str]) -> str:
        first = brief.split(".")[0][:60] or "the project"
        out = [
            f"# Channel plan - {first}",
            "",
            "Default playbook per channel. Tune cadences after the first 30 days of data.",
            "",
            "| Channel | Cadence | Message angle | KPI |",
            "| --- | --- | --- | --- |",
        ]
        for ch in channels:
            entry = _CHANNEL_PLAYBOOK.get(
                ch.lower(),
                {
                    "cadence": "weekly",
                    "angle": "Founder POV + product clip.",
                    "kpi": "Engagement + sign-ups",
                },
            )
            out.append(
                f"| **{ch}** | {entry['cadence']} | {entry['angle']} | {entry['kpi']} |"
            )
        out.append("")
        out.append("## Sequencing")
        out.append("")
        out.append(
            "1. **Weeks 1-2**: warm the audience on the strongest channel only.\n"
            "2. **Weeks 3-4**: add the second channel; reuse the strongest piece in a new format.\n"
            "3. **Week 5+**: layer paid retargeting against the warmest pages."
        )
        out.append("")
        return "\n".join(out)

    def _render_checklist(self, brief: str) -> str:
        first = brief.split(".")[0][:60] or "the project"
        return "\n".join([
            f"# Launch checklist - {first}",
            "",
            "## T-14",
            "",
            "- [ ] Final positioning sign-off (founder + first customer).",
            "- [ ] Hero video (<60s) recorded.",
            "- [ ] Landing page copy + OG image final.",
            "- [ ] PH hunter confirmed + assets uploaded.",
            "- [ ] Newsletter teaser sent to early list.",
            "",
            "## T-7",
            "",
            "- [ ] Press list: 5 outlets + 5 newsletters with personal pitch.",
            "- [ ] HN draft Show HN copy reviewed by 2 ex-poster friends.",
            "- [ ] Bug bash: 5 friends do the onboarding cold; fix top 3 issues.",
            "- [ ] Status page + incident comms doc ready.",
            "",
            "## T-1",
            "",
            "- [ ] Smoke test signup + billing in production.",
            "- [ ] Schedule social posts for T-0.",
            "- [ ] Founder sleep early. Yes, really.",
            "",
            "## T-0",
            "",
            "- [ ] Publish PH at 12:01am PT.",
            "- [ ] Show HN post within 30 min of PH.",
            "- [ ] Reply to every comment within 1 hour for first 6 hours.",
            "- [ ] Capture top quotes for testimonials wall.",
            "",
            "## T+7",
            "",
            "- [ ] Post-mortem: what worked, what didn't, what to repeat.",
            "- [ ] Update landing with real metrics + quotes.",
            "- [ ] Email nurture sequence triggered for sign-ups.",
            "- [ ] Plan v0.2 milestone based on top 3 user requests.",
            "",
        ])
