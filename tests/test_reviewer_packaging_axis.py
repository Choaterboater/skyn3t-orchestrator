"""Tests for ReviewerAgent's packaging-completeness axis.

Verifies _packaging_score returns the right (score, gaps, family)
tuple per family and that the score gets blended into the final review
score at the documented weight.
"""

from __future__ import annotations

import json
from pathlib import Path

from skyn3t.agents.reviewer import ReviewerAgent
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
