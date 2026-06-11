"""Phase 3 regression tests for owner_code_agent.

Covers the four contracts owned by this leaf:

  * ``code_agent._extract_fenced_only`` — the hardened ``_strip_fences``
    that drops trailing prose after a closing fence (the real sonos.js
    leak) while keeping every already-correct case green.
  * ``code_agent._is_entrypoint_stub`` — entrypoint generation-failure
    stub detection.
  * ``code_stage.planned_imports_signal`` — the extra output dict keys
    (planned_imports / stub_markers / entrypoint_files /
    entrypoint_is_stub), exercised through the pure ``_collect_stub_signal``
    helper and the budget-gated ``_regen_entrypoint_if_needed`` guard.

All file IO uses tmp dirs. NO LLM/CLI/network calls: the regen tests
only exercise the deterministic short-circuit branches (non-entry file,
already-real body, exhausted budget) so they never reach a backend.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.agents.code_agent import (
    CodeAgent,
    _collect_stub_signal,
    _inline_redefined_planned,
    _is_entrypoint_stub,
    _placeholder_for,
    _planned_component_names,
    _strip_fences,
)

# --------------------------------------------------------------------------
# _strip_fences (contract: _extract_fenced_only)
# --------------------------------------------------------------------------


def test_strip_fences_passthrough_when_no_fence() -> None:
    body = "export const x = 1;\nconst y = 2;\n"
    assert _strip_fences(body) == body


def test_strip_fences_empty() -> None:
    assert _strip_fences("") == ""


def test_strip_fences_drops_leading_and_trailing_prose() -> None:
    """A proper paired fence with prose on BOTH sides yields only the
    fenced contents."""
    body = (
        "Sure! Here is the file:\n"
        "```python\n"
        "import os\n"
        "print(os.getcwd())\n"
        "```\n"
        "Let me know if you need anything else."
    )
    assert _strip_fences(body) == "import os\nprint(os.getcwd())"


def test_strip_fences_sonos_trailing_prose_leak() -> None:
    """Regression fixture for the real sonos.js leak: a closing fence
    with NO preceding newline, followed by trailing prose. The trailing
    prose must be discarded, not left on disk."""
    body = (
        "Here is sonos.js:\n"
        "```js\n"
        "export function connect() { return true; }```\n"
        "That should work for sonos over upnp."
    )
    assert _strip_fences(body) == "export function connect() { return true; }"


def test_strip_fences_audit_fixture_unchanged() -> None:
    """The exact shape the audit test pins (leading prose + fence, close
    on its own line, no trailing newline) is unchanged."""
    body = (
        "Here is the file you asked for:\n"
        "```jsx\n"
        "export default function W() {\n"
        "  return <div>real</div>;\n"
        "}\n"
        "```"
    )
    assert _strip_fences(body) == (
        "export default function W() {\n  return <div>real</div>;\n}"
    )


def test_strip_fences_open_fence_no_close_keeps_remainder() -> None:
    """v44 behavior preserved: an opening fence with no closing fence
    drops the opening fence line and keeps the rest (we can't tell where
    code ends without a close)."""
    body = "```js\nexport const x = 1;\nconst y = 2;\n"
    assert _strip_fences(body) == "export const x = 1;\nconst y = 2;\n"


def test_strip_fences_lone_trailing_fence_no_opening() -> None:
    """v43 behavior preserved: a lone trailing ``` with no opening fence
    is stripped, both with and without a trailing newline."""
    assert _strip_fences("export const x = 1;\nconst y = 2;\n```") == (
        "export const x = 1;\nconst y = 2;"
    )
    assert _strip_fences("export const x = 1;\n```\n") == "export const x = 1;"


# --------------------------------------------------------------------------
# _is_entrypoint_stub
# --------------------------------------------------------------------------


def test_is_entrypoint_stub_detects_placeholder() -> None:
    ph = _placeholder_for("src/App.jsx", "top-level app", "react_vite")
    assert _is_entrypoint_stub(ph, "src/App.jsx") is True


def test_is_entrypoint_stub_detects_markers() -> None:
    backfill = "// @skyn3t-backfill-stub: App\nexport default function App() {}\n"
    assert _is_entrypoint_stub(backfill, "src/App.jsx") is True

    todo = (
        "// TODO[skyn3t]: code generation failed for src/main.jsx\n"
        "export default function Main() { return null; }\n"
    )
    assert _is_entrypoint_stub(todo, "src/main.jsx") is True

    gen_failed = (
        "export default function App() { "
        "return <p>Generation failed for this component.</p>; }"
    )
    assert _is_entrypoint_stub(gen_failed, "src/App.jsx") is True


def test_is_entrypoint_stub_detects_export_default_null() -> None:
    assert _is_entrypoint_stub("// note\nexport default null;\n", "src/main.jsx") is True


def test_is_entrypoint_stub_real_app_is_not_stub() -> None:
    real = (
        "import Dashboard from './components/Dashboard.jsx';\n"
        "export default function App() { return <Dashboard />; }\n"
    )
    assert _is_entrypoint_stub(real, "src/App.jsx") is False


def test_is_entrypoint_stub_fail_open_on_edge_inputs() -> None:
    # Empty / None bodies and non-entry paths never flag.
    assert _is_entrypoint_stub("", "src/App.jsx") is False
    assert _is_entrypoint_stub(None, "src/App.jsx") is False  # type: ignore[arg-type]
    # A non-entry component that merely contains the marker is not an
    # *entrypoint* stub.
    marker_body = "// @skyn3t-backfill-stub\nexport default function C() {}\n"
    assert _is_entrypoint_stub(marker_body, "src/components/Card.jsx") is False


def test_is_entrypoint_stub_legit_todo_mention_not_flagged() -> None:
    """A real entry file that merely mentions the word TODO in UI text
    must NOT be flagged (the marker is the full failure phrase, not bare
    'TODO')."""
    legit = (
        "export default function App() {\n"
        '  const items = ["Buy milk", "Finish TODO list"];\n'
        "  return <ul>{items.map((t) => <li key={t}>{t}</li>)}</ul>;\n"
        "}\n"
    )
    assert _is_entrypoint_stub(legit, "src/App.jsx") is False


# --------------------------------------------------------------------------
# planned-component helpers
# --------------------------------------------------------------------------


def test_planned_component_names() -> None:
    specs = [
        {"path": "src/components/HabitCard.jsx"},
        {"path": "src/components/HabitList.tsx"},
        {"path": "src/App.jsx"},  # not a component
        {"path": "src/components/styles.css"},  # not jsx/tsx
        "notadict",
    ]
    assert sorted(_planned_component_names(specs)) == ["HabitCard", "HabitList"]


def test_inline_redefined_planned_detects_offender() -> None:
    specs = [
        {"path": "src/components/HabitCard.jsx"},
        {"path": "src/components/HabitList.jsx"},
    ]
    body = (
        "import HabitList from './components/HabitList.jsx';\n"
        "function HabitCard({ habit }) { return <div>{habit.name}</div>; }\n"
        "export default function App() { return <HabitList />; }\n"
    )
    assert _inline_redefined_planned(body, specs) == ["HabitCard"]


def test_inline_redefined_planned_clean_when_all_imported() -> None:
    specs = [{"path": "src/components/HabitCard.jsx"}]
    body = (
        "import HabitCard from './components/HabitCard.jsx';\n"
        "export default function App() { return <HabitCard />; }\n"
    )
    assert _inline_redefined_planned(body, specs) == []
    # named import also counts
    body2 = (
        "import { HabitCard } from './components/HabitCard.jsx';\n"
        "export default function App() { return <HabitCard />; }\n"
    )
    assert _inline_redefined_planned(body2, specs) == []


# --------------------------------------------------------------------------
# _collect_stub_signal (contract: code_stage.planned_imports_signal)
# --------------------------------------------------------------------------


def _mk_scaffold(tmp_path: Path) -> Path:
    out = tmp_path / "scaffold"
    (out / "src" / "components").mkdir(parents=True)
    return out


def test_collect_stub_signal_full(tmp_path: Path) -> None:
    out = _mk_scaffold(tmp_path)
    app = out / "src" / "App.jsx"
    app.write_text(_placeholder_for("src/App.jsx", "app", "react_vite"))
    card = out / "src" / "components" / "Card.jsx"
    card.write_text("// @skyn3t-backfill-stub: Card\nexport default function Card() { return null; }\n")
    header = out / "src" / "components" / "Header.jsx"
    header.write_text("export default function Header() { return <h1>Hi</h1>; }\n")
    cfg = out / "src" / "config.js"
    cfg.write_text("export default null;\n")

    file_specs = [
        {"path": "src/components/Card.jsx"},
        {"path": "src/components/Header.jsx"},
        {"path": "src/App.jsx"},
    ]
    files_written = [str(app), str(card), str(header), str(cfg)]

    planned, markers, entries, is_stub = _collect_stub_signal(
        out, file_specs, files_written
    )

    assert sorted(planned) == ["src/components/Card.jsx", "src/components/Header.jsx"]
    assert entries == ["src/App.jsx"]
    assert is_stub is True

    by_file = {m["file"]: m for m in markers}
    assert by_file["src/App.jsx"]["kind"] == "entrypoint-stub"
    assert by_file["src/components/Card.jsx"]["kind"] == "component-stub"
    assert by_file["src/config.js"]["kind"] == "export-default-null"
    # The real component is not flagged.
    assert "src/components/Header.jsx" not in by_file


def test_collect_stub_signal_real_entrypoint_not_stub(tmp_path: Path) -> None:
    out = _mk_scaffold(tmp_path)
    app = out / "src" / "App.jsx"
    app.write_text(
        "import Header from './components/Header.jsx';\n"
        "export default function App() { return <Header />; }\n"
    )
    header = out / "src" / "components" / "Header.jsx"
    header.write_text("export default function Header() { return <h1>Hi</h1>; }\n")

    planned, markers, entries, is_stub = _collect_stub_signal(
        out,
        [{"path": "src/components/Header.jsx"}, {"path": "src/App.jsx"}],
        [str(app), str(header)],
    )
    assert planned == ["src/components/Header.jsx"]
    assert entries == ["src/App.jsx"]
    assert is_stub is False
    assert markers == []


def test_collect_stub_signal_fail_open_on_bad_input(tmp_path: Path) -> None:
    out = _mk_scaffold(tmp_path)
    # Non-list file_specs and None files_written must not raise.
    assert _collect_stub_signal(out, "notalist", None) == ([], [], [], False)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# _regen_entrypoint_if_needed — deterministic short-circuit branches only
# (NO LLM call: these never reach a backend)
# --------------------------------------------------------------------------


def test_regen_noop_for_non_entry_file() -> None:
    agent = CodeAgent()
    agent._entrypoint_regen_budget = 2
    specs = [{"path": "src/components/Foo.jsx"}]
    stub = _placeholder_for("src/App.jsx", "app", "react_vite")
    out = asyncio.run(
        agent._regen_entrypoint_if_needed(
            rel="src/components/Foo.jsx",
            body=stub,
            purpose="c",
            brief="b",
            stack="react_vite",
            file_specs=specs,
        )
    )
    assert out == stub
    assert agent._entrypoint_regen_budget == 2  # untouched


def test_regen_noop_for_real_entry_body() -> None:
    agent = CodeAgent()
    agent._entrypoint_regen_budget = 2
    specs = [{"path": "src/components/Foo.jsx"}]
    real = (
        "import Foo from './components/Foo.jsx';\n"
        "export default function App() { return <Foo />; }\n"
    )
    out = asyncio.run(
        agent._regen_entrypoint_if_needed(
            rel="src/App.jsx",
            body=real,
            purpose="app",
            brief="b",
            stack="react_vite",
            file_specs=specs,
        )
    )
    assert out == real
    assert agent._entrypoint_regen_budget == 2  # untouched


def test_regen_returns_original_when_budget_exhausted() -> None:
    agent = CodeAgent()
    agent._entrypoint_regen_budget = 0
    specs = [{"path": "src/components/Foo.jsx"}]
    stub = _placeholder_for("src/App.jsx", "app", "react_vite")
    out = asyncio.run(
        agent._regen_entrypoint_if_needed(
            rel="src/App.jsx",
            body=stub,
            purpose="app",
            brief="b",
            stack="react_vite",
            file_specs=specs,
        )
    )
    assert out == stub
    assert agent._entrypoint_regen_budget == 0


def test_regen_returns_original_when_no_openrouter_key(monkeypatch) -> None:
    """With NO OpenRouter key available (env removed + settings stubbed),
    the regen returns the original stub unchanged and never makes a
    network call. Budget is still decremented (the attempt was 'spent')."""

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Stub get_settings so no real key is found.
    import skyn3t.config.settings as _settings_mod

    class _NoKeySettings:
        openrouter_api_key = None

    monkeypatch.setattr(_settings_mod, "get_settings", lambda: _NoKeySettings())

    agent = CodeAgent()
    agent._entrypoint_regen_budget = 1
    specs = [{"path": "src/components/Foo.jsx"}]
    stub = _placeholder_for("src/App.jsx", "app", "react_vite")
    out = asyncio.run(
        agent._regen_entrypoint_if_needed(
            rel="src/App.jsx",
            body=stub,
            purpose="app",
            brief="b",
            stack="react_vite",
            file_specs=specs,
        )
    )
    assert out == stub  # unchanged, no network call
    assert agent._entrypoint_regen_budget == 0  # the attempt was spent
