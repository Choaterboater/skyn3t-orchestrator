from __future__ import annotations

from skyn3t.intelligence.domain_corpus import assess_github_learning_source


def test_public_permissive_github_repo_is_allowed_read_only():
    decision = assess_github_learning_source(
        "https://github.com/kenn-io/agentsview.git",
        public=True,
        license_spdx="MIT",
    )

    assert decision.allowed is True
    assert decision.repo == "kenn-io/agentsview"
    assert decision.read_only_original is True
    assert decision.candidate_strategy == "local_candidate_copy"
    assert decision.redaction_required is True
    assert decision.license_status == "permissive"


def test_unknown_license_requires_approval():
    decision = assess_github_learning_source(
        "https://github.com/example/project",
        public=True,
        license_spdx="",
    )

    assert decision.allowed is False
    assert decision.license_status == "unknown"
    assert any("license unknown" in reason for reason in decision.reasons)


def test_approved_unknown_license_is_pattern_only_safe():
    decision = assess_github_learning_source(
        "https://github.com/example/project",
        public=True,
        approved=True,
    )

    assert decision.allowed is True
    assert decision.read_only_original is True
    assert any("read-only" in reason for reason in decision.reasons)


def test_private_repo_requires_explicit_approval():
    decision = assess_github_learning_source(
        "git@github.com:example/private-tool.git",
        public=False,
        license_spdx="MIT",
    )

    assert decision.allowed is False
    assert any("explicit approval" in reason for reason in decision.reasons)
