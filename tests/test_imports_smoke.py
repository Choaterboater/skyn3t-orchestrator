"""Smoke-import every public skyn3t.* module.

This is a guard rail, not a test of behavior. It catches the class of
bug where a commit adds `from skyn3t.foo import bar` without creating
`skyn3t/foo.py` — which silently breaks pytest collection for every
test module transitively importing the offending file.

The original `skyn3t.prompt_compression` regression broke 47 test
modules at once. A smoke import would have caught it in <1s before
the broken commit ever shipped.

Notes:
- Some modules legitimately raise ImportError when an optional dep
  is missing (e.g. `typer`, GPU SDKs). Those exceptions are reported
  but do not fail the test, since they're environment-conditional.
  The hard failure is `ModuleNotFoundError` for a `skyn3t.*` symbol
  the codebase itself claims to provide.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKYN3T_ROOT = REPO_ROOT / "skyn3t"

# Modules that are entry points or have intentional side effects on import.
# Skipping them keeps the smoke test side-effect-free.
SKIP_MODULES: frozenset[str] = frozenset({
    "skyn3t.__main__",
    "skyn3t.cli.main",
})


def _discover_modules() -> list[str]:
    names: list[str] = []
    package = importlib.import_module("skyn3t")
    for info in pkgutil.walk_packages(package.__path__, prefix="skyn3t."):
        if info.name in SKIP_MODULES:
            continue
        names.append(info.name)
    return sorted(names)


@pytest.mark.parametrize("module_name", _discover_modules())
def test_module_imports(module_name: str) -> None:
    """Every skyn3t.* submodule must be importable.

    Optional-dependency failures (e.g. missing `typer`, missing GPU
    SDK) are skipped, not failed — they reflect the environment, not
    a codebase defect. A *skyn3t.* symbol that the codebase itself
    references but doesn't define is a real defect and will fail.
    """
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".")[0]
        if missing and missing != "skyn3t":
            pytest.skip(f"optional dependency missing: {exc.name}")
        raise


def _static_skyn3t_import_targets() -> list[tuple[str, int, str]]:
    """Scan every .py for `from skyn3t.X import Y` and return (file, line, target).

    Includes lazy imports nested inside functions or try-blocks, which the
    runtime smoke test above can't see.
    """
    import ast

    results: list[tuple[str, int, str]] = []
    for path in SKYN3T_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("skyn3t."):
                results.append((str(path.relative_to(REPO_ROOT)), node.lineno, node.module))
    return results


def test_no_skyn3t_imports_target_missing_modules() -> None:
    """Every `from skyn3t.X import Y` in the codebase must resolve.

    Catches lazy-import bugs (`try: from skyn3t.X import Y` inside a
    function) that the parametrized smoke test misses because the
    importing module loads fine until that branch executes.
    """
    targets = _static_skyn3t_import_targets()
    missing: list[str] = []
    for file_, line, mod in targets:
        parts = mod.split(".")
        base = SKYN3T_ROOT.parent / Path(*parts)
        if base.with_suffix(".py").exists() or (base / "__init__.py").exists():
            continue
        missing.append(f"{file_}:{line} → {mod}")
    assert not missing, "broken skyn3t.* imports:\n  " + "\n  ".join(sorted(set(missing)))
