"""Tests for CodeAgent consuming prior_summaries from input_data.

PR #21 added a ``prior_summaries`` dict to every downstream stage's
``input_data`` — curated, bounded recaps of what each prior stage
decided. This was the producer side; CodeAgent now consumes it by
prepending an "Upstream essentials" block to its ``prior_context``.

These tests pin the consumer contract: the essentials block survives
the per-file ``_relevant_context`` filter (so the model sees it even
on focused per-file scaffold calls) and is structured so a reader
can tell which stage said what.
"""

from __future__ import annotations

from skyn3t.agents.code_agent import (
    _entrypoint_import_instructions,
    _relevant_context,
)


class TestRelevantContextKeepsEssentials:
    """`_relevant_context` filters prior_context per-file extension to
    stay under CLI prompt-size limits. The Upstream essentials block is
    small enough (≤16KB total budget across 8 stages, usually <1KB) to
    survive every filter — without that, prior_summaries would only
    reach the planning prompt and not the per-file write calls."""

    ESSENTIALS = (
        "### Upstream essentials\n\n"
        "Brief, curated recaps from each completed prior stage. "
        "Treat these as the canonical 'what did upstream decide'.\n\n"
        "- **architect**: Picked react-vite + express on port 3000.\n"
        "- **designer**: Brand pack written (7 files)."
    )

    ARCHITECTURE = (
        "### architecture.md\n\n"
        "## Overview\nA habit tracker on react-vite + express."
    )

    BRAND = "### brand.md\n\nPrimary terracotta on cream."
    DESIGN = "### design.md\n\nUse an operator dashboard, not raw JSON dumps."

    def _ctx(self, *blocks: str) -> str:
        return "\n\n---\n\n".join(blocks)

    def test_essentials_kept_for_frontend_file(self):
        ctx = self._ctx(self.ESSENTIALS, self.DESIGN, self.BRAND, self.ARCHITECTURE)
        out = _relevant_context(ctx, "src/App.jsx")
        assert "Upstream essentials" in out
        # Frontend wants design, brand, and architecture too.
        assert "design.md" in out
        assert "brand.md" in out
        assert "architecture.md" in out

    def test_essentials_kept_for_config_file(self):
        # vite.config.js normally only gets architecture.md — brand is
        # noise for a config file. Essentials must still be present.
        ctx = self._ctx(self.ESSENTIALS, self.DESIGN, self.BRAND, self.ARCHITECTURE)
        out = _relevant_context(ctx, "vite.config.js")
        assert "Upstream essentials" in out
        assert "architecture.md" in out
        assert "design.md" not in out
        assert "brand.md" not in out

    def test_essentials_kept_for_server_file(self):
        ctx = self._ctx(self.ESSENTIALS, self.BRAND, self.ARCHITECTURE)
        out = _relevant_context(ctx, "server/index.js")
        assert "Upstream essentials" in out
        # Server wants architecture, NOT brand.
        assert "brand.md" not in out

    def test_absent_essentials_does_not_break_filter(self):
        # Pre-PR builds (or builds where no upstream stage produced a
        # summary) have prior_context with no essentials block. Filter
        # must still work normally.
        ctx = self._ctx(self.ARCHITECTURE, self.BRAND)
        out = _relevant_context(ctx, "src/App.jsx")
        assert "Upstream essentials" not in out
        assert "architecture.md" in out

    def test_essentials_listing_format_survives(self):
        # The bullet structure (`- **stage**: summary`) is the
        # contract — a reader has to be able to tell who said what.
        ctx = self._ctx(self.ESSENTIALS, self.ARCHITECTURE)
        out = _relevant_context(ctx, "src/App.jsx")
        assert "- **architect**: Picked react-vite + express on port 3000." in out
        assert "- **designer**: Brand pack written (7 files)." in out


# ─── entrypoint "must import planned components" instructions ─────────


class TestEntrypointImportInstructions:
    """When generating an entrypoint (App.jsx / page.tsx) alongside
    planned component files, the prompt must instruct the LLM to
    IMPORT those components rather than reinvent inline. e79bc0
    shipped App.jsx with inline HabitCard etc. while the planned
    HabitCard.jsx (and 6 others) sat orphaned in components/."""

    HABIT_PLAN = [
        {"path": "src/App.jsx", "purpose": "entry"},
        {"path": "src/components/HabitCard.jsx", "purpose": "habit row"},
        {"path": "src/components/HabitList.jsx", "purpose": "list"},
        {"path": "src/components/StreakBadge.jsx", "purpose": "badge"},
        {"path": "src/components/WeeklyGrid.jsx", "purpose": "grid"},
        {"path": "vite.config.js", "purpose": "vite config"},
    ]

    def test_app_jsx_gets_import_pin_when_components_planned(self):
        out = _entrypoint_import_instructions(
            rel="src/App.jsx",
            file_specs=self.HABIT_PLAN,
        )
        assert out
        assert "IMPORT, do NOT redefine" in out
        # Should list at least the components/* files.
        assert "components/HabitCard.jsx" in out
        assert "components/HabitList.jsx" in out
        # Non-component files like vite.config.js should NOT appear.
        assert "vite.config.js" not in out

    def test_main_jsx_also_gets_pin(self):
        out = _entrypoint_import_instructions(
            rel="src/main.jsx",
            file_specs=self.HABIT_PLAN,
        )
        assert out
        assert "IMPORT, do NOT redefine" in out

    def test_next_page_tsx_also_gets_pin(self):
        out = _entrypoint_import_instructions(
            rel="app/page.tsx",
            file_specs=self.HABIT_PLAN,
        )
        assert out

    def test_non_entrypoint_file_gets_no_pin(self):
        # A component file generating itself — no instruction needed
        out = _entrypoint_import_instructions(
            rel="src/components/HabitCard.jsx",
            file_specs=self.HABIT_PLAN,
        )
        assert out == ""

    def test_no_components_planned_gives_no_pin(self):
        # CLI / static-site / API-only briefs may plan zero component files
        plan_no_components = [
            {"path": "src/App.jsx", "purpose": "entry"},
            {"path": "vite.config.js", "purpose": "vite config"},
            {"path": "package.json", "purpose": "deps"},
        ]
        out = _entrypoint_import_instructions(
            rel="src/App.jsx",
            file_specs=plan_no_components,
        )
        assert out == ""

    def test_caps_listing_at_12_components(self):
        # A huge component plan shouldn't blow up the entrypoint prompt.
        big_plan = [
            {"path": "src/App.jsx", "purpose": "entry"},
        ] + [
            {"path": f"src/components/C{i}.jsx", "purpose": f"comp {i}"}
            for i in range(20)
        ]
        out = _entrypoint_import_instructions(
            rel="src/App.jsx",
            file_specs=big_plan,
        )
        assert out
        # First 12 listed
        assert "components/C0.jsx" in out
        assert "components/C11.jsx" in out
        # Beyond 12 referenced via summary, not listed
        assert "components/C19.jsx" not in out
        assert "and 8 more" in out


_DESIGN_CTX = (
    "### components.md\n\n"
    "| Component | Classes |\n"
    "| StatusPill | `inline-flex items-center px-2 py-0.5 "
    "border-[var(--brand-primary)] text-[10px] uppercase` |\n"
    "| DeviceCard | `rounded-lg bg-[var(--brand-bg)] p-4` |\n\n"
    "### brand.md\n\nDark NOC theme.\n"
)


def test_component_spec_directive_elevates_the_components_exact_classes():
    """80->85 adherence fix: a UI component file must get its OWN spec row
    surfaced at the top with a 'use verbatim' instruction, so the model stops
    hardcoding classes instead of applying components.md."""
    out = _relevant_context(_DESIGN_CTX, "src/components/StatusPill.jsx")
    assert out.lstrip().startswith("## DESIGN SPEC")
    assert "apply VERBATIM for `StatusPill`" in out
    assert "do NOT hardcode" in out
    head = out.split("### components.md")[0]
    assert "border-[var(--brand-primary)]" in head
    assert "DeviceCard" not in head


def test_component_spec_directive_skips_non_components():
    assert "DESIGN SPEC" not in _relevant_context(_DESIGN_CTX, "vite.config.js")
    assert "DESIGN SPEC" not in _relevant_context(_DESIGN_CTX, "src/App.jsx")
    assert "DESIGN SPEC" not in _relevant_context(_DESIGN_CTX, "server/routes/api.js")
