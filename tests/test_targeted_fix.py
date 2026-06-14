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
    assert "src/App.jsx" in result.files_preserved
    assert result.ok is False
    assert result.fix_label == "noop"
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


def test_fix_hints_are_threaded_into_regen_prompt(tmp_path: Path):
    """PATTERN 2 (Hermes loop): experience-index recall passed as
    fix_hints must appear in the per-file regen prompt so the fix LLM
    prefers known-good strategies and avoids known anti-patterns."""
    scaffold = tmp_path / "scaffold"
    (scaffold / "src").mkdir(parents=True)
    target = scaffold / "src" / "App.jsx"
    target.write_text("export default function App() { return null; }\n")

    captured: dict = {}

    class CaptureLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            captured["prompt"] = prompt
            return "export default function App() { return <div/>; }\n"

    issues = [
        FileIssue(
            path="src/App.jsx",
            error_message="Line 1, col 1: Unexpected token",
            suggested_action="regenerate",
        )
    ]
    hints = (
        "  - WORKED: `regenerate:App.jsx` (3/3, 100%)\n"
        "  - did NOT work: `mixed:2` (2/2 failed)"
    )
    asyncio.run(
        apply_targeted_fix(
            scaffold_dir=scaffold,
            issues=issues,
            llm_client=CaptureLLM(),
            stack="react_vite",
            fix_hints=hints,
        )
    )
    prompt = captured["prompt"]
    assert "LEARNED FROM PRIOR ATTEMPTS" in prompt
    assert "regenerate:App.jsx" in prompt        # known-good winner shown
    assert "did NOT work" in prompt              # anti-pattern shown
    assert "do NOT repeat" in prompt             # explicit avoid instruction


def test_no_fix_hints_omits_learned_block(tmp_path: Path):
    """Empty fix_hints (cold index) must not inject a misleading header."""
    scaffold = tmp_path / "scaffold"
    (scaffold / "src").mkdir(parents=True)
    (scaffold / "src" / "App.jsx").write_text(
        "export default function App() { return null; }\n"
    )
    captured: dict = {}

    class CaptureLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            captured["prompt"] = prompt
            return "export default function App() { return <div/>; }\n"

    issues = [
        FileIssue(
            path="src/App.jsx",
            error_message="Line 1, col 1: Unexpected token",
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
    assert "LEARNED FROM PRIOR ATTEMPTS" not in captured["prompt"]


def test_preserve_only_round_is_noop_and_not_attributable(tmp_path: Path):
    """PATTERN 4: a preserve-only round changed nothing — it must report
    the file in files_preserved, stay ok=False, and carry a 'noop'
    fix_label so runner never attributes it as a 'worked' fix."""
    scaffold = tmp_path / "scaffold"
    (scaffold / "src").mkdir(parents=True)
    target = scaffold / "src" / "App.jsx"
    original = "export default function App() { return <div>ok</div>; }\n"
    target.write_text(original)

    class BrokenLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            # build-invalid: 'export default const' is not valid JS
            return "export default const App = () => <div>broken</div>;"

    issues = [
        FileIssue(path="src/App.jsx", error_message="boom", suggested_action="regenerate")
    ]
    result = asyncio.run(
        apply_targeted_fix(scaffold_dir=scaffold, issues=issues, llm_client=BrokenLLM())
    )
    assert result.files_changed == []
    assert result.files_created == []
    assert result.files_preserved == ["src/App.jsx"]
    assert result.ok is False
    assert result.fix_label == "noop"          # must NOT be 'regenerate:App.jsx'
    assert target.read_text(encoding="utf-8") == original


def test_successful_regenerate_keeps_real_label(tmp_path: Path):
    """Guard the happy path didn't regress to 'noop'."""
    scaffold = tmp_path / "scaffold"
    (scaffold / "src").mkdir(parents=True)
    (scaffold / "src" / "App.jsx").write_text(
        "export default function App(){return null;}\n"
    )

    class GoodLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            return "export default function App() { return null; }\n"

    issues = [FileIssue(path="src/App.jsx", error_message="x", suggested_action="regenerate")]
    result = asyncio.run(
        apply_targeted_fix(scaffold_dir=scaffold, issues=issues, llm_client=GoodLLM())
    )
    assert result.files_changed == ["src/App.jsx"]
    assert result.files_preserved == []
    assert result.fix_label == "regenerate:App.jsx"
    assert result.ok is True


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
    assert "src/App.jsx" in result.files_preserved
    assert result.ok is False
    assert result.fix_label == "noop"
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
    assert "src/App.jsx" in result.files_preserved
    assert result.ok is False
    assert result.fix_label == "noop"
    assert any("Invalid regenerated content for src/App.jsx" in e for e in result.errors)
    assert target.read_text(encoding="utf-8") == original


# ─── AREA B: deterministic-stub / rate-limit sentinel on regenerate ─────


_DETERMINISTIC_STUB = (
    "[deterministic-stub]\n"
    "context: Fix the following error in this react_vite file:\n"
    "thoughts: working without an LLM backend; returning a minimal scaffold.\n"
    "set ANTHROPIC_API_KEY, install `claude` CLI, or set OPENROUTER_API_KEY for real generation."
)


def test_regenerate_deterministic_stub_is_transient_not_build_invalid(tmp_path: Path):
    """AREA B: when LLMClient returns the [deterministic-stub] sentinel (its
    429/exhausted-key fallback), apply_targeted_fix must treat it as a
    TRANSIENT 'llm unavailable' failure — NOT mislabel it 'build-invalid
    output' — and (no manifest available) preserve the existing file."""
    scaffold = tmp_path / "scaffold"
    (scaffold / "src" / "components").mkdir(parents=True)
    target = scaffold / "src" / "components" / "DeviceCard.jsx"
    original = "export default function DeviceCard() { return <div>real</div>; }\n"
    target.write_text(original)

    class StubLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            return _DETERMINISTIC_STUB

    issues = [
        FileIssue(
            path="src/components/DeviceCard.jsx",
            error_message="regenerate me",
            suggested_action="regenerate",
        )
    ]
    result = asyncio.run(
        apply_targeted_fix(
            scaffold_dir=scaffold,
            issues=issues,
            llm_client=StubLLM(),
            stack="react_vite",
        )
    )
    assert "src/components/DeviceCard.jsx" not in result.files_changed
    assert "src/components/DeviceCard.jsx" in result.files_preserved
    # Honest label: transient unavailability, not "build-invalid".
    assert any("unavailable" in e.lower() for e in result.errors)
    assert not any("build-invalid" in e.lower() for e in result.errors)
    # Existing real file is left intact.
    assert target.read_text(encoding="utf-8") == original


def test_regenerate_429_raise_is_transient_and_recovers_via_manifest(tmp_path: Path):
    """AREA A+B regression: a client that RAISES (real 429 path before the
    LLMClient fallback) on a path that HAS a deterministic generator must end
    with a real, non-stub file via manifest_for — NOT a preserved stub — so
    the build does not hard-fail with UnresolvedScaffoldStubError."""
    scaffold = tmp_path / "scaffold"
    (scaffold / "src" / "components").mkdir(parents=True)
    target = scaffold / "src" / "components" / "ActivityFeed.jsx"
    # The file on disk is the backfill stub from a prior round.
    target.write_text(
        "// @skyn3t-backfill-stub: for missing import.\n"
        "export default function ActivityFeed() {\n  return null;\n}\n"
    )

    class Raises429:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            raise RuntimeError("Client error '429 Too Many Requests'")

    issues = [
        FileIssue(
            path="src/components/ActivityFeed.jsx",
            error_message="regenerate me",
            suggested_action="regenerate",
        )
    ]
    result = asyncio.run(
        apply_targeted_fix(
            scaffold_dir=scaffold,
            issues=issues,
            llm_client=Raises429(),
            stack="react_vite",
        )
    )
    # ActivityFeed.jsx has a real manifest_for generator, so the stub-on-disk
    # is upgraded BEFORE the regen even runs (stub treated as missing), then
    # the 429 never matters. Either way the final file is real, not a stub.
    final = target.read_text(encoding="utf-8")
    assert "@skyn3t-backfill-stub" not in final
    assert "Auto-generated placeholder" not in final
    assert "src/components/ActivityFeed.jsx" in (
        result.files_changed + result.files_created
    )


def test_regenerate_stub_on_disk_is_upgraded_via_manifest_before_llm(tmp_path: Path):
    """AREA A: a stub written on a prior round (backfill marker) must be
    treated as MISSING and re-upgraded via manifest_for, NOT preserved. Uses a
    path WITH a generator (ServiceDetail.jsx)."""
    scaffold = tmp_path / "scaffold"
    (scaffold / "src" / "components").mkdir(parents=True)
    target = scaffold / "src" / "components" / "ServiceDetail.jsx"
    target.write_text(
        "// @skyn3t-backfill-stub: for missing import.\n"
        "export default function ServiceDetail() {\n  return null;\n}\n"
    )

    class StubLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            return _DETERMINISTIC_STUB

    issues = [
        FileIssue(
            path="src/components/ServiceDetail.jsx",
            error_message="regenerate me",
            suggested_action="regenerate",
        )
    ]
    result = asyncio.run(
        apply_targeted_fix(
            scaffold_dir=scaffold,
            issues=issues,
            llm_client=StubLLM(),
            stack="react_vite",
        )
    )
    final = target.read_text(encoding="utf-8")
    assert "@skyn3t-backfill-stub" not in final
    assert "src/components/ServiceDetail.jsx" in result.files_created + result.files_changed


def test_regenerate_build_invalid_recovers_via_manifest_when_available(tmp_path: Path):
    """AREA A: a build-invalid regen on a real (non-stub) file that HAS a
    manifest generator should be rescued by writing the deterministic template
    instead of preserving — turning a no-fix into a real fix."""
    scaffold = tmp_path / "scaffold"
    (scaffold / "src" / "components").mkdir(parents=True)
    target = scaffold / "src" / "components" / "ActivityFeed.jsx"
    # Real (non-stub) file the build flagged as broken. The regen attempt then
    # also fails (prose), so recovery substitutes the known-good template.
    target.write_text(
        "export default function ActivityFeed() {\n"
        "  return <ul>BROKEN_ORIGINAL</ul>;\n"
        "}\n"
    )

    class BuildInvalidLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            # build-invalid: prose with no JSX signals would fail _syntax_ok,
            # but it must be non-empty and non-sentinel to reach that gate.
            return "this is not code at all just an explanation paragraph."

    issues = [
        FileIssue(
            path="src/components/ActivityFeed.jsx",
            error_message="regenerate me",
            suggested_action="regenerate",
        )
    ]
    result = asyncio.run(
        apply_targeted_fix(
            scaffold_dir=scaffold,
            issues=issues,
            llm_client=BuildInvalidLLM(),
            stack="react_vite",
        )
    )
    final = target.read_text(encoding="utf-8")
    # Recovered via manifest — the real ActivityFeed template (not the prose,
    # not the broken original that the build flagged).
    assert "this is not code" not in final
    assert "BROKEN_ORIGINAL" not in final
    assert "src/components/ActivityFeed.jsx" in result.files_changed


def test_regenerate_build_invalid_preserves_when_no_manifest(tmp_path: Path):
    """AREA A honest limitation: for a path with NO generator (DeviceCard.jsx),
    a build-invalid regen still PRESERVES — manifest_for returns None so we
    cannot do better than today. This documents the partial rescue."""
    scaffold = tmp_path / "scaffold"
    (scaffold / "src" / "components").mkdir(parents=True)
    target = scaffold / "src" / "components" / "DeviceCard.jsx"
    original = "export default function DeviceCard() { return <div>real</div>; }\n"
    target.write_text(original)

    class BuildInvalidLLM:
        async def complete(self, prompt: str, max_tokens: int, temperature: float) -> str:  # noqa: ARG002
            return "this is not code at all just an explanation paragraph."

    issues = [
        FileIssue(
            path="src/components/DeviceCard.jsx",
            error_message="regenerate me",
            suggested_action="regenerate",
        )
    ]
    result = asyncio.run(
        apply_targeted_fix(
            scaffold_dir=scaffold,
            issues=issues,
            llm_client=BuildInvalidLLM(),
            stack="react_vite",
        )
    )
    assert "src/components/DeviceCard.jsx" in result.files_preserved
    assert "src/components/DeviceCard.jsx" not in result.files_changed
    assert target.read_text(encoding="utf-8") == original


def test_content_is_stub_detects_all_markers():
    from skyn3t.agents.targeted_fix import _content_is_stub

    assert _content_is_stub(
        "// @skyn3t-backfill-stub: for missing import.\nexport default function x(){return null;}\n"
    )
    assert _content_is_stub(
        "// Auto-generated placeholder\nexport default function Placeholder(){return <div>Placeholder</div>;}\n"
    )
    assert _content_is_stub("// TODO[skyn3t]: code generation failed for x\n")
    # Real code is not a stub.
    assert not _content_is_stub(
        "export default function DeviceCard({ device }) { return <article>{device.name}</article>; }\n"
    )
    # A large file that merely mentions the word is not a stub (ceiling).
    assert not _content_is_stub("// Auto-generated placeholder mention\n" + "x;\n" * 300)


# ─── PHASE 2: transient-error propagation ──────────────────────────────


def test_apply_targeted_fix_reraises_transient_when_all_fail(tmp_path: Path):
    """A round where EVERY regen failed transiently (429/5xx/timeout) must
    re-raise TransientLLMError so the caller can bounded-retry — it must NOT
    quietly return a preserve-only "noop" FixResult."""
    from skyn3t.adapters.llm_client import TransientLLMError

    scaffold = tmp_path / "scaffold"
    (scaffold / "src").mkdir(parents=True)
    target = scaffold / "src" / "App.jsx"
    target.write_text("export default function App() { return <div>ok</div>; }\n")

    class ThrottledLLM:
        async def complete(self, prompt, max_tokens, temperature):  # noqa: ARG002
            raise TransientLLMError("openrouter 429 after 8 attempts")

    issues = [
        FileIssue(
            path="src/App.jsx",
            error_message="unexpected build failure",
            suggested_action="regenerate",
        )
    ]

    import pytest

    with pytest.raises(TransientLLMError):
        asyncio.run(
            apply_targeted_fix(
                scaffold_dir=scaffold, issues=issues, llm_client=ThrottledLLM()
            )
        )
    # The existing file is preserved on disk (never overwritten with a stub).
    assert target.read_text(encoding="utf-8").startswith("export default function App")


def test_apply_targeted_fix_keeps_progress_on_partial_transient(tmp_path: Path):
    """If at least one file was really fixed, a transient error on a DIFFERENT
    file does NOT discard the round — it returns normally with the real fix."""
    from skyn3t.adapters.llm_client import TransientLLMError

    scaffold = tmp_path / "scaffold"
    (scaffold / "src").mkdir(parents=True)
    good = scaffold / "src" / "Good.jsx"
    good.write_text("export default function Good() { return <div>old</div>; }\n")
    bad = scaffold / "src" / "Bad.jsx"
    bad.write_text("export default function Bad() { return <div>old</div>; }\n")

    class MixedLLM:
        async def complete(self, prompt, max_tokens, temperature):  # noqa: ARG002
            if "Bad.jsx" in prompt:
                raise TransientLLMError("openrouter 429 after 8 attempts")
            return "export default function Good() { return <div>new</div>; }\n"

    issues = [
        FileIssue(
            path="src/Good.jsx",
            error_message="fix me",
            suggested_action="regenerate",
        ),
        FileIssue(
            path="src/Bad.jsx",
            error_message="fix me too",
            suggested_action="regenerate",
        ),
    ]

    result = asyncio.run(
        apply_targeted_fix(
            scaffold_dir=scaffold, issues=issues, llm_client=MixedLLM()
        )
    )
    # Real fix kept; the throttled file preserved; no exception raised.
    assert "src/Good.jsx" in result.files_changed
    assert "src/Bad.jsx" in result.files_preserved
