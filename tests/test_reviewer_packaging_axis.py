"""Tests for ReviewerAgent's packaging-completeness axis.

Verifies _packaging_score returns the right (score, gaps, family)
tuple per family and that the score gets blended into the final review
score at the documented weight.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from skyn3t.agents.reviewer import ReviewerAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_reviewer() -> ReviewerAgent:
    return ReviewerAgent(event_bus=EventBus())


def _make_web_artifact(tmp_path: Path, *, with_settings: bool, with_useconfig: bool,
                      with_readme: bool = True, with_gitignore: bool = True) -> Path:
    """A react_vite scaffold with optional packaging artifacts."""
    artifact = tmp_path / "my-web-app"
    scaffold = artifact / "scaffold"
    _write(scaffold, "package.json", json.dumps({
        "name": "demo",
        "dependencies": {"react": "^18"},
        "devDependencies": {"vite": "^5"},
    }))
    _write(scaffold, "src/App.jsx", "export default function App(){return null;}")
    _write(scaffold, "src/api.js", "const url = import.meta.env.VITE_API_URL;\n")
    if with_settings:
        _write(scaffold, "src/Settings.jsx", "export default function Settings(){return null;}")
    if with_useconfig:
        _write(scaffold, "src/hooks/useConfig.js", "export function useConfig(){return {};}")
    if with_readme:
        # >200 chars so it doesn't count as a stub
        _write(artifact, "README.md",
               "# Demo app\n\n" + ("This is the documentation for the demo app. " * 10))
    if with_gitignore:
        _write(artifact, ".gitignore", "node_modules/\ndist/\n.env\n")
    return artifact


def _make_server_artifact(tmp_path: Path, *, with_dockerfile: bool, with_compose: bool,
                         with_env_example: bool, with_readme: bool = True,
                         with_gitignore: bool = True) -> Path:
    """A fastapi scaffold with optional packaging artifacts."""
    artifact = tmp_path / "my-server-app"
    artifact.mkdir(parents=True)
    _write(artifact, "requirements.txt", "fastapi==0.110\nuvicorn[standard]==0.27\n")
    _write(artifact, "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    if with_dockerfile:
        _write(artifact, "Dockerfile", "FROM python:3.12-slim\n")
    if with_compose:
        _write(artifact, "docker-compose.yml", "services:\n  app:\n    build: .\n")
    if with_env_example:
        _write(artifact, ".env.example", "JWT_SECRET=\n")
    if with_readme:
        _write(artifact, "README.md",
               "# Server app\n\n" + ("This is the documentation. " * 15))
    if with_gitignore:
        _write(artifact, ".gitignore", "__pycache__/\n.venv/\n.env\n")
    return artifact


def _make_fullstack_artifact(tmp_path: Path, *, frontend_in_compose: bool) -> Path:
    """A react+fastapi fullstack with optional combo wiring."""
    artifact = tmp_path / "my-fullstack-app"
    scaffold = artifact / "scaffold"
    _write(scaffold, "package.json", json.dumps({
        "name": "demo",
        "dependencies": {"react": "^18"},
        "devDependencies": {"vite": "^5"},
    }))
    _write(scaffold, "src/App.jsx", "export default function App(){return null;}")
    _write(scaffold, "src/Settings.jsx", "export default function Settings(){return null;}")
    _write(scaffold, "src/hooks/useConfig.js", "export function useConfig(){return {};}")
    _write(artifact, "requirements.txt", "fastapi==0.110\nuvicorn[standard]==0.27\n")
    _write(artifact, "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    _write(artifact, "Dockerfile", "FROM python:3.12-slim\n")
    if frontend_in_compose:
        _write(artifact, "docker-compose.yml",
               "services:\n  app:\n    build: .\n  frontend:\n    image: nginx\n")
    else:
        _write(artifact, "docker-compose.yml",
               "services:\n  app:\n    build: .\n")
    _write(artifact, "README.md", "# Fullstack\n\n" + ("Full docs. " * 25))
    _write(artifact, ".gitignore", "node_modules/\n.env\n")
    return artifact


def _make_zero_config_web_artifact(tmp_path: Path) -> Path:
    """A react_vite scaffold with no env vars, so Settings UI is not expected."""
    artifact = tmp_path / "zero-config-web"
    scaffold = artifact / "scaffold"
    _write(scaffold, "package.json", json.dumps({
        "name": "demo",
        "dependencies": {"react": "^18"},
        "devDependencies": {"vite": "^5"},
    }))
    _write(scaffold, "src/App.jsx", "export default function App(){return <main>Offline app</main>;}")
    _write(artifact, "README.md", "# Zero config app\n\n" + ("Runs without any setup. " * 15))
    _write(artifact, ".gitignore", "node_modules/\ndist/\n")
    return artifact


# ---------------------------------------------------------------------------
# Web tier scoring
# ---------------------------------------------------------------------------

class TestWebScoring:
    def test_perfect_web_score(self, tmp_path: Path) -> None:
        artifact = _make_web_artifact(tmp_path, with_settings=True, with_useconfig=True)
        score, gaps, family = _make_reviewer()._packaging_score(artifact)
        assert family == "web"
        assert score == 10
        assert gaps == []

    def test_missing_settings_jsx(self, tmp_path: Path) -> None:
        artifact = _make_web_artifact(tmp_path, with_settings=False, with_useconfig=True)
        score, gaps, family = _make_reviewer()._packaging_score(artifact)
        # 3 (readme) + 2 (gitignore) + 0 (no settings) + 2 (useconfig) = 7
        assert score == 7
        assert any("Settings.jsx" in g for g in gaps)

    def test_missing_useconfig_hook(self, tmp_path: Path) -> None:
        artifact = _make_web_artifact(tmp_path, with_settings=True, with_useconfig=False)
        score, gaps, _ = _make_reviewer()._packaging_score(artifact)
        # 3 + 2 + 3 + 0 = 8
        assert score == 8
        assert any("useConfig" in g for g in gaps)

    def test_stub_readme_not_credited(self, tmp_path: Path) -> None:
        artifact = _make_web_artifact(tmp_path, with_settings=True, with_useconfig=True, with_readme=False)
        # Create a tiny stub README
        (artifact / "README.md").write_text("# x")
        score, gaps, _ = _make_reviewer()._packaging_score(artifact)
        # No README credit (stub) but everything else: 0 + 2 + 3 + 2 = 7
        assert score == 7
        assert any("stub" in g.lower() for g in gaps)

    def test_no_gitignore(self, tmp_path: Path) -> None:
        artifact = _make_web_artifact(tmp_path, with_settings=True, with_useconfig=True, with_gitignore=False)
        score, gaps, _ = _make_reviewer()._packaging_score(artifact)
        assert score == 8
        assert any("gitignore" in g.lower() for g in gaps)

    def test_zero_config_web_not_docked_for_missing_settings_ui(self, tmp_path: Path) -> None:
        artifact = _make_zero_config_web_artifact(tmp_path)
        score, gaps, family = _make_reviewer()._packaging_score(artifact)
        assert family == "web"
        assert score == 10
        assert gaps == []

    def test_nested_scaffold_is_not_rescanned_for_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifact = _make_web_artifact(tmp_path, with_settings=True, with_useconfig=True)

        from skyn3t.agents import env_scanner

        original_scan = env_scanner.scan
        scan_calls: list[Path] = []

        def wrapped_scan(root: Path):
            scan_calls.append(root)
            return original_scan(root)

        monkeypatch.setattr(env_scanner, "scan", wrapped_scan)

        score, gaps, family = _make_reviewer()._packaging_score(artifact)

        assert family == "web"
        assert score == 10
        assert gaps == []
        assert scan_calls == [artifact]

    def test_artifact_walk_prunes_node_modules_before_descending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifact = _make_web_artifact(tmp_path, with_settings=True, with_useconfig=True)
        _write(artifact, "node_modules/pkg/index.js", "console.log('skip');")

        original_walk = os.walk
        visited: list[Path] = []

        def wrapped_walk(root: Path, *args, **kwargs):
            for dirpath, dirnames, filenames in original_walk(root, *args, **kwargs):
                visited.append(Path(dirpath))
                yield dirpath, dirnames, filenames

        monkeypatch.setattr("skyn3t.agents.reviewer.os.walk", wrapped_walk)

        files = _make_reviewer()._artifact_files(artifact)

        names = {path.relative_to(artifact).as_posix() for path in files}
        assert "node_modules/pkg/index.js" not in names
        assert all(path.name != "node_modules" for path in visited)


# ---------------------------------------------------------------------------
# Server tier scoring
# ---------------------------------------------------------------------------

class TestServerScoring:
    def test_perfect_server_score(self, tmp_path: Path) -> None:
        artifact = _make_server_artifact(
            tmp_path, with_dockerfile=True, with_compose=True, with_env_example=True,
        )
        score, gaps, family = _make_reviewer()._packaging_score(artifact)
        assert family == "server"
        assert score == 10
        assert gaps == []

    def test_missing_dockerfile(self, tmp_path: Path) -> None:
        artifact = _make_server_artifact(
            tmp_path, with_dockerfile=False, with_compose=True, with_env_example=True,
        )
        score, gaps, _ = _make_reviewer()._packaging_score(artifact)
        # 3 + 2 + 0 (no docker) + 2 (compose) + 1 (env) = 8
        assert score == 8
        assert any("Dockerfile" in g for g in gaps)

    def test_missing_compose(self, tmp_path: Path) -> None:
        artifact = _make_server_artifact(
            tmp_path, with_dockerfile=True, with_compose=False, with_env_example=True,
        )
        score, gaps, _ = _make_reviewer()._packaging_score(artifact)
        # 3 + 2 + 2 + 0 + 1 = 8
        assert score == 8
        assert any("docker-compose" in g for g in gaps)

    def test_missing_env_example(self, tmp_path: Path) -> None:
        artifact = _make_server_artifact(
            tmp_path, with_dockerfile=True, with_compose=True, with_env_example=False,
        )
        score, gaps, _ = _make_reviewer()._packaging_score(artifact)
        # 3 + 2 + 2 + 2 + 0 = 9
        assert score == 9
        assert any(".env.example" in g for g in gaps)

    def test_compose_yaml_counts_as_compose_manifest(self, tmp_path: Path) -> None:
        artifact = _make_server_artifact(
            tmp_path, with_dockerfile=True, with_compose=False, with_env_example=True,
        )
        _write(artifact, "compose.yaml", "services:\n  app:\n    build: .\n")
        score, gaps, _ = _make_reviewer()._packaging_score(artifact)
        assert score == 10
        assert not any("docker-compose" in g for g in gaps)


# ---------------------------------------------------------------------------
# Fullstack tier scoring
# ---------------------------------------------------------------------------

class TestFullstackScoring:
    def test_perfect_fullstack_score(self, tmp_path: Path) -> None:
        artifact = _make_fullstack_artifact(tmp_path, frontend_in_compose=True)
        score, gaps, family = _make_reviewer()._packaging_score(artifact)
        assert family == "fullstack"
        assert score == 10
        assert gaps == []

    def test_missing_compose_wiring(self, tmp_path: Path) -> None:
        artifact = _make_fullstack_artifact(tmp_path, frontend_in_compose=False)
        score, gaps, _ = _make_reviewer()._packaging_score(artifact)
        # 3 + 2 + 2 (web) + 2 (server) + 0 (no combo) = 9
        assert score == 9
        assert any("Frontend not wired" in g for g in gaps)

    def test_compose_yaml_counts_for_fullstack_wiring(self, tmp_path: Path) -> None:
        artifact = _make_fullstack_artifact(tmp_path, frontend_in_compose=True)
        compose = artifact / "docker-compose.yml"
        compose_yaml = artifact / "compose.yaml"
        compose_yaml.write_text(compose.read_text(encoding="utf-8"), encoding="utf-8")
        compose.unlink()
        score, gaps, family = _make_reviewer()._packaging_score(artifact)
        assert family == "fullstack"
        assert score == 10
        assert gaps == []


# ---------------------------------------------------------------------------
# Unknown family doesn't unfairly penalize
# ---------------------------------------------------------------------------

class TestUnknownFamily:
    def test_unknown_family_awards_default_5_points(self, tmp_path: Path) -> None:
        # Just an empty dir with README + gitignore — no manifests, no stack.
        artifact = tmp_path / "unknown-thing"
        artifact.mkdir()
        _write(artifact, "README.md", "# Unknown\n\n" + ("Docs. " * 50))
        _write(artifact, ".gitignore", "*.log\n")
        score, gaps, family = _make_reviewer()._packaging_score(artifact)
        assert family == "unknown"
        # 3 + 2 + 5 (default for unknown) = 10
        assert score == 10
        assert gaps == []


# ---------------------------------------------------------------------------
# Score blending into final review
# ---------------------------------------------------------------------------

class TestBlending:
    def test_packaging_axis_affects_final_score(self, tmp_path: Path) -> None:
        """End-to-end: a project with full packaging scores higher than one without."""
        # We can't easily run the full reviewer (needs LLM), but we can
        # confirm that _packaging_score returns different values for
        # different inputs and that the blend math is correct.
        rev = _make_reviewer()

        good = _make_web_artifact(tmp_path / "good", with_settings=True, with_useconfig=True)
        bad_dir = tmp_path / "bad"
        bad = _make_web_artifact(bad_dir, with_settings=False, with_useconfig=False,
                                  with_readme=False, with_gitignore=False)
        # Helper honors all flags; bad has no README, gitignore, Settings, or useConfig.

        good_score, _, _ = rev._packaging_score(good)
        bad_score, _, _ = rev._packaging_score(bad)
        assert good_score == 10
        # bad has nothing — 0/10
        assert bad_score == 0

    def test_score_breakdown_appears_in_review_md(self, tmp_path: Path) -> None:
        """Render path: review.md should include the packaging axis breakdown."""
        rev = _make_reviewer()
        rendered = rev._render_review_md(
            artifact_dir=tmp_path,
            files=[],
            completeness=[],
            consistency=[],
            risks=[],
            verdict="go",
            score=85,
            heuristic_score=80,
            llm_score=90,
            packaging_score=8,
            packaging_gaps=["No Settings.jsx (users will be stuck on a .env wall)"],
            packaging_family="web",
        )
        assert "packaging=8/10" in rendered
        assert "Packaging (web tier): 8/10" in rendered
        assert "Settings.jsx" in rendered

    def test_perfect_packaging_renders_check(self, tmp_path: Path) -> None:
        rev = _make_reviewer()
        rendered = rev._render_review_md(
            artifact_dir=tmp_path,
            files=[],
            completeness=[],
            consistency=[],
            risks=[],
            verdict="go",
            score=95,
            heuristic_score=95,
            llm_score=95,
            packaging_score=10,
            packaging_gaps=[],
            packaging_family="server",
        )
        assert "packaging=10/10" in rendered
        assert "Packaging (server tier): 10/10" in rendered
        assert "✓ All packaging artifacts present" in rendered


# ---------------------------------------------------------------------------
# 4-axis rubric parser (BR-028 follow-up: structured scoring)
# ---------------------------------------------------------------------------


class TestSubScoreParser:
    """The reviewer asks the LLM for four /25 sub-scores plus a /100 total.
    These tests exercise the regex used to extract them from free-form LLM
    output — different models follow the format with varying precision."""

    def _parse(self, text: str):
        from skyn3t.agents.reviewer import _SCORE_RE, _SUB_SCORE_RES
        subs = {}
        for axis, rx in _SUB_SCORE_RES.items():
            m = rx.search(text)
            if m:
                subs[axis] = int(m.group(1))
        total = None
        m = _SCORE_RE.search(text)
        if m:
            total = int(m.group(1))
        return subs, total

    def test_canonical_format(self):
        text = (
            "## 5. Score\n"
            "Completeness: 18/25\n"
            "Correctness:  20/25\n"
            "Consistency:  22/25\n"
            "Packaging:    15/25\n"
            "Score:        75/100\n"
        )
        subs, total = self._parse(text)
        assert subs == {
            "completeness": 18, "correctness": 20,
            "consistency": 22, "packaging": 15,
        }
        assert total == 75

    def test_loose_format_no_denominator(self):
        text = (
            "Completeness 18\n"
            "Correctness 20\n"
            "Consistency 22\n"
            "Packaging 15\n"
            "Score 75/100\n"
        )
        subs, total = self._parse(text)
        assert subs["completeness"] == 18
        assert subs["correctness"] == 20
        assert subs["consistency"] == 22
        assert subs["packaging"] == 15
        assert total == 75

    def test_em_dash_separator(self):
        text = "- Completeness — 14/25\n- Correctness — 16/25\n"
        subs, _ = self._parse(text)
        assert subs["completeness"] == 14
        assert subs["correctness"] == 16

    def test_partial_axes_returns_partial_dict(self):
        # Real-world: smaller models sometimes drop an axis.
        text = "Completeness: 20/25\nConsistency: 18/25\nScore: 70/100\n"
        subs, total = self._parse(text)
        assert subs == {"completeness": 20, "consistency": 18}
        assert total == 70


class TestTotalScoreParsing:
    """Production ``_parse_total_score`` must recover a total from loose
    formats. Cheaper model tiers frequently drop the exact ``Score:`` label;
    when the total can't be parsed the whole LLM review is discarded and the
    blend collapses to a deterministic heuristic-only ~53 (the react_vite
    score regression this guards against)."""

    def _p(self, text: str):
        return ReviewerAgent._parse_total_score(text)

    def test_canonical_score_line(self):
        assert self._p("Score: 82/100") == 82

    def test_rating_keyword(self):
        assert self._p("Overall Rating: 71") == 71

    def test_slash_100_without_keyword(self):
        assert self._p("Total: 76/100") == 76

    def test_out_of_100_phrasing(self):
        assert self._p("I'd put this at 68 out of 100 overall.") == 68

    def test_lone_sub_score_is_not_a_total(self):
        assert self._p("Completeness: 20/25") is None

    def test_no_numeric_score_returns_none(self):
        assert self._p("This review has no numeric score at all.") is None


class TestPartialAxisScoreRecovery:
    """When the LLM emits 2-3 axes and no parseable total, _llm_review should
    estimate the total from the mean axis rather than returning None (which
    forces the deterministic heuristic-only fallback)."""

    @pytest.mark.asyncio
    async def test_three_axes_no_total_recovers_score(self, monkeypatch):
        agent = _make_reviewer()

        async def fake_gen(**_kwargs):
            return (
                "## Review\n"
                "Completeness: 20/25\n"
                "Correctness: 18/25\n"
                "Consistency: 22/25\n"
                "Looks solid overall.\n"
            )

        monkeypatch.setattr(agent, "_llm_generate", fake_gen)
        body, score, subs = await agent._llm_review(
            brief="b", contents={"a.py": "x = 1"}
        )
        assert body is not None
        # mean axis = (20+18+22)/3 = 20 → *4 = 80
        assert score == 80
        # only three axes → not a full 4-axis sub_score dict
        assert subs is None

    @pytest.mark.asyncio
    async def test_review_with_slash_100_total_is_kept(self, monkeypatch):
        agent = _make_reviewer()

        async def fake_gen(**_kwargs):
            return "## Review\nStrong work.\nTotal: 77/100\n"

        monkeypatch.setattr(agent, "_llm_generate", fake_gen)
        _body, score, _subs = await agent._llm_review(
            brief="b", contents={"a.py": "x = 1"}
        )
        assert score == 77


class TestEdgeCases:
    def test_nonexistent_path_returns_zero(self) -> None:
        score, gaps, family = _make_reviewer()._packaging_score(Path("/does/not/exist"))
        assert score == 0
        assert gaps
        assert family == "unknown"

    def test_fullstack_zero_config_not_docked(self, tmp_path: Path) -> None:
        """Fullstack with no env vars should auto-score the web layer."""
        artifact = tmp_path / "fs-zero"
        scaffold = artifact / "scaffold"
        _write(scaffold, "package.json", json.dumps({
            "name": "demo", "dependencies": {"react": "^18"},
            "devDependencies": {"vite": "^5"},
        }))
        _write(scaffold, "src/App.jsx", "export default function App(){return null;}")
        # No Settings.jsx or useConfig.js — but also no env vars.
        _write(artifact, "Dockerfile", "FROM python:3.12-slim\n")
        _write(artifact, "docker-compose.yml", "services:\n  app:\n    build: .\n  frontend:\n    build: ./scaffold\n")
        _write(artifact, "requirements.txt", "fastapi\nuvicorn\n")
        _write(artifact, "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        _write(artifact, "README.md", "# FS\n" + ("Documentation for the fullstack demo application. " * 10))
        _write(artifact, ".gitignore", "node_modules/\n.env\n")
        # No .env.example — simulating zero-config.
        score, gaps, family = _make_reviewer()._packaging_score(artifact)
        assert family == "fullstack"
        # readme (3) + gitignore (2) + web zero-config (2) + server (2) + combo (1) = 10
        assert score == 10
        assert gaps == []


@pytest.mark.asyncio
async def test_objective_verification_failure_caps_score(tmp_path: Path) -> None:
    """H26: a failed build verifier must cap the reviewer's score even if the
    LLM/heuristic path would have scored highly."""
    artifact = _make_web_artifact(tmp_path, with_settings=True, with_useconfig=True)
    reviewer = _make_reviewer()

    # Force a high LLM score; the cap should still win.
    async def _fake_llm_review(*args, **kwargs):
        return "great", 95, {}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(reviewer, "_llm_review", _fake_llm_review)
    try:
        result = await reviewer.execute(
            TaskRequest(
                task_id="t1",
                input_data={
                    "brief": "Build a React todo app",
                    "artifact_dir": str(artifact),
                    "objective_verification": {
                        "build": {"verdict": "no", "failure_hint": "syntax error"},
                        "boot": {"verdict": "yes"},
                        "integration": {"verdict": "skipped"},
                    },
                },
            )
        )
    finally:
        monkeypatch.undo()

    assert result.success
    output = result.output or {}
    assert output.get("score", 100) <= 49
    assert output.get("verdict") == "no-go"
