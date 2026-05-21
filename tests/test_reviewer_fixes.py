from __future__ import annotations

import json
from pathlib import Path

import pytest

from skyn3t.agents.reviewer_fixes import FixCandidate, _try_fix_one
import skyn3t.adapters as adapters


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_try_fix_one_backfills_missing_local_hook_import(monkeypatch, tmp_path: Path) -> None:
    artifact = tmp_path / "habit-app"
    scaffold = artifact / "scaffold"
    _write(
        scaffold,
        "package.json",
        json.dumps(
            {
                "name": "habit-app",
                "dependencies": {"react": "^18"},
                "devDependencies": {"vite": "^5"},
            }
        ),
    )
    _write(
        scaffold,
        "src/hooks/useHabits.js",
        "export function useHabits() {\n"
        "  return { habits: [] };\n"
        "}\n"
        "export default useHabits;\n",
    )

    class FakeLLMClient:
        def __init__(self, default_model=None, backend=None):  # noqa: D401, ARG002
            pass

        async def complete(self, prompt, system, max_tokens, temperature, timeout):  # noqa: ARG002
            return (
                "import { useAuth } from './useAuth';\n"
                "export function useHabits() {\n"
                "  const { user } = useAuth();\n"
                "  return { user, habits: [] };\n"
                "}\n"
                "export default useHabits;\n"
            )

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(adapters, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(
        "skyn3t.agents.reviewer_fixes.MODEL_LADDER",
        ("openrouter/owl-alpha",),
    )

    result = await _try_fix_one(
        FixCandidate(
            file_path="src/hooks/useHabits.js",
            issue="Hook should use auth context instead of placeholder text.",
        ),
        brief="Build a habit tracker with streaks",
        scaffold_dir=scaffold,
    )

    assert result.ok is True
    use_auth = scaffold / "src/hooks/useAuth.js"
    assert use_auth.is_file()
    body = use_auth.read_text(encoding="utf-8")
    assert "export function useAuth" in body
    assert "export default useAuth" in body


@pytest.mark.asyncio
async def test_try_fix_one_llm_returns_empty(monkeypatch, tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold,
        "package.json",
        json.dumps({"name": "test", "dependencies": {"react": "^18"}}),
    )
    _write(scaffold, "src/App.jsx", "export default function App(){return null;}")

    class EmptyLLMClient:
        def __init__(self, default_model=None, backend=None):
            pass

        async def complete(self, prompt, system, max_tokens, temperature, timeout):
            return ""

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(adapters, "LLMClient", EmptyLLMClient)
    monkeypatch.setattr(
        "skyn3t.agents.reviewer_fixes.MODEL_LADDER",
        ("openrouter/owl-alpha",),
    )

    result = await _try_fix_one(
        FixCandidate(file_path="src/App.jsx", issue="fix this"),
        brief="test app",
        scaffold_dir=scaffold,
    )

    assert result.ok is False
    assert "empty" in result.error.lower() or "llm" in result.error.lower()


@pytest.mark.asyncio
async def test_try_fix_one_missing_scaffold_dir(tmp_path: Path) -> None:
    result = await _try_fix_one(
        FixCandidate(file_path="src/App.jsx", issue="fix this"),
        brief="test app",
        scaffold_dir=tmp_path / "does_not_exist",
    )

    assert result.ok is False


@pytest.mark.asyncio
async def test_try_fix_one_no_issue_in_candidate(monkeypatch, tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold,
        "package.json",
        json.dumps({"name": "test", "dependencies": {"react": "^18"}}),
    )
    _write(scaffold, "src/App.jsx", "export default function App(){return null;}")

    class FakeLLMClient:
        def __init__(self, default_model=None, backend=None):
            pass

        async def complete(self, prompt, system, max_tokens, temperature, timeout):
            return "export default function App(){return <div className='app'><h1>Hello</h1><p>This is a longer response so it passes the length check.</p></div>;}"

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(adapters, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(
        "skyn3t.agents.reviewer_fixes.MODEL_LADDER",
        ("openrouter/owl-alpha",),
    )

    result = await _try_fix_one(
        FixCandidate(file_path="src/App.jsx", issue=""),
        brief="test app",
        scaffold_dir=scaffold,
    )

    # Should still attempt the fix even with empty issue.
    assert result.ok is True
