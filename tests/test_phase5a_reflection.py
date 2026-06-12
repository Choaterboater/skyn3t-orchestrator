"""Phase 5A — ReflectionPlannerHook (M3_reflection).

Tests for intelligence.reflection.build_retry_directive() + RetryDirective and
the error_signatures.signatures_for_blockers() helper.

These are PURE/sync unit tests: no I/O, no LLM calls, no event bus, no
orchestrator. Synthetic errors/blockers in, structured directive out.
"""


import pytest

from skyn3t.intelligence.error_signatures import signatures_for_blockers
from skyn3t.intelligence.reflection import (
    FailurePatternAnalyzer,
    RetryDirective,
    build_retry_directive,
    reflective_retry_enabled,
)


# --------------------------------------------------------------------------
# Graceful no-op
# --------------------------------------------------------------------------
def test_no_failure_context_is_passthrough():
    """No error/hint/blockers/backend/stages -> augmented_brief == brief."""
    brief = "Build a budget tracker web app"
    d = build_retry_directive(brief=brief)
    assert isinstance(d, RetryDirective)
    assert d.augmented_brief == brief
    assert d.prompt_patches == []
    assert d.forced_stage_tier == {}
    assert d.avoid_backends == []
    assert d.signatures == []


def test_empty_brief_no_context_passthrough():
    d = build_retry_directive(brief="")
    assert d.augmented_brief == ""
    assert d.signatures == []


# --------------------------------------------------------------------------
# Build-hint driven directive (syntax error)
# --------------------------------------------------------------------------
def test_build_hint_yields_signature_and_patches():
    d = build_retry_directive(
        brief="Build a todo app",
        build_hint="SyntaxError: unexpected token in src/App.jsx",
    )
    # signature derived from the build-log classifier
    assert any("syntax_error" in s for s in d.signatures)
    # syntax_error pattern -> output-format prompt patch
    assert d.prompt_patches, "expected prompt patches for syntax_error"
    assert any("valid" in p.lower() or "json" in p.lower() for p in d.prompt_patches)
    # brief is augmented with the build hint as a constraint
    assert "Build a todo app" in d.augmented_brief
    assert "SyntaxError" in d.augmented_brief
    assert d.augmented_brief != "Build a todo app"


def test_syntax_error_escalates_code_to_balanced():
    d = build_retry_directive(
        brief="x",
        build_hint="syntax error: unexpected token at line 12",
    )
    # syntax-class failure escalates the code stage a notch (cost-aware)
    assert d.forced_stage_tier.get("code") == "balanced"


# --------------------------------------------------------------------------
# Stub / entrypoint failure -> strong code tier
# --------------------------------------------------------------------------
def test_stub_failure_escalates_code_to_strong():
    d = build_retry_directive(
        brief="Build a dashboard",
        error="code generation failed: entrypoint shipped as stub",
        failed_stages=["code"],
    )
    assert d.forced_stage_tier.get("code") == "strong"
    assert "stub" in d.augmented_brief.lower() or "entrypoint" in d.augmented_brief.lower()
    assert d.rationale


def test_stub_failure_beats_syntax_tier():
    """When both stub and syntax signals present, code must be 'strong'."""
    d = build_retry_directive(
        brief="x",
        build_hint="SyntaxError in App.jsx",
        error="entrypoint stub: export default null",
    )
    assert d.forced_stage_tier.get("code") == "strong"


# --------------------------------------------------------------------------
# prior_backend -> avoid_backends + brief note
# --------------------------------------------------------------------------
def test_prior_backend_added_to_avoid_and_brief():
    d = build_retry_directive(
        brief="Build an API",
        error="build failed",
        prior_backend="kimi_cli",
    )
    assert d.avoid_backends == ["kimi_cli"]
    assert "kimi_cli" in d.augmented_brief
    assert "different" in d.augmented_brief.lower()


def test_prior_backend_whitespace_stripped():
    d = build_retry_directive(brief="b", error="x", prior_backend="  copilot_cli  ")
    assert d.avoid_backends == ["copilot_cli"]


# --------------------------------------------------------------------------
# Reviewer blockers -> signatures + labels in brief
# --------------------------------------------------------------------------
def test_blockers_produce_signatures():
    blockers = [
        {"category": "palette_schism", "severity": "blocker", "file": "src/App.jsx"},
        {"category": "placeholder_leak", "severity": "warning"},
    ]
    d = build_retry_directive(brief="Build a site", blockers=blockers)
    assert "reviewer:palette_schism:App.jsx" in d.signatures
    assert "reviewer:placeholder_leak" in d.signatures
    # blocker labels surface in the augmented brief
    assert "palette_schism" in d.augmented_brief
    assert "placeholder_leak" in d.augmented_brief


def test_signatures_for_blockers_helper():
    blockers = [
        {"category": "missing_mount", "file": "vite.config.js"},
        {"rule": "no_inline_secrets"},
        {"category": "missing_mount", "file": "vite.config.js"},  # dup
        "not-a-dict",
        {"nothing": "useful"},  # no category -> dropped
    ]
    sigs = signatures_for_blockers(blockers)
    assert sigs == ["reviewer:missing_mount:vite.config.js", "reviewer:no_inline_secrets"]


def test_signatures_for_blockers_empty():
    assert signatures_for_blockers([]) == []
    assert signatures_for_blockers([{"nothing": 1}]) == []


# --------------------------------------------------------------------------
# Environmental failures never escalate tier (cost-safe)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "err",
    [
        "rate limit exceeded (429)",
        "unauthorized: invalid api key",
        "request timed out after 180s",
    ],
)
def test_environmental_failures_do_not_escalate_tier(err):
    d = build_retry_directive(brief="x", error=err)
    # No tier forced for purely environmental issues — that would just burn
    # budget. They still produce advisory prompt patches though.
    assert d.forced_stage_tier == {}


def test_context_length_relies_on_patch_not_tier():
    d = build_retry_directive(brief="x", error="maximum context length exceeded")
    assert "code" not in d.forced_stage_tier
    assert d.prompt_patches  # summarize/larger-context advice


# --------------------------------------------------------------------------
# Idempotency & purity
# --------------------------------------------------------------------------
def test_idempotent():
    kwargs = dict(
        brief="Build a thing",
        build_hint="SyntaxError x",
        prior_backend="kimi_cli",
        failed_stages=["code"],
        blockers=[{"category": "palette_schism", "severity": "blocker"}],
    )
    a = build_retry_directive(**kwargs)
    b = build_retry_directive(**kwargs)
    assert a == b


def test_does_not_mutate_shared_default_patterns():
    """build_retry_directive must not bump occurrence_count on the shared
    FailurePatternAnalyzer.DEFAULT_PATTERNS singletons (would poison the
    global failure stats and break idempotency across calls)."""
    before = [p.occurrence_count for p in FailurePatternAnalyzer.DEFAULT_PATTERNS]
    build_retry_directive(
        brief="x",
        error="rate limit 429 throttled timeout",
        build_hint="SyntaxError unexpected token",
    )
    after = [p.occurrence_count for p in FailurePatternAnalyzer.DEFAULT_PATTERNS]
    assert before == after


def test_does_not_mutate_input_brief():
    brief = "Build a thing"
    build_retry_directive(brief=brief, error="boom")
    assert brief == "Build a thing"


# --------------------------------------------------------------------------
# Flag gate
# --------------------------------------------------------------------------
def test_flag_default_on(monkeypatch):
    monkeypatch.delenv("SKYN3T_REFLECTIVE_RETRY", raising=False)
    assert reflective_retry_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE", "Off", ""])
def test_flag_off_values(monkeypatch, val):
    monkeypatch.setenv("SKYN3T_REFLECTIVE_RETRY", val)
    assert reflective_retry_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "on", "yes"])
def test_flag_on_values(monkeypatch, val):
    monkeypatch.setenv("SKYN3T_REFLECTIVE_RETRY", val)
    assert reflective_retry_enabled() is True


# --------------------------------------------------------------------------
# Combined realistic retry scenario
# --------------------------------------------------------------------------
def test_realistic_full_directive():
    d = build_retry_directive(
        brief="Build a self-hosted bookmark manager with a React frontend",
        error="code generation failed",
        build_hint="missing export 'App' in src/App.jsx",
        blockers=[{"category": "missing_mount", "severity": "blocker", "file": "main.jsx"}],
        prior_backend="kimi_cli",
        failed_stages=["code", "verify"],
    )
    # signatures from build hint + blocker
    assert any("missing_export" in s or "missing_mount" in s for s in d.signatures)
    # avoid prior backend
    assert "kimi_cli" in d.avoid_backends
    # augmented brief carries the original
    assert "bookmark manager" in d.augmented_brief
    # failed stages mentioned
    assert "code" in d.augmented_brief and "verify" in d.augmented_brief
    # rationale non-empty
    assert d.rationale
