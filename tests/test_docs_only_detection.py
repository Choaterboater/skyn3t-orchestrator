"""Tests for the planner's code-stage forcing + the runner's docs-only failure
detection. Together these ensure that "make this better" or "fix the dashboard"
briefs don't quietly produce a stack of markdown files."""

from __future__ import annotations

import pytest

from skyn3t.studio.planner import _should_force_code_agent
from skyn3t.studio.runner import StudioRunner

# ---------------------------------------------------------------------------
# Planner: which briefs force a code stage?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("brief", [
    "build a small todo app",
    "create an API for X",
    "fix the dashboard layout",
    "redesign the UI",
    "improve the orchestrator",
    "add a settings page",
    "refactor the planner",
    "wire up auth",
    "hook the webhook to slack",
    "build me a tic-tac-toe game",
])
def test_build_verbs_force_code_stage(brief):
    assert _should_force_code_agent(brief) is True, f"expected force-code for: {brief}"


@pytest.mark.parametrize("brief", [
    "make this better",  # truly ambiguous — could be docs or code
    "improve it",         # no object → can't classify
])
def test_truly_ambiguous_briefs_do_not_force_code_stage(brief):
    """Genuinely ambiguous briefs are left to the LLM planner. We don't
    force a code stage from regex alone — that would create false positives
    for marketing/copy briefs."""
    assert _should_force_code_agent(brief) is False


@pytest.mark.parametrize("brief", [
    "",  # empty
    "   ",  # whitespace only
    "write a readme for this project",
    "draft a spec for the new feature",
    "produce a research summary of LLM agents",
    "prepare a blog post about the launch",
    "compose an email to the team",
])
def test_docs_intents_dont_force_code_stage(brief):
    assert _should_force_code_agent(brief) is False, f"expected NO code-force for: {brief}"


def test_target_file_disables_force_code_stage():
    # If the user names a specific file (CodeImproverAgent territory), we
    # don't want to add a generic CodeAgent stage on top.
    assert _should_force_code_agent("fix skyn3t/web/app.py:1234") is False


# ---------------------------------------------------------------------------
# Runner: docs-only-for-code-brief detection
# ---------------------------------------------------------------------------


def test_docs_only_detects_md_only_output_when_brief_was_code():
    artifacts = ["brainstorm.md", "spec.md", "review.md"]
    assert StudioRunner._is_docs_only_for_code_brief("build a todo app", artifacts) is True


def test_docs_only_negative_when_code_artifacts_present():
    artifacts = ["brainstorm.md", "scaffold/app.py", "review.md"]
    assert StudioRunner._is_docs_only_for_code_brief("build a todo app", artifacts) is False


def test_docs_only_negative_for_html_output():
    artifacts = ["spec.md", "scaffold/index.html", "scaffold/style.css"]
    assert StudioRunner._is_docs_only_for_code_brief("redesign the UI", artifacts) is False


def test_docs_only_negative_for_dockerfile():
    # Files without an extension that are obviously infra-as-code shouldn't
    # be classified as docs-only.
    artifacts = ["spec.md", "Dockerfile"]
    assert StudioRunner._is_docs_only_for_code_brief("ship the service", artifacts) is False


def test_docs_only_negative_when_brief_is_docs_intent():
    artifacts = ["brief.md", "research.md"]
    assert StudioRunner._is_docs_only_for_code_brief("write a research summary", artifacts) is False


def test_docs_only_negative_when_no_artifacts():
    # No artifacts at all → different failure path; don't double-flag.
    assert StudioRunner._is_docs_only_for_code_brief("build a todo app", []) is False


def test_docs_only_yaml_counts_as_code():
    # A docker-compose.yml is real infra deliverable, not docs.
    artifacts = ["docker-compose.yml", "spec.md"]
    assert StudioRunner._is_docs_only_for_code_brief("build the deploy", artifacts) is False
