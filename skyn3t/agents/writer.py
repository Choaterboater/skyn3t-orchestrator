"""Writer Agent - templated copy/content generator.

LLM-free. Picks a template by ``kind`` and interpolates the brief plus a
tone-specific adjective bank. Future LLM variants can subclass and override
``_render``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


_TONE_ADJECTIVES: Dict[str, List[str]] = {
    "professional": ["reliable", "rigorous", "measured", "trusted", "disciplined"],
    "playful": ["delightful", "snappy", "cheeky", "vibrant", "spirited"],
    "technical": ["composable", "deterministic", "low-latency", "observable", "type-safe"],
}


_LENGTH_TARGET: Dict[str, int] = {"short": 1, "medium": 2, "long": 3}


class WriterAgent(BaseAgent):
    """Produces a single ``<kind>.md`` artifact from a brief."""

    def __init__(
        self,
        name: str = "writer",
        event_bus: EventBus = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="writer",
            provider="local",
            event_bus=event_bus,
            config=config,
        )
        self.add_capability(AgentCapability(
            name="writing",
            description="Write structured long-form content from a brief.",
            parameters={"brief": "str", "kind": "str", "tone": "str"},
        ))
        self.add_capability(AgentCapability(
            name="content",
            description="Produce blog posts, READMEs, and specs.",
            parameters={"brief": "str", "kind": "str"},
        ))
        self.add_capability(AgentCapability(
            name="copy",
            description="Produce short-form marketing copy (landing, email).",
            parameters={"brief": "str", "kind": "str"},
        ))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")

        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip() or "Untitled project"
        kind: str = (data.get("kind") or "readme").lower()
        tone: str = (data.get("tone") or "professional").lower()
        if tone not in _TONE_ADJECTIVES:
            tone = "professional"
        length: str = (data.get("length") or "medium").lower()
        artifact_dir = Path(data.get("artifact_dir") or ".")
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        renderer = {
            "readme": self._render_readme,
            "landing_copy": self._render_landing,
            "email": self._render_email,
            "blog": self._render_blog,
            "spec": self._render_spec,
        }.get(kind, self._render_readme)

        adjectives = _TONE_ADJECTIVES[tone]
        body = renderer(brief, adjectives, length)
        out_path = artifact_dir / f"{kind}.md"
        out_path.write_text(body, encoding="utf-8")
        await self.think(f"wrote {out_path.name}")

        if next_agent:
            await self.send_message(
                to=next_agent,
                kind="info",
                content=f"{self.name} done; artifacts in {artifact_dir}",
                payload={"files": [str(out_path)], "kind": kind, "tone": tone},
            )

        await self.share_learning(
            f"Writer template '{kind}' rendered with tone='{tone}'.",
            scope="global",
            kind=kind,
            tone=tone,
        )

        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "files": [str(out_path)],
                "kind": kind,
                "tone": tone,
                "summary": f"{kind}.md written ({len(body)} chars).",
            },
        )

    @staticmethod
    def _title(brief: str) -> str:
        first_line = brief.splitlines()[0] if brief else "Project"
        return first_line.strip()[:80] or "Project"

    def _render_readme(self, brief: str, adj: List[str], length: str) -> str:
        title = self._title(brief)
        a0, a1, a2 = adj[0], adj[1], adj[2 % len(adj)]
        out = [
            f"# {title}",
            "",
            "![build](https://img.shields.io/badge/build-passing-brightgreen) "
            "![license](https://img.shields.io/badge/license-MIT-blue)",
            "",
            "## Description",
            "",
            f"{title} is a {a0} and {a1} project. {brief}",
            "",
            "## Install",
            "",
            "```bash",
            "git clone https://example.com/your/repo.git",
            f"cd {title.lower().replace(' ', '-')}",
            "make install   # or: pip install -e . / npm install",
            "```",
            "",
            "## Usage",
            "",
            "```bash",
            f"{title.lower().replace(' ', '-')} --help",
            "```",
            "",
            f"The CLI is {a2}; see `docs/` for advanced flags.",
            "",
            "## Contributing",
            "",
            "Issues and pull requests are welcome. Please run the tests and the",
            "linter before opening a PR; commit messages should follow Conventional",
            "Commits.",
            "",
            "## License",
            "",
            "MIT - see `LICENSE`.",
            "",
        ]
        return "\n".join(out)

    def _render_landing(self, brief: str, adj: List[str], length: str) -> str:
        title = self._title(brief)
        a0, a1, a2 = adj[0], adj[1 % len(adj)], adj[2 % len(adj)]
        out = [
            f"# {title}: the {a0} way to ship.",
            "",
            f"## A {a1} workflow your team will actually use.",
            "",
            f"_{brief}_",
            "",
            "### Why teams pick us",
            "",
            f"- **{a0.title()} by default** - sensible presets, zero yak-shaving.",
            f"- **{a1.title()} surface** - clear primitives, no surprises.",
            f"- **{a2.title()} integrations** - works with the tools you already pay for.",
            "",
            "### Trusted by",
            "",
            "> _\"Your logo here.\"_ - early customer #1",
            ">",
            "> _\"Your logo here.\"_ - early customer #2",
            "",
            "### Get started in under 5 minutes",
            "",
            "[**Start free trial -->**](#)  [Talk to founders](#)",
            "",
        ]
        return "\n".join(out)

    def _render_email(self, brief: str, adj: List[str], length: str) -> str:
        title = self._title(brief)
        a0, a1 = adj[0], adj[1 % len(adj)]
        out = [
            f"Subject: {title} - a {a0} way to {self._verb_for(brief)}",
            "",
            f"Preview: A {a1} take on a problem you probably have.",
            "",
            "---",
            "",
            f"Hi {{first_name}},",
            "",
            f"I'm reaching out because {title} just shipped. {brief}",
            "",
            f"It's {a0}, {a1}, and built specifically for teams who don't have time",
            "to assemble the pieces themselves. We're letting in 50 design partners",
            "before the public launch.",
            "",
            "**Want a slot?** Reply with \"in\" and I'll set up a 20-min walkthrough.",
            "",
            "Thanks,",
            "{sender_name}",
            "",
            "[Unsubscribe](#) | [View in browser](#)",
            "",
        ]
        return "\n".join(out)

    def _render_blog(self, brief: str, adj: List[str], length: str) -> str:
        title = self._title(brief)
        a0, a1, a2 = adj[0], adj[1 % len(adj)], adj[2 % len(adj)]
        sections = max(3, _LENGTH_TARGET.get(length, 2) + 2)
        out = [
            f"# {title}: a {a0} approach",
            "",
            f"_{brief}_",
            "",
            f"There's a quiet shift happening in how teams ship. The {a1} approach",
            f"that worked at 10 people falls apart at 50. We've been thinking about",
            f"this for a while, and {title} is what came out the other side.",
            "",
        ]
        section_titles = [
            "The problem we kept hitting",
            "What we tried first",
            "What actually worked",
            "Trade-offs you should know about",
            "Where we're going next",
        ][:sections]
        for st in section_titles:
            out.append(f"## {st}")
            out.append("")
            out.append(
                f"This section explores {st.lower()}. The short version: a {a2} "
                "system makes the easy path the right path, and the right path the "
                "default. We'll write more about the implementation details in the "
                "docs."
            )
            out.append("")
        out.append("## Conclusion")
        out.append("")
        out.append(
            f"If any of this resonates, we'd love to hear how you're solving it. "
            f"{title} is in private beta - reply or open an issue."
        )
        out.append("")
        return "\n".join(out)

    def _render_spec(self, brief: str, adj: List[str], length: str) -> str:
        title = self._title(brief)
        a0 = adj[0]
        out = [
            f"# {title} - Spec",
            "",
            f"_{brief}_",
            "",
            "## Goals",
            "",
            f"- Deliver a {a0} v0.1 that solves the headline use case end-to-end.",
            "- Enable a single user to complete the primary task in <2 minutes.",
            "- Instrument the funnel so we can identify the first drop-off.",
            "",
            "## Non-goals",
            "",
            "- Multi-region deployment.",
            "- Full RBAC; v0.1 is single-tenant per workspace.",
            "- Mobile apps; web first, native later.",
            "",
            "## Requirements",
            "",
            "- **R1** - Sign up + sign in via email magic link.",
            "- **R2** - Create, view, edit, and delete the primary entity.",
            "- **R3** - Export a record as JSON.",
            "- **R4** - Audit log retains the last 90 days of mutations.",
            "",
            "## Milestones",
            "",
            "- **M1 (week 2)** - Schema + auth scaffolding merged.",
            "- **M2 (week 4)** - End-to-end happy path behind a feature flag.",
            "- **M3 (week 6)** - Closed beta with 10 design partners.",
            "- **M4 (week 8)** - Public beta + pricing live.",
            "",
        ]
        return "\n".join(out)

    @staticmethod
    def _verb_for(brief: str) -> str:
        b = brief.lower()
        if any(k in b for k in ("ship", "deploy")): return "ship faster"
        if any(k in b for k in ("learn", "study")): return "learn smarter"
        if any(k in b for k in ("track", "monitor")): return "stay on top of things"
        if any(k in b for k in ("sell", "market")): return "grow revenue"
        return "get more done"
