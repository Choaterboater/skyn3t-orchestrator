"""Phase 3 tests for BuildVerifierAgent — the visual + test gates.

These pin the two NEW non-blocking gates added in Phase 3:

  * output['test_run']           (build_verifier.test_run contract)
  * output['visual_verification'] (build_verifier.visual_verification contract)
  * the _visual_capture static helper (build_verifier._render_smoke_test_v2)

The cardinal rule under test: both gates must DEGRADE to verdict 'skipped'
with the top-level verdict UNCHANGED whenever their external tooling (npm,
playwright/chromium, network) is unavailable or the env flag is off — an
absent gate never penalizes a build. Only a real failure folds verdict→'no'.

Everything runs in tmp dirs; no real browser, server, or network is needed —
the heavy paths are monkeypatched.
"""

from __future__ import annotations

import json

import pytest

from skyn3t.agents.build_verifier import (
    BuildVerifierAgent,
    _env_flag_on,
    _visual_min_score,
)
from skyn3t.core.agent import TaskRequest

# ----------------------------------------------------------------------
# env-flag helpers
# ----------------------------------------------------------------------

def test_env_flag_on_defaults_true_and_respects_off(monkeypatch):
    monkeypatch.delenv("SKYN3T_VERIFY_VISUAL", raising=False)
    assert _env_flag_on("SKYN3T_VERIFY_VISUAL") is True
    for off in ("0", "false", "no", "off", "OFF", "False"):
        monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", off)
        assert _env_flag_on("SKYN3T_VERIFY_VISUAL") is False
    monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", "1")
    assert _env_flag_on("SKYN3T_VERIFY_VISUAL") is True


def test_visual_min_score_reads_env_and_falls_back(monkeypatch):
    monkeypatch.delenv("SKYN3T_VISUAL_MIN_SCORE", raising=False)
    assert isinstance(_visual_min_score(), int)
    monkeypatch.setenv("SKYN3T_VISUAL_MIN_SCORE", "72")
    assert _visual_min_score() == 72
    monkeypatch.setenv("SKYN3T_VISUAL_MIN_SCORE", "not-a-number")
    assert isinstance(_visual_min_score(), int)  # falls back, never raises


# ----------------------------------------------------------------------
# Additive output keys — must always be present, never removed.
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_output_always_carries_new_keys_even_for_python(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "app.py").write_text("x = 1\n")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert "visual_verification" in out
    assert "test_run" in out
    # Python stack never triggers the node-only gates → both None.
    assert out["visual_verification"] is None
    assert out["test_run"] is None
    # Existing keys untouched.
    assert out["verdict"] == "yes"
    assert out["stack"] == "python"


# ----------------------------------------------------------------------
# Test gate
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_test_gate_skipped_when_no_test_script(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")  # keep build offline
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0", "scripts": {"build": "echo ok"}})
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes"  # unchanged
    tr = out["test_run"]
    assert tr is not None
    assert tr["ran"] is False
    assert tr["verdict"] == "skipped"
    assert "no test script" in tr["summary"]


@pytest.mark.asyncio
async def test_test_gate_skipped_for_npm_default_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({
            "name": "demo", "version": "0.0.0",
            "scripts": {"test": 'echo "Error: no test specified" && exit 1'},
        })
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes"  # the placeholder must NOT fail the build
    assert out["test_run"]["verdict"] == "skipped"


@pytest.mark.asyncio
async def test_test_gate_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    monkeypatch.setenv("SKYN3T_VERIFY_TESTS", "0")
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0",
                    "scripts": {"test": "vitest run"},
                    "devDependencies": {"vitest": "^1.6.0"}})
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes"
    assert out["test_run"]["verdict"] == "skipped"
    assert "disabled" in out["test_run"]["summary"]


@pytest.mark.asyncio
async def test_test_gate_skipped_when_install_disabled_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    monkeypatch.setenv("SKYN3T_VERIFY_TESTS", "1")
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0",
                    "scripts": {"test": "vitest run"},
                    "devDependencies": {"vitest": "^1.6.0"}})
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes"
    assert out["test_run"]["verdict"] == "skipped"


@pytest.mark.asyncio
async def test_test_gate_failure_folds_verdict_to_no(tmp_path, monkeypatch):
    """When _run_test_gate reports a real failure, the top-level verdict folds
    to 'no' and a failure_hint is produced — without touching npm/node."""
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0",
                    "scripts": {"build": "echo ok", "test": "vitest run"},
                    "devDependencies": {"vitest": "^1.6.0"}})
    )

    async def _fake_test_gate(self, *_a, **_kw):
        return {
            "ran": True, "passed": False, "verdict": "no",
            "summary": "tests failed (exit 1)",
            "stdout_tail": "FAIL src/foo.test.js\n  expected 2 to be 3",
        }

    monkeypatch.setattr(BuildVerifierAgent, "_run_test_gate", _fake_test_gate)
    # Keep the visual gate out of the way.
    monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", "0")

    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no"
    assert out["test_run"]["verdict"] == "no"
    assert out["failure_hint"]
    assert "test gate" in (out["stderr"] or "")


# ----------------------------------------------------------------------
# Visual gate
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_visual_gate_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", "0")
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0",
                    "scripts": {"build": "echo ok"}})
    )
    (scaffold / "index.html").write_text("<!doctype html><html><body>hi</body></html>")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes"
    vv = out["visual_verification"]
    assert vv is not None and vv["ran"] is False and vv["verdict"] == "skipped"


@pytest.mark.asyncio
async def test_visual_gate_skipped_when_no_serve_dir(tmp_path, monkeypatch):
    """A node project with no dist/ and no index.html → nothing to serve →
    visual gate skips and verdict is unchanged (and no chromium is launched)."""
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", "1")

    # Guard: if _locate_serve_dir works, _playwright_renderable must NOT be hit.
    def _boom(*_a, **_kw):
        raise AssertionError("_playwright_renderable should not run when nothing to serve")

    monkeypatch.setattr(BuildVerifierAgent, "_playwright_renderable", staticmethod(_boom))

    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0",
                    "scripts": {"build": "echo ok"}})
    )
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes"
    assert out["visual_verification"]["verdict"] == "skipped"
    assert "serve" in out["visual_verification"]["reasons"][0]


@pytest.mark.asyncio
async def test_visual_gate_skipped_when_playwright_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", "1")
    monkeypatch.setattr(
        BuildVerifierAgent, "_playwright_renderable", staticmethod(lambda: False)
    )
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0",
                    "scripts": {"build": "echo ok"}})
    )
    dist = scaffold / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><html><body>hi</body></html>")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes"  # unchanged — never penalize a missing browser
    vv = out["visual_verification"]
    assert vv["verdict"] == "skipped"
    assert "playwright" in vv["reasons"][0]


@pytest.mark.asyncio
async def test_visual_gate_unstyled_page_folds_verdict_to_no(tmp_path, monkeypatch):
    """A page that renders effectively unstyled (default bg, ~1 color, no
    overflow) must fold the verdict to 'no' on the cheap structural heuristic —
    this is the unstyled-but-error-free regression the gate exists to catch."""
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", "1")
    monkeypatch.setenv("SKYN3T_VERIFY_TESTS", "0")
    monkeypatch.setattr(
        BuildVerifierAgent, "_playwright_renderable", staticmethod(lambda: True)
    )

    def _fake_blocking(self, serve_dir):
        return {
            "desktop_screenshot": str(serve_dir / "desktop.png"),
            "mobile_screenshot": str(serve_dir / "mobile.png"),
            "heuristics": {
                "non_default_bg": False, "distinct_colors": 1,
                "has_radius": False, "has_shadow": False,
                "horizontal_overflow": False,
            },
            "a11y_violations": 0, "reasons": [],
        }

    monkeypatch.setattr(BuildVerifierAgent, "_visual_gate_blocking", _fake_blocking)

    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0",
                    "scripts": {"build": "echo ok"}})
    )
    dist = scaffold / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><html><body>hi</body></html>")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no"
    vv = out["visual_verification"]
    assert vv["ran"] is True and vv["verdict"] == "no"
    assert any("unstyled" in r for r in vv["reasons"])
    assert "visual gate" in (out["failure_hint"] or "")


@pytest.mark.asyncio
async def test_visual_gate_styled_page_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", "1")
    monkeypatch.setenv("SKYN3T_VERIFY_TESTS", "0")
    monkeypatch.setattr(
        BuildVerifierAgent, "_playwright_renderable", staticmethod(lambda: True)
    )

    def _fake_blocking(self, serve_dir):
        return {
            "desktop_screenshot": str(serve_dir / "desktop.png"),
            "mobile_screenshot": str(serve_dir / "mobile.png"),
            "heuristics": {
                "non_default_bg": True, "distinct_colors": 9,
                "has_radius": True, "has_shadow": True,
                "horizontal_overflow": False,
            },
            "a11y_violations": 2, "reasons": [],
        }

    monkeypatch.setattr(BuildVerifierAgent, "_visual_gate_blocking", _fake_blocking)

    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0",
                    "scripts": {"build": "echo ok"}})
    )
    dist = scaffold / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><html><body>hi</body></html>")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "yes"
    vv = out["visual_verification"]
    assert vv["verdict"] == "yes"
    assert vv["heuristics"]["distinct_colors"] == 9
    assert vv["a11y_violations"] == 2


@pytest.mark.asyncio
async def test_visual_gate_horizontal_overflow_folds_to_no(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", "1")
    monkeypatch.setenv("SKYN3T_VERIFY_TESTS", "0")
    monkeypatch.setattr(
        BuildVerifierAgent, "_playwright_renderable", staticmethod(lambda: True)
    )

    def _fake_blocking(self, serve_dir):
        return {
            "desktop_screenshot": None, "mobile_screenshot": None,
            "heuristics": {
                "non_default_bg": True, "distinct_colors": 6,
                "has_radius": True, "has_shadow": True,
                "horizontal_overflow": True,
            },
            "a11y_violations": 0, "reasons": [],
        }

    monkeypatch.setattr(BuildVerifierAgent, "_visual_gate_blocking", _fake_blocking)

    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0",
                    "scripts": {"build": "echo ok"}})
    )
    dist = scaffold / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><html><body>hi</body></html>")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no"
    assert any("overflow" in r for r in out["visual_verification"]["reasons"])


@pytest.mark.asyncio
async def test_visual_gate_rubric_below_threshold_folds_to_no(tmp_path, monkeypatch):
    """Even a structurally-fine page fails when the optional vision rubric
    scores below SKYN3T_VISUAL_MIN_SCORE."""
    monkeypatch.setenv("SKYN3T_VERIFY_NPM_INSTALL", "0")
    monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", "1")
    monkeypatch.setenv("SKYN3T_VERIFY_TESTS", "0")
    monkeypatch.setenv("SKYN3T_VISUAL_MIN_SCORE", "50")
    monkeypatch.setattr(
        BuildVerifierAgent, "_playwright_renderable", staticmethod(lambda: True)
    )

    def _fake_blocking(self, serve_dir):
        return {
            "desktop_screenshot": str(serve_dir / "desktop.png"),
            "mobile_screenshot": None,
            "heuristics": {
                "non_default_bg": True, "distinct_colors": 7,
                "has_radius": True, "has_shadow": True,
                "horizontal_overflow": False,
            },
            "a11y_violations": 0, "reasons": [],
        }

    async def _fake_score(self, image_path, *, brief="", mood=""):
        return 20, ["looks like a generic AI dashboard"]

    monkeypatch.setattr(BuildVerifierAgent, "_visual_gate_blocking", _fake_blocking)
    monkeypatch.setattr(BuildVerifierAgent, "_maybe_score_screenshot", _fake_score)

    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0",
                    "scripts": {"build": "echo ok"}})
    )
    dist = scaffold / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><html><body>hi</body></html>")
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no"
    vv = out["visual_verification"]
    assert vv["score"] == 20
    assert any("rubric" in r for r in vv["reasons"])


@pytest.mark.asyncio
async def test_visual_gate_only_runs_after_passing_build(tmp_path, monkeypatch):
    """A failing build must NOT trigger the visual/test gates at all — there's
    nothing meaningful to serve, and they must never resurrect a 'no' verdict."""
    monkeypatch.setenv("SKYN3T_VERIFY_VISUAL", "1")
    monkeypatch.setenv("SKYN3T_VERIFY_TESTS", "1")

    def _boom_visual(self, *_a, **_kw):
        raise AssertionError("visual gate must not run on a failed build")

    def _boom_test(self, *_a, **_kw):
        raise AssertionError("test gate must not run on a failed build")

    monkeypatch.setattr(BuildVerifierAgent, "_run_visual_gate", _boom_visual)
    monkeypatch.setattr(BuildVerifierAgent, "_run_test_gate", _boom_test)

    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "app.py").write_text("def broken(:\n  pass\n")  # syntax error
    agent = BuildVerifierAgent()
    await agent.initialize()
    res = await agent.execute(TaskRequest(input_data={"scaffold_dir": str(scaffold)}))
    out = res.output
    assert out["verdict"] == "no"
    assert out["visual_verification"] is None
    assert out["test_run"] is None


# ----------------------------------------------------------------------
# _maybe_score_screenshot degrade contract
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_score_screenshot_degrades_when_scorer_absent(tmp_path, monkeypatch):
    """When design_vision has no score_screenshot (owned by another agent and
    possibly not present), the helper returns (None, []) — heuristics only."""
    agent = BuildVerifierAgent()
    from skyn3t.agents import design_vision

    monkeypatch.delattr(design_vision, "score_screenshot", raising=False)
    score, reasons = await agent._maybe_score_screenshot(
        tmp_path / "shot.png", brief="b", mood="m"
    )
    assert score is None
    assert reasons == []


@pytest.mark.asyncio
async def test_maybe_score_screenshot_uses_present_scorer(tmp_path, monkeypatch):
    agent = BuildVerifierAgent()
    from skyn3t.agents import design_vision

    async def _scorer(image_path, *, brief="", mood=""):
        return {"score": 81, "verdict": "pass", "reasons": ["clean type scale"],
                "generic_ai_tells": ["centered hero"]}

    monkeypatch.setattr(design_vision, "score_screenshot", _scorer, raising=False)
    score, reasons = await agent._maybe_score_screenshot(tmp_path / "s.png")
    assert score == 81
    assert any("type scale" in r for r in reasons)
    assert any("generic-AI tells" in r for r in reasons)


# ----------------------------------------------------------------------
# static helpers
# ----------------------------------------------------------------------

def test_locate_serve_dir_prefers_dist(tmp_path):
    scaffold = tmp_path / "scaffold"
    (scaffold / "dist").mkdir(parents=True)
    (scaffold / "dist" / "index.html").write_text("<html></html>")
    (scaffold / "index.html").write_text("<html></html>")
    assert BuildVerifierAgent._locate_serve_dir(scaffold) == scaffold / "dist"


def test_locate_serve_dir_falls_back_to_root(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "index.html").write_text("<html></html>")
    assert BuildVerifierAgent._locate_serve_dir(scaffold) == scaffold


def test_locate_serve_dir_none_when_nothing(tmp_path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text("{}")
    assert BuildVerifierAgent._locate_serve_dir(scaffold) is None


def test_free_port_returns_int():
    port = BuildVerifierAgent._free_port()
    assert port is None or (isinstance(port, int) and 0 < port < 65536)


def test_render_smoke_test_signature_preserved(tmp_path, monkeypatch):
    """The static-path _render_smoke_test must keep returning None when
    Playwright is unimportable (backward-compat for the existing static gate)."""
    import builtins

    real_import = builtins.__import__

    def _no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("playwright not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_playwright)
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "index.html").write_text("<html></html>")
    assert BuildVerifierAgent._render_smoke_test(scaffold, "index.html") is None
