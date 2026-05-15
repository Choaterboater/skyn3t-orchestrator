"""Marketer Agent - GTM scaffolding.

LLM-first. Reads the brief, asks an LLM to choose channels appropriate to the
audience and to draft positioning + launch checklist material specific to the
product. If the LLM is offline, falls back to a curated default playbook.
"""

from __future__ import annotations

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
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="marketer",
            provider="local",
            event_bus=event_bus or EventBus(),
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

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")

        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip() or "Untitled project"
        audience: str = (data.get("audience") or "early-stage product teams").strip()
        channels: List[str] = list(data.get("channels") or _DEFAULT_CHANNELS)
        if not channels:
            channels = list(_DEFAULT_CHANNELS)
        artifact_dir = self.resolve_artifact_dir(data.get("artifact_dir"))
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        positioning_md = await self._render_positioning(brief, audience)
        positioning_path = artifact_dir / "positioning.md"
        positioning_path.write_text(positioning_md, encoding="utf-8")
        await self.think(f"wrote {positioning_path.name}")

        channels_md = await self._render_channels(brief, audience, channels)
        channels_path = artifact_dir / "channel_plan.md"
        channels_path.write_text(channels_md, encoding="utf-8")
        await self.think(f"wrote {channels_path.name}")

        checklist_md = await self._render_checklist(brief)
        checklist_path = artifact_dir / "launch_checklist.md"
        checklist_path.write_text(checklist_md, encoding="utf-8")
        await self.think(f"wrote {checklist_path.name}")

        # ── Real, ready-to-ship assets ───────────────────────────────────
        # The strategy markdown above tells the user WHAT to post. These
        # files give them ACTUAL POSTS — copy-pasteable artifacts, not
        # descriptions of artifacts. Same pattern DesignerAgent uses for
        # tokens.css / logo.svg.
        product_name = self._extract_product_name(brief)
        tweets_path = artifact_dir / "tweets.md"
        tweets_path.write_text(
            self._render_tweets(brief, product_name, audience),
            encoding="utf-8",
        )
        await self.think(f"wrote {tweets_path.name}")

        email_path = artifact_dir / "launch_email.html"
        email_path.write_text(
            self._render_launch_email(brief, product_name, audience),
            encoding="utf-8",
        )
        await self.think(f"wrote {email_path.name}")

        hero_path = artifact_dir / "hero.html"
        hero_path.write_text(
            self._render_hero_html(brief, product_name, audience),
            encoding="utf-8",
        )
        await self.think(f"wrote {hero_path.name}")

        og_path = artifact_dir / "og_image.svg"
        og_path.write_text(
            self._render_og_image(product_name, audience),
            encoding="utf-8",
        )
        await self.think(f"wrote {og_path.name}")

        files = [
            str(positioning_path), str(channels_path), str(checklist_path),
            str(tweets_path), str(email_path), str(hero_path), str(og_path),
        ]

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

    # ------------------------------------------------------------------
    # LLM helper
    # ------------------------------------------------------------------
    async def _llm_generate(
        self,
        *,
        role_prompt: str,
        brief: str,
        fallback: str,
        max_tokens: int = 2500,
    ) -> str:
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
                "Produce ONLY the markdown (or JSON if asked) - no code fences, no preamble."
            )
            out = await client.complete(prompt, max_tokens=max_tokens, temperature=0.7)
            if out and "[deterministic-stub]" not in out and len(out.strip()) > 80:
                return out.strip()
        except Exception:
            pass
        return fallback

    # ------------------------------------------------------------------
    # Positioning
    # ------------------------------------------------------------------
    async def _render_positioning(self, brief: str, audience: str) -> str:
        fallback = self._render_positioning_fallback(brief, audience)
        role = (
            "You are a senior product marketer. Produce a markdown positioning "
            "doc specific to this product. Required sections (in order): "
            "JTBD framing, ICP description (a SPECIFIC persona, not a "
            "generic segment), Value proposition (one sentence), 3 "
            "differentiators (concrete and verifiable, not generic), 3 likely "
            "objections + responses (objections that THIS audience would "
            "actually raise). Avoid hype words. No filler.\n\n"
            f"Default audience guess if the brief doesn't specify one: {audience}"
        )
        return await self._llm_generate(
            role_prompt=role,
            brief=brief,
            fallback=fallback,
            max_tokens=4000,
        )

    def _render_positioning_fallback(self, brief: str, audience: str) -> str:
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

    # ------------------------------------------------------------------
    # Channel plan
    # ------------------------------------------------------------------
    async def _render_channels(
        self,
        brief: str,
        audience: str,
        channels: List[str],
    ) -> str:
        fallback = self._render_channels_fallback(brief, channels)
        role = (
            "You are a head of growth. Read the brief and choose the 4-6 most "
            "relevant channels for THIS audience. Do NOT default to "
            "twitter/linkedin/hn/producthunt unless those are genuinely where "
            "this audience lives. Think hard about who'd actually use this "
            "product and where they spend time online.\n\n"
            "Output a markdown channel plan with: a one-line statement of who "
            "the audience is, a markdown table with columns "
            "Channel | Cadence | Message angle | KPI, and a short Sequencing "
            "section explaining the order to activate channels in.\n\n"
            f"Audience hint (override if the brief implies a different one): {audience}"
        )
        return await self._llm_generate(
            role_prompt=role,
            brief=brief,
            fallback=fallback,
            max_tokens=3500,
        )

    def _render_channels_fallback(self, brief: str, channels: List[str]) -> str:
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

    # ------------------------------------------------------------------
    # Launch checklist
    # ------------------------------------------------------------------
    async def _render_checklist(self, brief: str) -> str:
        fallback = self._render_checklist_fallback(brief)
        role = (
            "You are a launch lead. Produce a markdown launch checklist with "
            "the sections T-14, T-7, T-1, T-0, T+7 (in that order). Each "
            "section must contain checklist items (- [ ] ...) that are "
            "SPECIFIC to this product - not generic. Tasks should reflect "
            "the actual surface area of the product and audience."
        )
        return await self._llm_generate(
            role_prompt=role,
            brief=brief,
            fallback=fallback,
            max_tokens=3500,
        )

    def _render_checklist_fallback(self, brief: str) -> str:
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

    # ─── Real-asset emitters ────────────────────────────────────────────
    # The .md files above describe the campaign. These produce actual
    # postable artifacts. Same fix pattern as DesignerAgent (tokens.css,
    # logo.svg) — close the gap between "told you what to do" and "did it."

    @staticmethod
    def _extract_product_name(brief: str) -> str:
        """Pull a product name from the brief, falling back to a generic."""
        import re
        text = brief or ""
        # Look for explicit "called X" / "named X" patterns first.
        m = re.search(r"\b(?:called|named)\s+['\"]?([A-Za-z0-9][A-Za-z0-9\-_.]+)['\"]?", text)
        if m:
            return m.group(1)
        # Quoted name.
        m = re.search(r"['\"]([A-Za-z0-9][A-Za-z0-9\s\-_.]{1,30})['\"]", text)
        if m:
            return m.group(1).strip()
        # Don't try the "first capitalized word" trick on a freeform brief —
        # it picks the first imperative verb (Build / Create / Launch) which
        # is never the product. Without an explicit name, return a generic
        # placeholder the user can find-and-replace.
        return "the product"

    @staticmethod
    def _render_tweets(brief: str, product: str, audience: str) -> str:
        """Five ready-to-post tweets across angles: launch, why-now, demo,
        social-proof teaser, ask-for-feedback. Counted to <280 chars."""
        brief_short = (brief or "").strip().split(".")[0][:140]
        tweets = [
            (
                "LAUNCH",
                f"Just shipped {product}.\n\n{brief_short}\n\nBuilt for {audience}.\n\nTry it: [link]",
            ),
            (
                "WHY NOW",
                f"After watching {audience} hack around the same problem for years, we built {product} so they don't have to.\n\nDetails ↓ [link]",
            ),
            (
                "SHORT DEMO",
                f"30 seconds of {product} in action ↓\n\n[gif]\n\nFull post: [link]",
            ),
            (
                "SOCIAL PROOF TEASER",
                f"Early users of {product} are seeing real wins.\n\nA few quotes ↓\n\n[link to landing]",
            ),
            (
                "ASK FOR FEEDBACK",
                f"{product} is live.\n\nIf you're {audience.split(' and ')[0]}, what's the #1 thing it should do that it doesn't yet?\n\nReplies open. Building in public.",
            ),
        ]
        out = ["# Ready-to-post tweets", "",
               f"_Product: **{product}** · audience: **{audience}**_", "",
               "Copy-paste, swap `[link]`/`[gif]` placeholders, post. "
               "Each tweet is under the 280 character limit.", ""]
        for label, body in tweets:
            chars = len(body)
            out.append(f"## {label} ({chars} chars)")
            out.append("")
            out.append("```")
            out.append(body)
            out.append("```")
            out.append("")
        return "\n".join(out)

    @staticmethod
    def _render_launch_email(brief: str, product: str, audience: str) -> str:
        """A single-column, plain-table HTML email — works in every client."""
        import html as _h
        product_e = _h.escape(product)
        audience_e = _h.escape(audience)
        brief_e = _h.escape((brief or "").strip())
        return (
            "<!doctype html>\n"
            "<html><head><meta charset=\"utf-8\">"
            f"<title>{product_e} — launching today</title></head>\n"
            "<body style=\"margin:0;padding:0;background:#f5f5f4;font-family:"
            "-apple-system,Segoe UI,Helvetica,sans-serif;color:#1a1a1a;\">\n"
            "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" "
            "cellspacing=\"0\" style=\"background:#f5f5f4;padding:32px 0;\">"
            "<tr><td align=\"center\">\n"
            "<table role=\"presentation\" width=\"560\" cellpadding=\"0\" "
            "cellspacing=\"0\" style=\"background:#ffffff;border-radius:8px;"
            "overflow:hidden;\">\n"
            "<tr><td style=\"padding:32px 32px 16px;\">\n"
            f"<h1 style=\"margin:0 0 12px;font-size:24px;line-height:1.2;\">{product_e} is live.</h1>\n"
            f"<p style=\"margin:0 0 16px;color:#555;font-size:15px;\">"
            f"Built for {audience_e}.</p>\n"
            "</td></tr>\n"
            "<tr><td style=\"padding:0 32px 24px;font-size:15px;line-height:1.55;\">\n"
            f"<p>{brief_e}</p>\n"
            "<p>We'd love your feedback — reply to this email or hit us on "
            "Twitter.</p>\n"
            "</td></tr>\n"
            "<tr><td align=\"center\" style=\"padding:0 32px 32px;\">\n"
            "<a href=\"[link]\" style=\"display:inline-block;background:#1a1a1a;"
            "color:#fff;text-decoration:none;padding:12px 24px;border-radius:6px;"
            f"font-weight:600;\">Try {product_e}</a>\n"
            "</td></tr>\n"
            "<tr><td style=\"padding:16px 32px;border-top:1px solid #eee;"
            "color:#888;font-size:12px;\">\n"
            "You're receiving this because you signed up at our waitlist. "
            "Unsubscribe: [unsub-link]\n"
            "</td></tr>\n"
            "</table>\n"
            "</td></tr></table>\n"
            "</body></html>\n"
        )

    @staticmethod
    def _render_hero_html(brief: str, product: str, audience: str) -> str:
        """A single-file landing-page hero — drop into any static host."""
        import html as _h
        product_e = _h.escape(product)
        audience_e = _h.escape(audience)
        brief_short = _h.escape((brief or "").strip().split(".")[0][:200])
        return (
            "<!doctype html>\n"
            "<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{product_e}</title>\n"
            "<style>\n"
            "  *{box-sizing:border-box;margin:0;padding:0}\n"
            "  body{font-family:-apple-system,Segoe UI,sans-serif;background:#0a0a0a;color:#f4f4f4;min-height:100vh;display:grid;place-items:center;padding:48px 24px}\n"
            "  .hero{max-width:720px;text-align:center}\n"
            "  h1{font-size:clamp(2rem,5vw,3.5rem);font-weight:800;letter-spacing:-0.02em;margin-bottom:16px;line-height:1.1}\n"
            "  .sub{font-size:1.125rem;color:#a3a3a3;margin-bottom:32px;max-width:560px;margin-left:auto;margin-right:auto}\n"
            "  .audience{font-size:.875rem;color:#737373;text-transform:uppercase;letter-spacing:.1em;margin-bottom:24px}\n"
            "  .cta{display:inline-flex;gap:12px;flex-wrap:wrap;justify-content:center}\n"
            "  .btn{display:inline-block;padding:14px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:1rem;transition:transform .15s ease}\n"
            "  .btn-primary{background:#f4f4f4;color:#0a0a0a}\n"
            "  .btn-ghost{border:1px solid #404040;color:#f4f4f4}\n"
            "  .btn:hover{transform:translateY(-1px)}\n"
            "</style></head>\n"
            "<body><div class=\"hero\">\n"
            f"<div class=\"audience\">For {audience_e}</div>\n"
            f"<h1>{product_e}</h1>\n"
            f"<p class=\"sub\">{brief_short}</p>\n"
            "<div class=\"cta\">\n"
            "  <a class=\"btn btn-primary\" href=\"[get-started-link]\">Get started</a>\n"
            "  <a class=\"btn btn-ghost\" href=\"[docs-link]\">Read the docs</a>\n"
            "</div>\n"
            "</div></body></html>\n"
        )

    @staticmethod
    def _render_og_image(product: str, audience: str) -> str:
        """An OG/social-share card at 1200x630 — the standard size for Twitter,
        LinkedIn, Slack, etc. SVG so it scales cleanly."""
        import html as _h
        product_e = _h.escape(product)
        audience_e = _h.escape(audience)[:60]
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630" '
            f'role="img" aria-label="{product_e} social card">\n'
            '  <rect width="1200" height="630" fill="#0a0a0a"/>\n'
            '  <rect x="60" y="60" width="1080" height="510" fill="none" '
            'stroke="#262626" stroke-width="2" rx="16"/>\n'
            f'  <text x="100" y="280" font-family="ui-sans-serif, system-ui" '
            f'font-size="84" font-weight="800" fill="#f4f4f4" '
            f'letter-spacing="-2">{product_e}</text>\n'
            f'  <text x="100" y="360" font-family="ui-sans-serif, system-ui" '
            f'font-size="28" fill="#a3a3a3">For {audience_e}</text>\n'
            '  <line x1="100" y1="490" x2="1100" y2="490" stroke="#404040" '
            'stroke-width="1"/>\n'
            f'  <text x="100" y="540" font-family="ui-sans-serif, system-ui" '
            f'font-size="22" fill="#737373">[your-domain].com</text>\n'
            '</svg>\n'
        )
