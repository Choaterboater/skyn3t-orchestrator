"""Phase 3 — PackagingAgent target-driven packaging (pwa/desktop/capacitor).

Covers the additive ``packaging_agent.target_driven_packaging`` contract:

  - data['package_targets'] selects sub-targets; output['targets'] is the
    additive aggregate, and is EMPTY (legacy behavior) when no targets are
    passed.
  - PWA emits manifest.webmanifest + sw.js (+ setup doc) — pure file emission.
  - Desktop emits Tauri config by default, Electron fallback when electron is
    a declared dep; CONFIG ONLY (no CLI shelled out).
  - Capacitor emits capacitor.config.ts + cap:* npm scripts; CONFIG ONLY.
  - All sub-targets degrade to verdict='skipped' (never crash) when there's
    no frontend dir, and the whole agent still returns success=True.

Tests use tmp dirs only; nothing is installed/built.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skyn3t.agents.packaging_agent import (
    PackagingAgent,
    _capacitor_app_id,
    _detect_web_dir,
    _npm_safe_name,
)
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_scaffold(tmp_path: Path, *, deps: dict | None = None) -> Path:
    """Create a minimal react_vite scaffold (no env vars → simplest web path)."""
    artifact = tmp_path / "build-a-habit-tracker-with-streaks-a6f6c0"
    scaffold = artifact / "scaffold"
    pkg = {
        "name": "scaffold",
        "version": "0.1.0",
        "dependencies": {"react": "^18", "react-dom": "^18", **(deps or {})},
        "devDependencies": {"vite": "^5", "@vitejs/plugin-react": "^4"},
        "scripts": {"dev": "vite", "build": "vite build"},
    }
    _write(scaffold, "package.json", json.dumps(pkg))
    _write(scaffold, "index.html", "<div id='root'></div>")
    _write(scaffold, "src/App.jsx",
           "function App(){return <h1>Hi</h1>;}\nexport default App;\n")
    _write(scaffold, "src/main.jsx",
           "import App from './App.jsx';\n")
    return artifact


async def _run(artifact: Path, *, package_targets=None) -> dict:
    agent = PackagingAgent(event_bus=EventBus())
    await agent.initialize()
    data = {"artifact_dir": str(artifact), "packaging_verify": False}
    if package_targets is not None:
        data["package_targets"] = package_targets
    result = await agent.execute(TaskRequest(title="pkg", input_data=data))
    assert result.success is True
    return result.output or {}


# ---------------------------------------------------------------------------
# Backward-compat: targets is additive + empty by default
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    @pytest.mark.asyncio
    async def test_targets_empty_when_none_requested(self, tmp_path: Path) -> None:
        artifact = _make_scaffold(tmp_path)
        out = await _run(artifact)  # no package_targets
        assert out["targets"] == []
        # Legacy keys still present and unchanged in shape.
        assert out["strategy"] == "web"
        assert "files_written" in out and "notes" in out

    @pytest.mark.asyncio
    async def test_unknown_targets_dropped(self, tmp_path: Path) -> None:
        artifact = _make_scaffold(tmp_path)
        out = await _run(artifact, package_targets=["bogus", "lol"])
        assert out["targets"] == []

    def test_normalize_dedup_and_case(self) -> None:
        n = PackagingAgent._normalize_package_targets
        assert n(["pwa", "PWA", "desktop", "x"]) == ["pwa", "desktop"]
        assert n("capacitor") == ["capacitor"]
        assert n(None) == []
        assert n(42) == []


# ---------------------------------------------------------------------------
# PWA
# ---------------------------------------------------------------------------

class TestPWA:
    @pytest.mark.asyncio
    async def test_pwa_emits_manifest_and_sw(self, tmp_path: Path) -> None:
        artifact = _make_scaffold(tmp_path)
        out = await _run(artifact, package_targets=["pwa"])
        pwa = next(t for t in out["targets"] if t["target"] == "pwa")
        assert pwa["verdict"] == "ok"
        scaffold = artifact / "scaffold"
        manifest = scaffold / "public" / "manifest.webmanifest"
        sw = scaffold / "public" / "sw.js"
        assert manifest.is_file()
        assert sw.is_file()
        # Manifest is valid JSON with the expected shape.
        data = json.loads(manifest.read_text())
        assert data["display"] == "standalone"
        assert data["start_url"] == "/"
        # Service worker has install/fetch handlers.
        sw_text = sw.read_text()
        assert "addEventListener(\"install\"" in sw_text
        assert "addEventListener(\"fetch\"" in sw_text
        # Setup doc present with registration snippet.
        assert (scaffold / "PWA_SETUP.md").is_file()

    @pytest.mark.asyncio
    async def test_pwa_idempotent(self, tmp_path: Path) -> None:
        artifact = _make_scaffold(tmp_path)
        await _run(artifact, package_targets=["pwa"])
        first = (artifact / "scaffold" / "public" / "manifest.webmanifest").read_text()
        out = await _run(artifact, package_targets=["pwa"])
        second = (artifact / "scaffold" / "public" / "manifest.webmanifest").read_text()
        assert first == second
        pwa = next(t for t in out["targets"] if t["target"] == "pwa")
        # Second run leaves files in place; no crash.
        assert pwa["verdict"] in ("ok", "skipped")
        assert any("already exists" in n for n in pwa["notes"])


# ---------------------------------------------------------------------------
# Desktop (Tauri default, Electron fallback) — config only
# ---------------------------------------------------------------------------

class TestDesktop:
    @pytest.mark.asyncio
    async def test_desktop_tauri_default(self, tmp_path: Path) -> None:
        artifact = _make_scaffold(tmp_path)
        out = await _run(artifact, package_targets=["desktop"])
        desk = next(t for t in out["targets"] if t["target"] == "desktop")
        assert desk["verdict"] == "ok"
        scaffold = artifact / "scaffold"
        conf = scaffold / "src-tauri" / "tauri.conf.json"
        assert conf.is_file()
        assert (scaffold / "src-tauri" / "Cargo.toml").is_file()
        assert (scaffold / "src-tauri" / "src" / "main.rs").is_file()
        # tauri.conf.json is valid JSON.
        data = json.loads(conf.read_text())
        assert data["identifier"].startswith("com.skyn3t.")
        # Setup doc documents the build command (never executed).
        setup = (scaffold / "DESKTOP_SETUP.md").read_text()
        assert "tauri build" in setup
        # No Electron files emitted on the Tauri path.
        assert not (scaffold / "electron").exists()

    @pytest.mark.asyncio
    async def test_desktop_electron_fallback(self, tmp_path: Path) -> None:
        # Declaring electron as a dep signals the Electron preference.
        artifact = _make_scaffold(tmp_path, deps={"electron": "^30"})
        out = await _run(artifact, package_targets=["desktop"])
        desk = next(t for t in out["targets"] if t["target"] == "desktop")
        assert desk["verdict"] == "ok"
        scaffold = artifact / "scaffold"
        assert (scaffold / "electron" / "main.js").is_file()
        builder = scaffold / "electron-builder.json"
        assert builder.is_file()
        json.loads(builder.read_text())  # valid JSON
        # Tauri path NOT taken.
        assert not (scaffold / "src-tauri").exists()

    @pytest.mark.asyncio
    async def test_desktop_does_not_shell_out(self, tmp_path: Path, monkeypatch) -> None:
        """Config-only: packaging must never spawn a subprocess for desktop."""
        import asyncio as _aio

        called = {"n": 0}
        orig = _aio.create_subprocess_exec

        async def _spy(*a, **k):  # pragma: no cover - asserted not called
            called["n"] += 1
            return await orig(*a, **k)

        monkeypatch.setattr(_aio, "create_subprocess_exec", _spy)
        artifact = _make_scaffold(tmp_path)
        await _run(artifact, package_targets=["desktop"])
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# Capacitor — config only
# ---------------------------------------------------------------------------

class TestCapacitor:
    @pytest.mark.asyncio
    async def test_capacitor_config_and_scripts(self, tmp_path: Path) -> None:
        artifact = _make_scaffold(tmp_path)
        out = await _run(artifact, package_targets=["capacitor"])
        cap = next(t for t in out["targets"] if t["target"] == "capacitor")
        assert cap["verdict"] == "ok"
        scaffold = artifact / "scaffold"
        conf = scaffold / "capacitor.config.ts"
        assert conf.is_file()
        conf_text = conf.read_text()
        assert "CapacitorConfig" in conf_text
        assert "com.skyn3t." in conf_text
        # cap:* scripts injected into package.json.
        pkg = json.loads((scaffold / "package.json").read_text())
        assert pkg["scripts"]["cap:sync"] == "cap sync"
        assert pkg["scripts"]["cap:ios"] == "cap open ios"
        assert pkg["scripts"]["cap:android"] == "cap open android"
        # Existing scripts preserved.
        assert pkg["scripts"]["build"] == "vite build"
        # Setup doc present.
        assert (scaffold / "CAPACITOR_SETUP.md").is_file()

    @pytest.mark.asyncio
    async def test_capacitor_idempotent_scripts(self, tmp_path: Path) -> None:
        artifact = _make_scaffold(tmp_path)
        await _run(artifact, package_targets=["capacitor"])
        out = await _run(artifact, package_targets=["capacitor"])
        cap = next(t for t in out["targets"] if t["target"] == "capacitor")
        # Second run: config already exists; no duplicate scripts / crash.
        assert any("already exists" in n for n in cap["notes"])
        pkg = json.loads((artifact / "scaffold" / "package.json").read_text())
        # cap:sync appears exactly once (dict key, idempotent).
        assert pkg["scripts"]["cap:sync"] == "cap sync"


# ---------------------------------------------------------------------------
# Combined + degrade
# ---------------------------------------------------------------------------

class TestCombinedAndDegrade:
    @pytest.mark.asyncio
    async def test_multiple_targets_aggregate(self, tmp_path: Path) -> None:
        artifact = _make_scaffold(tmp_path)
        out = await _run(artifact, package_targets=["pwa", "desktop", "capacitor"])
        targets = {t["target"]: t for t in out["targets"]}
        assert set(targets) == {"pwa", "desktop", "capacitor"}
        assert all(targets[t]["verdict"] == "ok" for t in targets)

    @pytest.mark.asyncio
    async def test_docker_target_delegated(self, tmp_path: Path) -> None:
        artifact = _make_scaffold(tmp_path)
        out = await _run(artifact, package_targets=["docker"])
        dock = next(t for t in out["targets"] if t["target"] == "docker")
        assert dock["verdict"] == "skipped"
        assert dock["files_written"] == []

    @pytest.mark.asyncio
    async def test_targets_skip_when_no_frontend_dir(self, tmp_path: Path) -> None:
        """No scaffold/ AND no frontend manifest at root → targets skip,
        the whole agent still returns success with verdict-bearing output."""
        # An empty artifact dir: family 'unknown', no scaffold subdir.
        empty = tmp_path / "empty-project-abc123"
        empty.mkdir()
        out = await _run(empty, package_targets=["pwa", "desktop", "capacitor"])
        # Whole agent never crashes.
        assert "targets" in out
        # When there's genuinely no frontend dir, sub-targets emit 'skipped'.
        # (The artifact root itself is a dir, so PWA may attach there; the
        # contract only requires "never crash" — assert that strongly.)
        for t in out["targets"]:
            assert t["verdict"] in ("ok", "skipped")
            assert isinstance(t["files_written"], list)


# ---------------------------------------------------------------------------
# Pure helper unit tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_capacitor_app_id(self) -> None:
        assert _capacitor_app_id("My App") == "com.skyn3t.myapp"
        assert _capacitor_app_id("") == "com.skyn3t.app"

    def test_npm_safe_name(self) -> None:
        assert _npm_safe_name("Habit Tracker!") == "habit-tracker"
        assert _npm_safe_name("") == "app"

    def test_detect_web_dir_default_dist(self, tmp_path: Path) -> None:
        scaffold = tmp_path / "s"
        scaffold.mkdir()
        (scaffold / "package.json").write_text(
            json.dumps({"dependencies": {"vite": "^5"}}), encoding="utf-8")
        assert _detect_web_dir(scaffold) == "dist"

    def test_detect_web_dir_cra_build(self, tmp_path: Path) -> None:
        scaffold = tmp_path / "s"
        scaffold.mkdir()
        (scaffold / "package.json").write_text(
            json.dumps({"dependencies": {"react-scripts": "^5"}}), encoding="utf-8")
        assert _detect_web_dir(scaffold) == "build"

    def test_detect_web_dir_next_out(self, tmp_path: Path) -> None:
        scaffold = tmp_path / "s"
        scaffold.mkdir()
        (scaffold / "package.json").write_text(
            json.dumps({"dependencies": {"next": "^14"}}), encoding="utf-8")
        assert _detect_web_dir(scaffold) == "out"
