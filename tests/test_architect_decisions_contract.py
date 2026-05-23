"""Tests for the architect → downstream decisions.json contract.

The architect commits to a small set of choices (frontend_port,
backend_port, framework, backend_language) in ``decisions.json`` so
downstream agents (PackagingAgent, ConsistencyReviewerAgent, etc.)
read one source of truth instead of each re-deriving from the
scaffold. This pins the contract surface:

1. ``derive_decisions`` maps the architect's stack bundle to the
   canonical decisions shape.
2. ``write_decisions`` / ``load_decisions`` round-trip cleanly.
3. PackagingAgent's ``_server_port`` honours the decisions override.
4. ConsistencyReviewerAgent flags a scaffold port that disagrees
   with the decisions contract.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents.consistency_reviewer import ConsistencyReviewerAgent
from skyn3t.agents.decisions import (
    derive_decisions,
    load_decisions,
    write_decisions,
)
from skyn3t.agents.packaging_agent import _server_port
from skyn3t.agents.stack_detector import StackDetection


class TestDeriveDecisions:
    """Each allowed bundle maps to a complete decisions dict."""

    def test_express_bundle(self):
        d = derive_decisions(
            {"frontend": "react-vite-tailwind", "backend": "express"}
        )
        assert d["frontend_bundle"] == "react-vite-tailwind"
        assert d["backend_bundle"] == "express"
        assert d["family"] == "fullstack"
        assert d["frontend_port"] == 5173
        assert d["backend_port"] == 3000
        assert d["framework"] == "express"
        assert d["backend_language"] == "node"

    def test_next_bundle(self):
        d = derive_decisions({"frontend": "next", "backend": "next"})
        assert d["family"] == "fullstack"
        assert d["frontend_port"] == 3000
        assert d["backend_port"] == 3000
        assert d["framework"] == "next"
        assert d["backend_language"] == "node"

    def test_hono_bundle(self):
        d = derive_decisions(
            {"frontend": "react-vite", "backend": "hono-node"}
        )
        assert d["family"] == "fullstack"
        assert d["frontend_port"] == 5173
        assert d["backend_port"] == 3000
        assert d["framework"] == "hono-node"
        assert d["backend_language"] == "node"

    def test_static_bundle_has_no_backend_port(self):
        d = derive_decisions({"frontend": "vanilla-vite", "backend": "none"})
        assert d["family"] == "web"
        assert d["frontend_port"] == 5173
        assert d["backend_port"] is None
        assert d["framework"] == "none"
        assert d["backend_language"] == "none"

    def test_react_vite_tailwind_web_bundle_has_no_backend_port(self):
        d = derive_decisions({"frontend": "react-vite-tailwind", "backend": "none"})
        assert d["family"] == "web"
        assert d["frontend_port"] == 5173
        assert d["backend_port"] is None
        assert d["framework"] == "none"
        assert d["backend_language"] == "none"

    def test_unknown_bundle_returns_none_ports(self):
        d = derive_decisions({"frontend": "svelte", "backend": "rocket"})
        assert d["family"] == "fullstack"
        assert d["frontend_port"] is None
        assert d["backend_port"] is None
        assert d["framework"] == "rocket"
        assert d["backend_language"] == "unknown"

    def test_empty_stack_does_not_crash(self):
        d = derive_decisions({})
        assert d["family"] == "unknown"
        assert d["framework"] == "none"
        assert d["backend_language"] == "none"


class TestWriteAndLoadDecisions:
    def test_round_trip(self, tmp_path: Path):
        stack = {"frontend": "react-vite-tailwind", "backend": "express"}
        path = write_decisions(tmp_path, stack)
        assert path.name == "decisions.json"
        loaded = load_decisions(tmp_path)
        assert loaded["frontend_bundle"] == "react-vite-tailwind"
        assert loaded["backend_bundle"] == "express"
        assert loaded["backend_port"] == 3000
        assert loaded["family"] == "fullstack"
        assert loaded["framework"] == "express"

    def test_load_missing_returns_none(self, tmp_path: Path):
        assert load_decisions(tmp_path) is None

    def test_load_malformed_returns_none(self, tmp_path: Path):
        (tmp_path / "decisions.json").write_text("not json", encoding="utf-8")
        assert load_decisions(tmp_path) is None

    def test_load_non_dict_returns_none(self, tmp_path: Path):
        (tmp_path / "decisions.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert load_decisions(tmp_path) is None


class TestPackagingAgentHonoursDecisions:
    """``_server_port`` must prefer ``port_override`` over the per-stack
    default — that override is how the architect's decisions reach
    every Dockerfile/compose/README without threading ``decisions``
    through every helper."""

    def test_override_wins_over_stack_default(self):
        d = StackDetection(stack="express", port_override=9090)
        assert _server_port(d) == 9090

    def test_stack_default_when_no_override(self):
        d = StackDetection(stack="express")
        assert _server_port(d) == 3000

    def test_fallback_when_unknown_stack(self):
        d = StackDetection(stack="totally-unknown")
        assert _server_port(d) == 8000

    def test_override_works_even_when_stack_unknown(self):
        d = StackDetection(stack="totally-unknown", port_override=7777)
        assert _server_port(d) == 7777


class TestConsistencyReviewerCheck8:
    """The decisions contract only buys us anything if the consistency
    reviewer treats a scaffold-vs-decisions disagreement as a blocker.
    Without this check, downstream agents could ignore the contract
    silently."""

    def test_env_port_disagrees_with_decisions(self, tmp_path: Path):
        scaffold = tmp_path / "scaffold"
        scaffold.mkdir()
        (scaffold / ".env.example").write_text("PORT=8000\n", encoding="utf-8")
        agent = ConsistencyReviewerAgent()
        decisions = {"backend_port": 3000, "framework": "express"}
        findings = agent._heuristic_check(scaffold, brief="", decisions=decisions)
        port_findings = [
            f for f in findings
            if f.file == ".env.example" and "PORT=8000" in f.message
        ]
        assert len(port_findings) == 1
        assert port_findings[0].severity == "blocker"
        assert port_findings[0].category == "contradiction"

    def test_compose_port_disagrees_with_decisions(self, tmp_path: Path):
        scaffold = tmp_path / "scaffold"
        scaffold.mkdir()
        (scaffold / "docker-compose.yml").write_text(
            'services:\n  app:\n    ports:\n      - "8000:8000"\n',
            encoding="utf-8",
        )
        agent = ConsistencyReviewerAgent()
        decisions = {"backend_port": 3000, "framework": "express"}
        findings = agent._heuristic_check(scaffold, brief="", decisions=decisions)
        compose_findings = [
            f for f in findings if f.file == "docker-compose.yml"
        ]
        assert any(
            f.severity == "blocker" and "3000" in f.message
            for f in compose_findings
        )

    def test_no_finding_when_ports_match(self, tmp_path: Path):
        scaffold = tmp_path / "scaffold"
        scaffold.mkdir()
        (scaffold / ".env.example").write_text("PORT=3000\n", encoding="utf-8")
        (scaffold / "docker-compose.yml").write_text(
            'services:\n  app:\n    ports:\n      - "3000:3000"\n',
            encoding="utf-8",
        )
        agent = ConsistencyReviewerAgent()
        decisions = {"backend_port": 3000, "framework": "express"}
        findings = agent._heuristic_check(scaffold, brief="", decisions=decisions)
        # No Check 8 findings should appear when everything aligns.
        assert not any(
            f.category == "contradiction" and "decisions.json" in f.message
            for f in findings
        )

    def test_no_check_8_findings_without_decisions(self, tmp_path: Path):
        """Backwards-compatible: builds with no decisions.json (older
        runs replayed, or future opt-out) skip Check 8 entirely."""
        scaffold = tmp_path / "scaffold"
        scaffold.mkdir()
        (scaffold / ".env.example").write_text("PORT=8000\n", encoding="utf-8")
        agent = ConsistencyReviewerAgent()
        findings = agent._heuristic_check(scaffold, brief="", decisions=None)
        assert not any(
            "decisions.json" in f.message for f in findings
        )

    def test_decided_port_field_missing_skips_check(self, tmp_path: Path):
        """A decisions dict without backend_port (e.g. a static-only
        build) must not crash Check 8."""
        scaffold = tmp_path / "scaffold"
        scaffold.mkdir()
        (scaffold / ".env.example").write_text("PORT=8000\n", encoding="utf-8")
        agent = ConsistencyReviewerAgent()
        decisions = {"backend_port": None, "framework": "none"}
        findings = agent._heuristic_check(scaffold, brief="", decisions=decisions)
        assert not any(
            "decisions.json" in f.message for f in findings
        )
