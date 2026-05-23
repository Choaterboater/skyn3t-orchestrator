import pytest

from skyn3t.studio.planner import _should_force_code_agent, plan_pipeline


class StubPlannerLLM:
    def __init__(self, response: str):
        self.response = response

    async def complete(self, *args, **kwargs) -> str:
        return self.response


@pytest.mark.asyncio
async def test_plan_pipeline_injects_code_agent_for_app_builds():
    llm = StubPlannerLLM(
        '{"agents":["ArchitectAgent","WriterAgent"],'
        '"expected_artifacts":["architecture.md","readme.md"],'
        '"rationale":{"ArchitectAgent":"Needs a technical plan.","WriterAgent":"Add docs."}}'
    )

    stages = await plan_pipeline(brief="Build a small todo app", llm_client=llm)

    assert [stage.agent for stage in stages] == [
        "BrainstormAgent",
        "ArchitectAgent",
        "CodeAgent",
        "WriterAgent",
        "ReviewerAgent",
    ]
    assert stages[2].rationale == (
        "brief asks SkyN3t to build working software, so the plan must include code output"
    )


@pytest.mark.asyncio
async def test_plan_pipeline_keeps_spec_only_briefs_doc_focused():
    llm = StubPlannerLLM(
        '{"agents":["ArchitectAgent","WriterAgent"],'
        '"expected_artifacts":["architecture.md","spec.md"],'
        '"rationale":{"ArchitectAgent":"Capture system boundaries.","WriterAgent":"Write the spec."}}'
    )

    stages = await plan_pipeline(brief="Write a product spec for a todo app", llm_client=llm)

    assert "CodeAgent" not in [stage.agent for stage in stages]


@pytest.mark.asyncio
async def test_plan_pipeline_prefers_code_improver_for_target_files():
    llm = StubPlannerLLM(
        '{"agents":["ArchitectAgent"],'
        '"expected_artifacts":["architecture.md"],'
        '"rationale":{"ArchitectAgent":"Quick implementation plan."}}'
    )

    stages = await plan_pipeline(
        brief="Fix the login bug in target_file: src/app.tsx",
        llm_client=llm,
    )

    agents = [stage.agent for stage in stages]
    assert "CodeImproverAgent" in agents
    assert "CodeAgent" not in agents


@pytest.mark.asyncio
async def test_plan_pipeline_does_not_inject_code_for_nonsoftware_build_language():
    llm = StubPlannerLLM(
        '{"agents":["MarketerAgent","WriterAgent"],'
        '"expected_artifacts":["positioning.md","launch_checklist.md"],'
        '"rationale":{"MarketerAgent":"Own the campaign.","WriterAgent":"Write the launch copy."}}'
    )

    stages = await plan_pipeline(
        brief="Build a launch campaign for a new AI code reviewer",
        llm_client=llm,
    )

    assert "CodeAgent" not in [stage.agent for stage in stages]


@pytest.mark.asyncio
async def test_plan_pipeline_software_build_includes_full_pipeline():
    """Every code-build brief should run the full pipeline including
    DesignerAgent, regardless of whether the brief mentions visual cues.

    Previous behavior required explicit "design"/"ui"/"dashboard" words
    to include Designer — that caused builds like "todo app" to skip
    brand.md generation entirely, leaving the resulting scaffold with
    hollow palettes and no design tokens. Every UI-bearing build now
    gets Architect + Designer by default."""
    stages = await plan_pipeline(brief="Build a small todo app", llm_client=None)

    assert [stage.agent for stage in stages] == [
        "BrainstormAgent",
        "ArchitectAgent",
        "DesignerAgent",
        "CodeAgent",
        "ReviewerAgent",
    ]


@pytest.mark.asyncio
async def test_plan_pipeline_heuristics_do_not_treat_generic_build_campaign_as_software():
    stages = await plan_pipeline(
        brief="Build a launch campaign for a new AI code reviewer",
        llm_client=None,
    )

    assert "ArchitectAgent" not in [stage.agent for stage in stages]
    assert "CodeAgent" not in [stage.agent for stage in stages]


@pytest.mark.asyncio
async def test_plan_pipeline_injects_code_when_brief_contains_build_plus_log_path():
    llm = StubPlannerLLM(
        '{"agents":["ResearchAgent","ArchitectAgent"],'
        '"expected_artifacts":["research.md","architecture.md"],'
        '"rationale":{"ResearchAgent":"Need API docs.","ArchitectAgent":"Plan integration shape."}}'
    )

    stages = await plan_pipeline(
        brief=(
            "Build a homelab dashboard with Vite + React and real service adapters.\n"
            "Missing npm dependency: <unknown>. Add it to server/package.json dependencies and re-install."
        ),
        llm_client=llm,
    )

    assert "CodeAgent" in [stage.agent for stage in stages]


def test_should_force_code_agent_handles_build_plus_incidental_file_path():
    brief = (
        "Build a homelab dashboard with Vite + React.\n"
        "Missing npm dependency: <unknown>. Add it to server/package.json dependencies."
    )
    assert _should_force_code_agent(brief) is True


def test_should_force_code_agent_stays_false_for_path_patch_without_build_phrase():
    assert _should_force_code_agent("Fix src/api/client.ts timeout handling.") is False


@pytest.mark.asyncio
async def test_plan_pipeline_keeps_designer_for_dashboard_build_even_if_style_is_specified():
    llm = StubPlannerLLM(
        '{"agents":["ArchitectAgent","DesignerAgent","CodeAgent"],'
        '"expected_artifacts":["architecture.md","brand.md","scaffold/"],'
        '"rationale":{"ArchitectAgent":"Plan system.","DesignerAgent":"Define style.","CodeAgent":"Build app."}}'
    )

    stages = await plan_pipeline(
        brief=(
            "Build a homelab dashboard with Tailwind dark theme inspired by Homarr, "
            "including polished UI and responsive pages."
        ),
        llm_client=llm,
    )

    assert "DesignerAgent" in [stage.agent for stage in stages]


@pytest.mark.asyncio
async def test_plan_pipeline_still_skips_designer_for_non_software_style_only_brief():
    llm = StubPlannerLLM(
        '{"agents":["DesignerAgent","WriterAgent"],'
        '"expected_artifacts":["brand.md","readme.md"],'
        '"rationale":{"DesignerAgent":"Style direction.","WriterAgent":"Document it."}}'
    )

    stages = await plan_pipeline(
        brief="Create brand guidelines with Tailwind dark theme inspired by Homarr.",
        llm_client=llm,
    )

    assert "DesignerAgent" not in [stage.agent for stage in stages]


@pytest.mark.asyncio
async def test_plan_pipeline_error_hint_with_file_path_does_not_trigger_improver():
    """Build brief with error message containing a file path should NOT trigger CodeImprover."""
    llm = StubPlannerLLM(
        '{"agents":["ArchitectAgent","CodeAgent"],'
        '"expected_artifacts":["architecture.md","src/app.tsx"],'
        '"rationale":{"ArchitectAgent":"Plan the fix.","CodeAgent":"Implement the fix."}}'
    )

    # Real build brief with an error hint containing an incidental file path
    brief = (
        "Build a homelab dashboard with React and Vite. "
        "Error: missing import in server/adapters/sonos.js. "
        "Fix the import path."
    )
    stages = await plan_pipeline(brief=brief, llm_client=llm)
    agents = [stage.agent for stage in stages]

    # CodeAgent should be included for the real build
    assert "CodeAgent" in agents
    # CodeImproverAgent should NOT be included (error hint doesn't count as patch directive)
    assert "CodeImproverAgent" not in agents


@pytest.mark.asyncio
async def test_plan_pipeline_injects_research_for_third_party_integrations():
    """Integration-heavy briefs should include ResearchAgent even if LLM planner skips it."""
    llm = StubPlannerLLM(
        '{"agents":["CodeAgent"],'
        '"expected_artifacts":["src/app.tsx"],'
        '"rationale":{"CodeAgent":"Build the app."}}'
    )

    # Brief with third-party API integrations
    brief = "Build a home automation dashboard with Sonarr and Radarr integration"
    stages = await plan_pipeline(brief=brief, llm_client=llm)
    agents = [stage.agent for stage in stages]

    # ResearchAgent should be injected for integration keywords even if LLM didn't include it
    assert "ResearchAgent" in agents
    # CodeAgent should also be there
    assert "CodeAgent" in agents


@pytest.mark.asyncio
async def test_plan_pipeline_skips_marketer_for_mission_setup_audience_only():
    brief = (
        "Build a habit tracker\n\n"
        "## Mission setup\n"
        "- Primary audience: General users\n"
        "- Operating mode: confirm first"
    )
    stages = await plan_pipeline(brief=brief, llm_client=None)
    agents = [stage.agent for stage in stages]

    assert "CodeAgent" in agents
    assert "MarketerAgent" not in agents


@pytest.mark.asyncio
async def test_plan_pipeline_keeps_marketer_for_explicit_gtm_brief():
    brief = "Build a SaaS app and write the go-to-market launch plan"
    stages = await plan_pipeline(brief=brief, llm_client=None)
    agents = [stage.agent for stage in stages]

    assert "MarketerAgent" in agents
