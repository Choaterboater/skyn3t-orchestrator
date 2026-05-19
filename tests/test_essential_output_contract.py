"""Tests for the essential-output contract between studio pipeline stages.

Each completed stage's `output["summary"]` accumulates into a
`prior_summaries` dict that's threaded into the next stage's `input_data`.
This lets downstream agents see "what did upstream decide" without
crawling every artifact file. Per Kimi K2.6's context-sharding pattern:
pass essential outputs, not full traces.

The integration of this with `StudioRunner.run()` is hard to unit-test
without spinning the full pipeline, but the contract surface is small:
a per-stage cap helper and a dict-accumulation pattern. These tests
pin both pieces down so future regressions show up locally.
"""

from __future__ import annotations

import pytest

from skyn3t.studio.runner import StudioRunner


class TestBoundEssentialSummary:
    """The per-stage cap is the foundation of the context-sharding
    contract. If it stops enforcing the cap, downstream input_data can
    blow up — 8 stages × runaway summaries = unbounded growth."""

    def test_empty_returns_empty(self):
        assert StudioRunner._bound_essential_summary("") == ""
        assert StudioRunner._bound_essential_summary(None) == ""
        assert StudioRunner._bound_essential_summary("   ") == ""

    def test_short_passes_through(self):
        s = "Brainstormed 8 angles; primary direction set."
        assert StudioRunner._bound_essential_summary(s) == s

    def test_strips_whitespace(self):
        assert StudioRunner._bound_essential_summary("  hello  ") == "hello"

    def test_at_cap_passes_through(self):
        # exactly 2000 chars — the cap allows this
        s = "x" * StudioRunner._PRIOR_SUMMARY_CAP
        out = StudioRunner._bound_essential_summary(s)
        assert len(out) == StudioRunner._PRIOR_SUMMARY_CAP

    def test_over_cap_truncated_with_ellipsis(self):
        s = "x" * (StudioRunner._PRIOR_SUMMARY_CAP + 500)
        out = StudioRunner._bound_essential_summary(s)
        assert len(out) == StudioRunner._PRIOR_SUMMARY_CAP
        assert out.endswith("...")

    def test_non_string_inputs_coerced(self):
        # Defensive: agents sometimes return ints/dicts/None as summary
        # if their output construction is broken. Coerce + cap rather
        # than blowing up.
        assert StudioRunner._bound_essential_summary(42) == "42"
        # Don't care about exact dict repr, just that it doesn't crash
        out = StudioRunner._bound_essential_summary({"k": "v"})
        assert isinstance(out, str)
        assert "v" in out


class TestSummarizeStageOutput:
    """`_summarize_stage_output` is what feeds `_bound_essential_summary`.
    These tests pin its behavior for the common shapes."""

    def test_summary_field_preferred(self):
        out = StudioRunner._summarize_stage_output({
            "summary": "Brand pack written (7 files).",
            "files": ["brand.md", "palette.json"],
        })
        assert "Brand pack" in out

    def test_falls_back_to_file_count(self):
        out = StudioRunner._summarize_stage_output({"files": ["a.md", "b.md"]})
        assert "Produced 2 files" in out

    def test_falls_back_to_reason(self):
        out = StudioRunner._summarize_stage_output({"reason": "skipped: no brief"})
        assert "skipped" in out

    def test_empty_output(self):
        assert StudioRunner._summarize_stage_output({}) == ""
        assert StudioRunner._summarize_stage_output(None) == ""

    def test_summary_capped_at_240_for_stage_records(self):
        # _summarize_stage_output's cap is 240 (used for stage history
        # records). _bound_essential_summary's cap is 2000 (used for
        # downstream input_data threading). Different consumers, different
        # ceilings.
        long_summary = "x" * 500
        out = StudioRunner._summarize_stage_output({"summary": long_summary})
        assert len(out) == 240


class TestPriorSummariesAccumulation:
    """Documents the dict-accumulation pattern used inside the stage loop.
    We can't easily unit-test the full StudioRunner.run() path, but we can
    pin down what the contract LOOKS like so future code review can spot
    drift: prior_summaries is a {stage_name: bounded_summary} dict, each
    value capped, empty summaries dropped, threaded into the NEXT stage's
    input_data unchanged."""

    def test_accumulation_drops_empty_summaries(self):
        # Simulate the loop body: only non-empty bounded summaries are
        # added to prior_summaries.
        prior = {}
        for name, raw in [
            ("brainstorm", "Brainstormed 8 angles."),
            ("architect", ""),  # empty — should be dropped
            ("designer", "   "),  # whitespace — should be dropped
            ("code", "Scaffolded 15/15 planned file(s)."),
        ]:
            bounded = StudioRunner._bound_essential_summary(raw)
            if bounded:
                prior[name] = bounded
        assert set(prior.keys()) == {"brainstorm", "code"}
        assert prior["brainstorm"] == "Brainstormed 8 angles."
        assert prior["code"].startswith("Scaffolded")

    def test_accumulation_preserves_insertion_order(self):
        # Python 3.7+ dicts preserve insertion order. Order matters so
        # downstream agents can scan "what came before me" chronologically.
        prior = {}
        for name in ["brainstorm", "architect", "designer", "code"]:
            prior[name] = StudioRunner._bound_essential_summary(f"{name} done")
        assert list(prior.keys()) == ["brainstorm", "architect", "designer", "code"]

    def test_combined_size_well_under_budget(self):
        # 8 stages each at the cap is the worst case. Verify it stays
        # under 20KB — comfortably below any prompt budget.
        prior = {}
        for i in range(8):
            prior[f"stage_{i}"] = StudioRunner._bound_essential_summary("x" * 5000)
        total = sum(len(v) for v in prior.values())
        assert total <= 8 * StudioRunner._PRIOR_SUMMARY_CAP
        assert total < 20_000
