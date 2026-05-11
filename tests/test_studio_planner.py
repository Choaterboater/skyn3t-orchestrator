import pytest

from skyn3t.studio.planner import plan_pipeline


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
async def test_plan_pipeline_heuristics_do_not_treat_build_as_ui():
    stages = await plan_pipeline(brief="Build a small todo app", llm_client=None)

    assert [stage.agent for stage in stages] == [
        "BrainstormAgent",
        "ArchitectAgent",
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
