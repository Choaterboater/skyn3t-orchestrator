"""Regression tests for the audit fix in CodeAgent (group: code_agent).

Covers bug #2 from the audit: the backfilled-import LLM path
(``_backfill_unresolved_local_imports``) wrote the model's output to disk
WITHOUT the fence/prelude sanitization that every other generation path
applies, so a fenced or prose-wrapped response produced an unparseable
file. It also rejected any body containing the substring ``"TODO"``,
which wrongly discarded legitimate files that merely mention "TODO"
(a real to-do app's own UI text, an ordinary code comment, etc.).

The fix: after ``body = await coro``, run ``_strip_cli_prelude`` +
``_strip_fences`` + ``_strip_copilot_footer``, gate acceptance on
``_syntax_ok``, and replace the blanket ``"TODO" in body`` reject with a
genuine stub-marker check.

These call the helper directly with a fake ``llm_client`` whose
``.complete()`` returns the problematic body. The import target is a
uniquely-named component that no deterministic generator handles, so the
LLM path is the one exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skyn3t.agents.code_agent import CodeAgent


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class _FakeLLM:
    """Minimal stand-in: returns a fixed body from .complete()."""

    def __init__(self, body: str) -> None:
        self._body = body
        self.calls = 0

    async def complete(self, *args, **kwargs) -> str:  # noqa: D401
        self.calls += 1
        return self._body


def _app_importing(component: str) -> str:
    return (
        f"import {component} from './components/{component}.jsx';\n"
        f"export default function App() {{ return <{component}/>; }}\n"
    )


@pytest.mark.asyncio
async def test_backfill_sanitizes_fenced_and_prose_llm_output(tmp_path: Path) -> None:
    """A fenced + prose-wrapped LLM body must be stripped to clean,
    parseable source — not written verbatim.

    Before the fix the fence/prose was written as-is and the resulting
    .jsx was unparseable; the test would find a ``` fence on disk.
    """
    out_dir = tmp_path / "scaffold"
    component = "FencyUnmappedWidget"
    _write(out_dir / "src" / "App.jsx", _app_importing(component))

    # Leading prose + a fenced code block — the exact shape the other
    # generation paths sanitize via _strip_cli_prelude + _strip_fences.
    # The old backfill code wrote this verbatim, leaving the ``` fence
    # (and prose) on disk and breaking the build.
    fenced = (
        "Here is the file you asked for:\n"
        "```jsx\n"
        f"export default function {component}() {{\n"
        "  return <div>real implementation</div>;\n"
        "}\n"
        "```"
    )
    fake = _FakeLLM(fenced)

    agent = CodeAgent()
    out = await agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[str(out_dir / "src" / "App.jsx")],
        stack="react_vite",
        brief="Build a dashboard",
        llm_client=fake,
    )

    target = out_dir / "src" / "components" / f"{component}.jsx"
    assert target.is_file()
    body = target.read_text()
    assert fake.calls == 1, "LLM path should have been exercised"
    # The fence and the surrounding prose must be gone.
    assert "```" not in body, "markdown fence leaked onto disk"
    assert "Here is the file" not in body
    assert "Let me know" not in body
    # The real implementation survived and is what got written.
    assert f"export default function {component}" in body
    # Not the deterministic placeholder.
    assert "@skyn3t-backfill-stub:" not in body
    assert str(target) in out


@pytest.mark.asyncio
async def test_backfill_accepts_legit_body_mentioning_todo(tmp_path: Path) -> None:
    """A valid file that merely mentions the word "TODO" must be accepted.

    Before the fix the blanket ``"TODO" in body`` reject discarded this
    body and fell back to a stub placeholder.
    """
    out_dir = tmp_path / "scaffold"
    component = "TodoListUnmappedWidget"
    _write(out_dir / "src" / "App.jsx", _app_importing(component))

    legit = (
        f"export default function {component}() {{\n"
        '  const items = ["Buy milk", "Finish TODO list"];\n'
        "  return <ul>{items.map((t) => <li key={t}>{t}</li>)}</ul>;\n"
        "}\n"
    )
    fake = _FakeLLM(legit)

    agent = CodeAgent()
    await agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[str(out_dir / "src" / "App.jsx")],
        stack="react_vite",
        brief="Build a todo app",
        llm_client=fake,
    )

    target = out_dir / "src" / "components" / f"{component}.jsx"
    assert target.is_file()
    body = target.read_text()
    # The legit body was accepted, not replaced by a stub.
    assert "@skyn3t-backfill-stub:" not in body, \
        "legit body mentioning 'TODO' was wrongly rejected"
    assert "Finish TODO list" in body
    assert f"export default function {component}" in body
