"""Tests for skyn3t.agents.stack_templates — the deterministic-skeleton path.

The LLM is competent at writing file content but unreliable at picking a
correct file SHAPE for a given ecosystem. These tests pin down the
keyword detector + each template's required files. A wrong shape (e.g.
Next 12 ``pages/`` instead of Next 14 ``app/``) breaks the build before
any code runs, so this layer matters.
"""

from __future__ import annotations

import pytest

from skyn3t.agents.stack_templates import (
    detect_stack,
    plan_for_stack,
    STACK_TEMPLATES,
    template_keys,
)


# ─── detect_stack ──────────────────────────────────────────────────────


@pytest.mark.parametrize("brief,expected", [
    ("Build me a Next.js dashboard with auth", "next"),
    ("Spin up a nextjs marketing site", "next"),
    ("React + Vite single-page app for tracking habits", "react_vite"),
    ("Build a React SPA for the team", "react_vite"),
    ("FastAPI service for /users CRUD", "fastapi"),
    ("Make a fast-api backend with sqlite", "fastapi"),
    ("Tiny Flask app that serves the form", "flask"),
    ("Node CLI to ping URLs in a list", "node_cli"),
    ("Python CLI that lists open ports", "python_cli"),
    ("argparse-driven script for renaming files", "python_cli"),
    ("Tic-tac-toe browser game", "static_site"),
    ("Build a todo app", "static_site"),
    ("Landing page for the launch", "static_site"),
])
def test_detect_stack_picks_expected_template(brief, expected):
    assert detect_stack(brief) == expected, brief


@pytest.mark.parametrize("brief", [
    "",
    "   ",
    "Write a blog post about deployment",   # docs-shaped
    "Research the competitive landscape",   # not a build at all
    "Brainstorm names for the product",     # no stack signal
])
def test_detect_stack_returns_none_when_no_signal(brief):
    assert detect_stack(brief) is None


def test_detect_stack_case_insensitive():
    assert detect_stack("BUILD A FASTAPI SERVICE") == "fastapi"
    assert detect_stack("Next.JS portfolio") == "next"


def test_detect_stack_prefers_more_specific_first():
    """When a brief mentions both 'react' and 'next.js', the next template
    should win — it's the more specific match and ships first in the
    trigger list."""
    assert detect_stack("Next.js app with React server components") == "next"


# ─── plan_for_stack ────────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(STACK_TEMPLATES.keys()))
def test_each_template_has_a_readme(key):
    plan = plan_for_stack(key)
    assert plan is not None
    paths = [rel for rel, _ in plan]
    assert "README.md" in paths, f"template {key} is missing README.md"


@pytest.mark.parametrize("key,must_contain", [
    ("static_site", {"index.html", "style.css", "script.js"}),
    ("python_cli", {"main.py", "requirements.txt"}),
    ("fastapi", {"src/main.py", "tests/test_health.py", "requirements.txt"}),
    ("flask", {"app.py", "templates/index.html"}),
    ("node_cli", {"index.js", "package.json"}),
    ("react_vite", {"index.html", "src/main.jsx", "src/App.jsx", "package.json", "vite.config.js"}),
    ("next", {"app/page.tsx", "app/layout.tsx", "package.json", "tsconfig.json"}),
])
def test_template_contains_required_files(key, must_contain):
    plan = plan_for_stack(key)
    paths = {rel for rel, _ in (plan or [])}
    missing = must_contain - paths
    assert not missing, f"{key} is missing: {missing}"


def test_plan_for_unknown_stack_returns_none():
    assert plan_for_stack("definitely-not-a-stack") is None
    assert plan_for_stack("") is None


def test_template_keys_returns_sorted_list():
    keys = template_keys()
    assert keys == sorted(keys)
    assert "fastapi" in keys
    assert "next" in keys


def test_every_template_purpose_is_nonempty():
    """Every (path, purpose) tuple must have a non-empty one-liner so
    CodeAgent's per-file LLM call has real direction."""
    for key, plan in STACK_TEMPLATES.items():
        for rel, purpose in plan:
            assert purpose.strip(), f"empty purpose for {key} → {rel}"
            assert len(purpose) < 200, f"purpose too long for {key} → {rel}"


# ─── hint_for_stack ────────────────────────────────────────────────────


def test_hint_for_unknown_or_none_returns_empty_string():
    """No hint = empty string (not None) so callers can blindly concat."""
    from skyn3t.agents.stack_templates import hint_for_stack
    assert hint_for_stack(None) == ""
    assert hint_for_stack("") == ""
    assert hint_for_stack("definitely-not-a-stack") == ""


@pytest.mark.parametrize("stack", sorted(STACK_TEMPLATES.keys()))
def test_every_template_has_a_build_hint(stack):
    """Every stack with a file template should also have an idiom hint so
    Phase 2 is consistent across stacks."""
    from skyn3t.agents.stack_templates import hint_for_stack
    hint = hint_for_stack(stack)
    assert hint, f"stack {stack} has a template but no build hint"
    # Reasonable length — short enough to fit in the system prompt budget.
    assert 60 <= len(hint) <= 700, f"hint length out of range for {stack}: {len(hint)}"


@pytest.mark.parametrize("stack,must_mention", [
    ("next", ("App Router", "app/")),
    ("react_vite", ("Vite", "createRoot")),
    ("fastapi", ("FastAPI", "pydantic")),
    ("flask", ("Flask",)),
    ("node_cli", ("Node", "process.argv")),
    ("python_cli", ("argparse",)),
    ("static_site", ("HTML", "defer")),
])
def test_hint_for_stack_carries_the_idiom_anchor(stack, must_mention):
    """Each hint must mention the framework's modern-idiom anchor so the
    model doesn't default to outdated shapes (Next pages/, React classes,
    etc.)."""
    from skyn3t.agents.stack_templates import hint_for_stack
    hint = hint_for_stack(stack)
    missing = [phrase for phrase in must_mention if phrase not in hint]
    assert not missing, f"{stack} hint missing: {missing}"


def test_next_hint_explicitly_rejects_pages_router():
    """The single most common Next.js scaffold failure is the model
    defaulting to pages/ instead of app/. Pin this case down."""
    from skyn3t.agents.stack_templates import hint_for_stack
    hint = hint_for_stack("next")
    # Some form of "don't use pages/" must be present.
    assert "NEVER use" in hint and "pages/" in hint
