"""Tests for PackagingAgent's web strategy.

Verifies the agent generates Settings.jsx + useConfig + slim README
+ .gitignore for a react_vite scaffold, patches App.jsx safely when
it can, and emits a useful README note when it can't.

Verification (npm install + build) is stubbed out — there's a
separate integration test fixture for that, but unit-level we just
care that the agent makes the right files and skips/warns
appropriately when npm isn't available.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skyn3t.agents.env_scanner import EnvVarRef, ScanResult
from skyn3t.agents.packaging_agent import PackagingAgent, _humanize_var_name, _render_settings_jsx
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


def _make_react_vite_scaffold(tmp_path: Path, app_jsx: str = None) -> Path:
    """Create a minimal react_vite scaffold for PackagingAgent to consume."""
    artifact = tmp_path / "build-a-habit-tracker-with-streaks-a6f6c0"
    scaffold = artifact / "scaffold"
    _write(scaffold, "package.json", json.dumps({
        "name": "scaffold",
        "version": "0.1.0",
        "dependencies": {"react": "^18", "react-dom": "^18"},
        "devDependencies": {"vite": "^5", "@vitejs/plugin-react": "^4"},
        "scripts": {"dev": "vite", "build": "vite build"},
    }))
    _write(scaffold, "index.html", "<div id='root'></div>")
    if app_jsx is None:
        app_jsx = (
            "import React from 'react';\n"
            "function App() { return <h1>Hi</h1>; }\n"
            "export default App;\n"
        )
    _write(scaffold, "src/App.jsx", app_jsx)
    _write(scaffold, "src/main.jsx", (
        "import React from 'react';\n"
        "import ReactDOM from 'react-dom/client';\n"
        "import App from './App.jsx';\n"
        "ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"
    ))
    return artifact


async def _run_packaging(artifact: Path, *, verify_enabled: bool = False) -> dict:
    """Run PackagingAgent.execute against a scaffold dir. Returns the output dict."""
    agent = PackagingAgent(event_bus=EventBus())
    await agent.initialize()
    task = TaskRequest(
        title="package",
        input_data={
            "artifact_dir": str(artifact),
            "packaging_verify": verify_enabled,
        },
    )
    result = await agent.execute(task)
    assert result.success is True
    return result.output or {}


# ---------------------------------------------------------------------------
# Smoke: empty / nonexistent
# ---------------------------------------------------------------------------

class TestSmoke:
    @pytest.mark.asyncio
    async def test_no_artifact_dir_errors(self) -> None:
        agent = PackagingAgent(event_bus=EventBus())
        await agent.initialize()
        result = await agent.execute(TaskRequest(title="x", input_data={}))
        assert result.success is False
        assert "artifact_dir required" in (result.error or "")

    @pytest.mark.asyncio
    async def test_unknown_family_emits_placeholder(self, tmp_path: Path) -> None:
        # An empty artifact dir: no manifest, no source. Family will be "unknown".
        empty = tmp_path / "empty-project"
        empty.mkdir()
        output = await _run_packaging(empty)
        assert output["strategy"] == "unknown"
        assert output["files_written"] == []
        assert output["verifier_skipped"] is True


# ---------------------------------------------------------------------------
# Web strategy: file generation
# ---------------------------------------------------------------------------

class TestWebFileGeneration:
    @pytest.mark.asyncio
    async def test_generates_useconfig_settings_gitignore_readme(self, tmp_path: Path) -> None:
        artifact = _make_react_vite_scaffold(tmp_path)
        output = await _run_packaging(artifact)
        assert output["strategy"] == "web"
        files = output["files_written"]
        # Core artifacts exist on disk
        assert (artifact / "scaffold/src/hooks/useConfig.js").is_file()
        assert (artifact / "scaffold/src/Settings.jsx").is_file()
        assert (artifact / ".gitignore").is_file()
        assert (artifact / "README.md").is_file()
        # All four are reported in files_written
        assert "scaffold/src/hooks/useConfig.js" in files
        assert "scaffold/src/Settings.jsx" in files
        assert ".gitignore" in files
        assert "README.md" in files

    @pytest.mark.asyncio
    async def test_readme_mentions_settings_not_env(self, tmp_path: Path) -> None:
        artifact = _make_react_vite_scaffold(tmp_path)
        await _run_packaging(artifact)
        readme = (artifact / "README.md").read_text()
        assert "Settings" in readme
        # README references the in-app Settings page rather than a .env file.
        lower = readme.lower()
        assert "open the app" in lower or "click **settings**" in lower
        # No traditional "create a .env file" / "copy .env.example" wall.
        assert "create a .env" not in lower
        assert "copy .env.example" not in lower

    @pytest.mark.asyncio
    async def test_settings_jsx_has_empty_fields_when_no_env_vars(self, tmp_path: Path) -> None:
        artifact = _make_react_vite_scaffold(tmp_path)
        await _run_packaging(artifact)
        settings = (artifact / "scaffold/src/Settings.jsx").read_text()
        assert "const FIELDS = []" in settings
        assert "No configuration needed" in settings

    @pytest.mark.asyncio
    async def test_settings_jsx_has_fields_when_env_vars_present(self, tmp_path: Path) -> None:
        artifact = _make_react_vite_scaffold(tmp_path)
        _write(artifact / "scaffold", "src/api.js",
               "const url = import.meta.env.VITE_API_URL;\n"
               "const key = import.meta.env.VITE_API_KEY;\n")
        await _run_packaging(artifact)
        settings = (artifact / "scaffold/src/Settings.jsx").read_text()
        assert "VITE_API_URL" in settings
        assert "VITE_API_KEY" in settings
        # API_KEY is secret → password input type
        assert '"password"' in settings
        # API_URL is url → url input type
        assert '"url"' in settings


# ---------------------------------------------------------------------------
# App.jsx patching
# ---------------------------------------------------------------------------

class TestAppPatching:
    @pytest.mark.asyncio
    async def test_patches_simple_app_jsx(self, tmp_path: Path) -> None:
        artifact = _make_react_vite_scaffold(tmp_path)
        output = await _run_packaging(artifact)
        assert "scaffold/src/App.jsx" in output["files_patched"]
        patched = (artifact / "scaffold/src/App.jsx").read_text()
        assert "@skyn3t-packaging" in patched
        assert "import Settings" in patched
        assert "SkynPackagingWrapper" in patched

    @pytest.mark.asyncio
    async def test_skips_patching_when_react_router_present(self, tmp_path: Path) -> None:
        app_with_router = (
            "import React from 'react';\n"
            "import { BrowserRouter } from 'react-router-dom';\n"
            "function App() { return <BrowserRouter><h1>Hi</h1></BrowserRouter>; }\n"
            "export default App;\n"
        )
        artifact = _make_react_vite_scaffold(tmp_path, app_jsx=app_with_router)
        output = await _run_packaging(artifact)
        assert "scaffold/src/App.jsx" not in output["files_patched"]
        readme = (artifact / "README.md").read_text()
        assert "react-router" in readme  # Manual note in README
        # App.jsx untouched
        assert (artifact / "scaffold/src/App.jsx").read_text() == app_with_router

    @pytest.mark.asyncio
    async def test_skips_patching_when_app_too_large(self, tmp_path: Path) -> None:
        # 300-line App.jsx — over our 200-line safety threshold
        big_app = "import React from 'react';\n" + "// line\n" * 280 + (
            "function App() { return <h1>Hi</h1>; }\n"
            "export default App;\n"
        )
        artifact = _make_react_vite_scaffold(tmp_path, app_jsx=big_app)
        output = await _run_packaging(artifact)
        assert "scaffold/src/App.jsx" not in output["files_patched"]
        readme = (artifact / "README.md").read_text()
        assert "lines" in readme.lower() or "manual" in readme.lower() or "wire" in readme.lower()

    @pytest.mark.asyncio
    async def test_idempotent_does_not_double_patch(self, tmp_path: Path) -> None:
        artifact = _make_react_vite_scaffold(tmp_path)
        await _run_packaging(artifact)
        first = (artifact / "scaffold/src/App.jsx").read_text()
        # Run again — file already contains the @skyn3t-packaging marker
        await _run_packaging(artifact)
        second = (artifact / "scaffold/src/App.jsx").read_text()
        # File unchanged on second run
        assert first == second

    @pytest.mark.asyncio
    async def test_patches_function_form_export(self, tmp_path: Path) -> None:
        # `export default function Foo() {}` shape (vs `export default Foo;`)
        app = (
            "import React from 'react';\n"
            "export default function App() { return <h1>Hi</h1>; }\n"
        )
        artifact = _make_react_vite_scaffold(tmp_path, app_jsx=app)
        output = await _run_packaging(artifact)
        assert "scaffold/src/App.jsx" in output["files_patched"]
        patched = (artifact / "scaffold/src/App.jsx").read_text()
        assert "SkynPackagingWrapper" in patched
        assert patched.count("@skyn3t-packaging") == 1

    @pytest.mark.asyncio
    async def test_no_app_jsx_leaves_helpful_note(self, tmp_path: Path) -> None:
        artifact = _make_react_vite_scaffold(tmp_path)
        (artifact / "scaffold/src/App.jsx").unlink()
        output = await _run_packaging(artifact)
        assert "scaffold/src/App.jsx" not in output["files_patched"]
        notes = " ".join(output["notes"])
        assert "App.jsx" in notes


# ---------------------------------------------------------------------------
# Verification — stubbed
# ---------------------------------------------------------------------------

class TestVerification:
    @pytest.mark.asyncio
    async def test_verify_skipped_when_disabled(self, tmp_path: Path) -> None:
        artifact = _make_react_vite_scaffold(tmp_path)
        output = await _run_packaging(artifact, verify_enabled=False)
        assert output["verified"] is False
        assert output["verifier_skipped"] is True

    @pytest.mark.asyncio
    async def test_verify_skipped_when_npm_unavailable(self, tmp_path: Path) -> None:
        artifact = _make_react_vite_scaffold(tmp_path)
        with patch("shutil.which", return_value=None):
            output = await _run_packaging(artifact, verify_enabled=True)
        assert output["verified"] is False
        notes = " ".join(output["notes"])
        assert "npm" in notes


# ---------------------------------------------------------------------------
# Settings.jsx rendering — direct tests
# ---------------------------------------------------------------------------

class TestSettingsRendering:
    def test_required_vs_optional_field(self) -> None:
        scan = ScanResult()
        scan.vars["JWT_SECRET"] = EnvVarRef(
            name="JWT_SECRET", default=None, type_hint="secret", is_secret=True, idiom="node",
        )
        scan.vars["CACHE_TTL"] = EnvVarRef(
            name="CACHE_TTL", default="60", type_hint="int", is_secret=False, idiom="node",
        )
        rendered = _render_settings_jsx(scan, app_name="Demo")
        # Required field
        assert 'name: "JWT_SECRET"' in rendered
        assert "required: true" in rendered
        # Optional field with default
        assert 'name: "CACHE_TTL"' in rendered
        assert "required: false" in rendered
        # JWT_SECRET sorted before CACHE_TTL (secrets first)
        assert rendered.index("JWT_SECRET") < rendered.index("CACHE_TTL")


class TestHumanize:
    @pytest.mark.parametrize("raw,expected", [
        ("API_KEY",                "API key"),
        ("VITE_API_BASE_URL",      "API base URL"),
        ("REACT_APP_SENTRY_DSN",   "Sentry DSN"),
        ("NEXT_PUBLIC_BASE",       "Base"),
        ("DATABASE_URL",           "Database URL"),
        ("DEBUG",                  "Debug"),
        ("HTTP_TIMEOUT",           "HTTP timeout"),
    ])
    def test_humanize(self, raw: str, expected: str) -> None:
        assert _humanize_var_name(raw) == expected


# ---------------------------------------------------------------------------
# Feature flag — extra={"packaging_enabled": False} should bypass execution
# elsewhere (in the runner). The agent itself respects packaging_verify=False
# but always runs when called directly — tested above.
# ---------------------------------------------------------------------------
