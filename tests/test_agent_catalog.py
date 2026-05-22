from skyn3t.registry.catalog import build_agent_override, get_agent_catalog_metadata


def test_catalog_does_not_force_runtime_defaults_without_user_overrides():
    merged = build_agent_override(class_name="VerifierAgent", runtime_name="verifier")

    assert merged == {}


def test_catalog_leaves_stage_agents_unpinned_without_user_overrides():
    merged = build_agent_override(class_name="ArchitectAgent", runtime_name="architect")

    assert merged == {}


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
    # recommended_backend was intentionally nulled for routed agents so
    # skyn3t.core.model_router picks the tier per-stage (and per-file
    # inside CodeAgent) instead of being short-circuited by a catalog
    # default. Confirm it's no longer hard-pinned to copilot_cli.
    assert metadata.get("recommended_backend") in (None, "")
