from skyn3t.registry.catalog import build_agent_override, get_agent_catalog_metadata


def test_catalog_supplies_verifier_defaults_without_user_overrides():
    merged = build_agent_override(class_name="VerifierAgent", runtime_name="verifier")

    assert merged == {"backend": "claude_cli", "model": "sonnet"}


def test_catalog_keeps_explicit_user_overrides_on_top():
    merged = build_agent_override(
        class_name="WriterAgent",
        runtime_name="writer",
        name_patch={"backend": "kimi_cli", "model": "kimi-code/kimi-for-coding"},
    )

    assert merged == {
        "backend": "kimi_cli",
        "model": "kimi-code/kimi-for-coding",
    }


def test_catalog_metadata_marks_internal_utility_agents():
    metadata = get_agent_catalog_metadata(class_name="CodeImproverAgent", runtime_name="code_improver")

    assert metadata["tier"] == "internal"
    assert metadata["label"] == "Code improver"
    assert metadata["recommended_backend"] == "copilot_cli"
