"""Tests for skyn3t.agents.targeted_fix.

Covers:
- _parse_build_errors: Vite/tsc/node SyntaxError shapes, dedup
- _strip_preamble: markdown fences, LLM chatter
- _validate_syntax: brace/paren/bracket balance, JSON, empty content
- _placeholder_for: per-extension stubs
- apply_targeted_fix: happy path, path-escape rejection, placeholder fallback
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.agents.targeted_fix import (
    FileIssue,
    FixResult,
    _parse_build_errors,
    _placeholder_for,
    _strip_preamble,
    _validate_syntax,
    apply_targeted_fix,
)

# ─── _parse_build_errors ───────────────────────────────────────────────


def test_parse_build_errors_vite_shape():
    stderr = "src/App.jsx:12:3: error: Unexpected token"
    issues = _parse_build_errors(stderr, "")
    assert len(issues) == 1
    assert issues[0].path == "src/App.jsx"
    assert issues[0].suggested_action == "regenerate"
    assert "Line 12" in issues[0].error_message


def test_parse_build_errors_tsc_shape():
    stderr = "src/App.tsx(15,5): error TS1005: '}' expected."
    issues = _parse_build_errors(stderr, "")
    assert len(issues) == 1
    assert issues[0].path == "src/App.tsx"
    assert "'}' expected" in issues[0].error_message


def test_parse_build_errors_node_syntax_error():
    stderr = "SyntaxError: /tmp/scaffold/src/index.js: Unexpected token (8:14)"
    issues = _parse_build_errors(stderr, "")
    assert len(issues) >= 1
    assert any("index.js" in i.path for i in issues)


def test_parse_build_errors_cannot_find_local_module_creates_placeholder():
    stderr = "Cannot find module './missing.jsx'"
    issues = _parse_build_errors(stderr, "")
    assert len(issues) == 1
    assert issues[0].path == "./missing.jsx"
    assert issues[0].suggested_action == "create_placeholder"


def test_parse_build_errors_cannot_find_npm_package_regenerates_package_json():
    stderr = "Cannot find module 'react-router-dom'"
    issues = _parse_build_errors(stderr, "")
    assert len(issues) == 1
    assert issues[0].path == "package.json"
    assert issues[0].suggested_action == "regenerate"
    assert "react-router-dom" in issues[0].error_message


def test_parse_build_errors_not_exported_by():
    stderr = '[commonjs--resolver] "useConfig" is not exported by "src/hooks/useConfig.js"'
    issues = _parse_build_errors(stderr, "")
    assert len(issues) >= 1
    assert any(i.path == "src/hooks/useConfig.js" for i in issues)


def test_parse_build_errors_node_named_export_mismatch_uses_scaffold_relative_path():
    stderr = (
        "file:///tmp/demo/scaffold/server/routes/config.js:2\n"
        'import { get } from "../config-store.js";\n'
        "         ^^^\n"
        "SyntaxError: The requested module '../config-store.js' does not provide "
        "an export named 'get'\n"
    )
    issues = _parse_build_errors(stderr, "")
    assert len(issues) >= 1
    assert any(i.path == "server/config-store.js" for i in issues)
    assert any(i.error_message == "Missing export: get" for i in issues)


def test_parse_build_errors_dedups_same_path_and_action():
    stderr = (
        "src/App.jsx:1:1: error: foo\n"
        "src/App.jsx:2:2: error: bar\n"
    )
    issues = _parse_build_errors(stderr, "")
    # Both rows hit the same path with the same suggested_action, so
    # the second one should be dropped.
    assert len(issues) == 1
    assert issues[0].path == "src/App.jsx"


def test_parse_build_errors_empty_input_returns_empty_list():
    assert _parse_build_errors("", "") == []


# ─── _strip_preamble ───────────────────────────────────────────────────


def test_strip_preamble_removes_leading_fence():
    raw = "```jsx\nexport default function App() { return null; }\n```"
    cleaned = _strip_preamble(raw, "App.jsx")
    assert not cleaned.startswith("```")
    assert not cleaned.endswith("```")
    assert "export default function App" in cleaned


def test_strip_preamble_handles_json_with_chatter_prefix():
    """JSON path: the helper finds the first '{' or '[' line and strips
    everything above it."""
    raw = (
        "Here's the package.json:\n"
        "Sure!\n"
        "{\n"
        '  "name": "demo"\n'
        "}"
    )
    cleaned = _strip_preamble(raw, "package.json")
    assert cleaned.startswith("{")


def test_strip_preamble_handles_pure_content_unchanged():
    raw = "export default function X() { return null; }"
    cleaned = _strip_preamble(raw, "X.jsx")
    assert cleaned == raw


# ─── _validate_syntax ──────────────────────────────────────────────────


def test_validate_syntax_empty_content_fails():
    assert _validate_syntax("", ".jsx", "App.jsx") == "Empty content"
    assert _validate_syntax("   \n  ", ".jsx", "App.jsx") == "Empty content"


def test_validate_syntax_balanced_jsx_passes():
    content = "export default function App() {\n  return <div>ok</div>;\n}\n"
    assert _validate_syntax(content, ".jsx", "App.jsx") == ""


def test_validate_syntax_unmatched_braces_fails():
    content = "export default function App() {\n  return <div>ok</div>;\n"
    err = _validate_syntax(content, ".jsx", "App.jsx")
    assert "Unmatched braces" in err


def test_validate_syntax_unmatched_parens_fails():
    content = "function x( {\n  return 1;\n}\n"
    err = _validate_syntax(content, ".js", "x.js")
    assert "Unmatched parentheses" in err


def test_validate_syntax_incomplete_export_dead_check():
    """The 'Incomplete export' check at targeted_fix.py:245 is dead
    because content is .strip()'d before .endswith('export '). The
    trailing space gets stripped, so the check never matches. Document
    current behavior rather than the comment's intent."""
    content = "import a from 'b';\nexport "
    err = _validate_syntax(content, ".js", "x.js")
    # The trailing-space check never fires — content is balanced
    # otherwise, so this is reported as valid.
    assert err == ""


def test_validate_syntax_json_invalid_fails():
    err = _validate_syntax("{ bad json", ".json", "x.json")
    assert "Invalid JSON" in err


def test_validate_syntax_json_valid_passes():
    assert _validate_syntax('{"a": 1}', ".json", "x.json") == ""


# ─── _placeholder_for ──────────────────────────────────────────────────


def test_placeholder_for_jsx_returns_react_stub():
    p = _placeholder_for("src/Foo.jsx")
    assert "export default function" in p
    assert "<div>" in p


def test_placeholder_for_hook_returns_hook_stub():
    """useX.js path should get a hook-shaped stub, not a generic one."""
    p = _placeholder_for("src/hooks/useFoo.js")
    assert "usePlaceholder" in p or "use" in p.lower()


def test_placeholder_for_router_returns_express_stub():
    p = _placeholder_for("server/routes/config.js")
    assert "Router" in p
    assert "export default router" in p


def test_placeholder_for_json_returns_empty_object():
    assert _placeholder_for("package.json").strip() == "{}"


def test_placeholder_for_css_returns_css_comment():
    p = _placeholder_for("styles/main.css")
    assert "/*" in p


def test_placeholder_for_unknown_extension_returns_safe_default():
    p = _placeholder_for("Makefile")
    # Just needs to be non-empty and not crash
    assert p.strip()


# ─── apply_targeted_fix integration ────────────────────────────────────


def test_apply_targeted_fix_create_placeholder_writes_file(tmp_path: Path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    issues = [
        FileIssue(
            path="src/missing.jsx",
            error_message="not found",
            suggested_action="create_placeholder",
        )
    ]
    result = asyncio.run(apply_targeted_fix(scaffold_dir=scaffold, issues=issues))
    assert isinstance(result, FixResult)
    assert "src/missing.jsx" in result.files_created
    assert (scaffold / "src" / "missing.jsx").exists()


def test_apply_targeted_fix_rejects_absolute_path(tmp_path: Path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    issues = [
        FileIssue(
            path="/etc/passwd",
            error_message="forbidden",
            suggested_action="create_placeholder",
        )
    ]
    result = asyncio.run(apply_targeted_fix(scaffold_dir=scaffold, issues=issues))
    assert result.files_created == []
    assert any("escapes scaffold" in e or "outside scaffold" in e for e in result.errors)


def test_apply_targeted_fix_rejects_parent_escape(tmp_path: Path):
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    issues = [
        FileIssue(
            path="../../../sensitive.txt",
            error_message="forbidden",
            suggested_action="create_placeholder",
        )
    ]
    result = asyncio.run(apply_targeted_fix(scaffold_dir=scaffold, issues=issues))
    assert result.files_created == []
    assert any("escapes scaffold" in e or "outside scaffold" in e for e in result.errors)


def test_apply_targeted_fix_regenerate_missing_file_creates_placeholder(
    tmp_path: Path,
):
    """When suggested_action='regenerate' but the file doesn't exist
    and there's no LLM client, fall back to placeholder creation."""
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    issues = [
        FileIssue(
            path="src/Missing.jsx",
            error_message="needs regeneration",
            suggested_action="regenerate",
        )
    ]
    result = asyncio.run(apply_targeted_fix(scaffold_dir=scaffold, issues=issues))
    assert "src/Missing.jsx" in result.files_created
    assert (scaffold / "src" / "Missing.jsx").exists()


def test_apply_targeted_fix_regenerate_with_no_llm_records_error(tmp_path: Path):
    """When file exists but no LLM is provided, we can't regenerate;
    the error should be recorded (not silently swallowed)."""
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "App.jsx").write_text("// existing")

    issues = [
        FileIssue(
            path="src/App.jsx",
            error_message="needs regeneration",
            suggested_action="regenerate",
        )
    ]
    result = asyncio.run(apply_targeted_fix(scaffold_dir=scaffold, issues=issues))
    assert "src/App.jsx" not in result.files_changed
    assert any("No LLM client" in e for e in result.errors)


def test_apply_targeted_fix_rejects_empty_path(tmp_path: Path):
    """Empty path is rejected with an error rather than silently writing
    nowhere."""
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    issues = [
        FileIssue(
            path="",
            error_message="oops",
            suggested_action="create_placeholder",
        )
    ]
    result = asyncio.run(apply_targeted_fix(scaffold_dir=scaffold, issues=issues))
    assert result.files_created == []
    assert any(result.errors)


def test_apply_targeted_fix_preserves_existing_file_for_build_invalid_output(tmp_path: Path):
    scaffold = tmp_path / "scaffold"
    (scaffold / "src").mkdir(parents=True)
    target = scaffold / "src" / "App.jsx"
    target.write_text("export default function App() { return <div>ok</div>; }\n")

    class FakeLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            return "export default const App = () => <div>broken</div>;"

    issues = [
        FileIssue(
            path="src/App.jsx",
            error_message="unexpected build failure",
            suggested_action="regenerate",
        )
    ]

    result = asyncio.run(
        apply_targeted_fix(scaffold_dir=scaffold, issues=issues, llm_client=FakeLLM())
    )

    assert "src/App.jsx" not in result.files_created
    assert "src/App.jsx" not in result.files_changed
    assert any("Build-invalid regenerated content for src/App.jsx" in e for e in result.errors)
    assert target.read_text(encoding="utf-8") == "export default function App() { return <div>ok</div>; }\n"


def test_missing_export_fix_is_grounded_in_real_exports(tmp_path: Path):
    """The dominant unrepaired failure ("X is not exported by Y"): the regen
    prompt must inject the file's REAL export surface + the exact missing symbol
    so the fix LLM adds it instead of re-hallucinating (which produced the live
    "build-invalid output" / preserved-existing no-fixes)."""
    from skyn3t.agents.targeted_fix import _extract_export_surface

    scaffold = tmp_path / "scaffold"
    (scaffold / "src" / "hooks").mkdir(parents=True)
    target = scaffold / "src" / "hooks" / "useConfig.js"
    target.write_text(
        "export const useConfig = () => ({});\n"
        "export default function ConfigProvider() { return null; }\n"
    )
    assert "useConfig" in _extract_export_surface(target.read_text())

    captured: dict = {}

    class CaptureLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            captured["prompt"] = prompt
            return (
                "export const useConfig = () => ({});\n"
                "export const fetchConfig = () => ({});\n"
            )

    issues = [
        FileIssue(
            path="src/hooks/useConfig.js",
            error_message="Missing export: fetchConfig",
            suggested_action="regenerate",
        )
    ]
    asyncio.run(
        apply_targeted_fix(
            scaffold_dir=scaffold,
            issues=issues,
            llm_client=CaptureLLM(),
            stack="react_vite",
        )
    )
    prompt = captured["prompt"]
    assert "GROUNDING" in prompt
    assert "fetchConfig" in prompt            # the exact missing symbol
    assert "useConfig" in prompt              # the real existing export surface


def test_apply_targeted_fix_timeout_preserves_existing_file(
    tmp_path: Path,
    monkeypatch,
):
    from skyn3t.agents import targeted_fix as targeted_fix_module

    scaffold = tmp_path / "scaffold"
    (scaffold / "src").mkdir(parents=True)
    target = scaffold / "src" / "App.jsx"
    target.write_text("export default function App() { return <div>ok</div>; }\n")

    class SlowLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            await asyncio.sleep(0.2)
            return "export default function App() { return <main>fixed</main>; }\n"

    monkeypatch.setattr(targeted_fix_module, "_regenerate_timeout_for", lambda _existing: 0.05)
    issues = [
        FileIssue(
            path="src/App.jsx",
            error_message="unexpected build failure",
            suggested_action="regenerate",
        )
    ]

    result = asyncio.run(
        apply_targeted_fix(scaffold_dir=scaffold, issues=issues, llm_client=SlowLLM())
    )

    assert "src/App.jsx" not in result.files_created
    assert "src/App.jsx" not in result.files_changed
    assert any("Timed out regenerating src/App.jsx" in e for e in result.errors)
    assert "preserved existing file instead" in result.errors[0]
    assert target.read_text(encoding="utf-8") == "export default function App() { return <div>ok</div>; }\n"


def test_apply_targeted_fix_preserves_existing_file_for_syntax_invalid_output(tmp_path: Path):
    scaffold = tmp_path / "scaffold"
    (scaffold / "src").mkdir(parents=True)
    target = scaffold / "src" / "App.jsx"
    original = "export default function App() { return <div>ok</div>; }\n"
    target.write_text(original)

    class FakeLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            return "export default function App() { return <div>broken</div>;\n"

    issues = [
        FileIssue(
            path="src/App.jsx",
            error_message="unexpected build failure",
            suggested_action="regenerate",
        )
    ]

    result = asyncio.run(
        apply_targeted_fix(scaffold_dir=scaffold, issues=issues, llm_client=FakeLLM())
    )

    assert "src/App.jsx" not in result.files_created
    assert "src/App.jsx" not in result.files_changed
    assert any("Invalid regenerated content for src/App.jsx" in e for e in result.errors)
    assert target.read_text(encoding="utf-8") == original
