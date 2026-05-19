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

from skyn3t.agents.code_agent import _relevant_context


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

    def _ctx(self, *blocks: str) -> str:
        return "\n\n---\n\n".join(blocks)

    def test_essentials_kept_for_frontend_file(self):
        ctx = self._ctx(self.ESSENTIALS, self.BRAND, self.ARCHITECTURE)
        out = _relevant_context(ctx, "src/App.jsx")
        assert "Upstream essentials" in out
        # Frontend wants brand and architecture too.
        assert "brand.md" in out
        assert "architecture.md" in out

    def test_essentials_kept_for_config_file(self):
        # vite.config.js normally only gets architecture.md — brand is
        # noise for a config file. Essentials must still be present.
        ctx = self._ctx(self.ESSENTIALS, self.BRAND, self.ARCHITECTURE)
        out = _relevant_context(ctx, "vite.config.js")
        assert "Upstream essentials" in out
        assert "architecture.md" in out
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
