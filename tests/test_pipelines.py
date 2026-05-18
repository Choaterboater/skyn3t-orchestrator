"""Tests for pipeline system."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.pipeline import (
    CollaborativePipeline,
    Pipeline,
    PipelineStage,
    create_pipeline,
)


class MockAgent(BaseAgent):
    """Mock agent for pipeline testing."""

    def __init__(self, name: str, event_bus: EventBus, response: str = "ok"):
        super().__init__(
            name=name,
            agent_type="mock",
            provider="test",
            event_bus=event_bus,
        )
        self.response = response
        self.execute_call_count = 0

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def execute(self, task: TaskRequest) -> TaskResult:
        self.execute_call_count += 1
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"response": f"{self.response} #{self.execute_call_count}"},
        )

    async def health_check(self) -> bool:
        return True


class FailingAgent(BaseAgent):
    """Agent that always fails."""

    def __init__(self, name: str, event_bus: EventBus):
        super().__init__(
            name=name,
            agent_type="mock",
            provider="test",
            event_bus=event_bus,
        )

    async def initialize(self) -> None:
        pass

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            success=False,
            error="Simulated failure",
        )

    async def health_check(self) -> bool:
        return True


@pytest.mark.asyncio
class TestCreatePipeline:
    async def test_create_pipeline_with_mocked_agents(self, event_bus):
        agent1 = MockAgent("a1", event_bus, "step1")
        agent2 = MockAgent("a2", event_bus, "step2")
        agent3 = MockAgent("a3", event_bus, "step3")

        pipeline = create_pipeline(
            name="test_pipeline",
            agents=[agent1, agent2, agent3],
            event_bus=event_bus,
            stage_names=["write", "review", "test"],
        )

        assert pipeline.name == "test_pipeline"
        assert len(pipeline.stages) == 3
        assert pipeline.stages[0].name == "write"
        assert pipeline.stages[1].name == "review"
        assert pipeline.stages[2].name == "test"

    async def test_create_pipeline_default_stage_names(self, event_bus):
        agent1 = MockAgent("a1", event_bus)
        pipeline = create_pipeline(
            name="simple",
            agents=[agent1],
            event_bus=event_bus,
        )
        assert pipeline.stages[0].name == "stage_1"


@pytest.mark.asyncio
class TestPipeOutputForwarding:
    async def test_output_forwarded_between_stages(self, event_bus):
        agent1 = MockAgent("writer", event_bus, "generated_code")
        agent2 = MockAgent("reviewer", event_bus, "reviewed_code")

        pipeline = Pipeline(
            name="forward_test",
            stages=[
                PipelineStage(name="write", agent=agent1),
                PipelineStage(name="review", agent=agent2),
            ],
            event_bus=event_bus,
        )

        result = await pipeline.run(initial_input={"message": "Write a function"})

        assert result.success is True
        assert len(result.stages) == 2
        # Second stage should have received output from first
        assert "generated_code #1" in result.stages[0]["output"]["response"]
        assert "reviewed_code #1" in result.stages[1]["output"]["response"]

    async def test_custom_output_transform(self, event_bus):
        agent1 = MockAgent("writer", event_bus, "code_result")
        agent2 = MockAgent("reviewer", event_bus, "review_result")

        def transform(result: TaskResult) -> dict:
            return {"code": result.output["response"]}

        pipeline = Pipeline(
            name="transform_test",
            stages=[
                PipelineStage(
                    name="write", agent=agent1, output_transform=transform
                ),
                PipelineStage(name="review", agent=agent2),
            ],
            event_bus=event_bus,
        )

        result = await pipeline.run(initial_input={"message": "go"})
        assert result.success is True
        # Second stage input should contain "code" key from transform
        assert result.stages[1]["output"]["response"] == "review_result #1"


@pytest.mark.asyncio
class TestPipelineCompletionEvent:
    async def test_completion_event_published(self, event_bus):
        agent1 = MockAgent("a1", event_bus)
        events = []

        def handler(event):
            events.append(event)

        event_bus.subscribe(handler, EventType.PIPELINE_COMPLETED)

        pipeline = Pipeline(
            name="event_test",
            stages=[PipelineStage(name="step1", agent=agent1)],
            event_bus=event_bus,
        )

        result = await pipeline.run(initial_input={})

        assert result.success is True
        assert pipeline.is_completed is True
        completion_events = [e for e in events if "pipeline_id" in e.payload]
        assert len(completion_events) >= 1
        assert completion_events[-1].payload["stages_completed"] == 1

    async def test_wait_for_completion(self, event_bus):
        agent1 = MockAgent("a1", event_bus)

        pipeline = Pipeline(
            name="wait_test",
            stages=[PipelineStage(name="step1", agent=agent1)],
            event_bus=event_bus,
        )

        asyncio.create_task(pipeline.run(initial_input={}))
        completed = await pipeline.wait_for_completion(timeout=2.0)
        assert completed is True


@pytest.mark.asyncio
class TestCollaborativeRun:
    async def test_collaborative_pipeline(self, event_bus):
        agent1 = MockAgent("claude", event_bus, "Claude says")
        agent2 = MockAgent("kimi", event_bus, "Kimi says")
        agent3 = MockAgent("copilot", event_bus, "Copilot says")

        pipeline = CollaborativePipeline(
            name="collab_test",
            stages=[
                PipelineStage(name="step1", agent=agent1),
                PipelineStage(name="step2", agent=agent2),
                PipelineStage(name="step3", agent=agent3),
            ],
            event_bus=event_bus,
        )

        result = await pipeline.run(
            initial_input={"message": "How to build a startup"}
        )

        assert result.success is True
        assert len(result.stages) == 3
        # Each stage should have history
        for stage in result.stages:
            assert stage["success"] is True

    async def test_collaborative_pipeline_failure(self, event_bus):
        agent1 = MockAgent("ok", event_bus, "ok")
        agent2 = FailingAgent("fail", event_bus)

        pipeline = CollaborativePipeline(
            name="fail_test",
            stages=[
                PipelineStage(name="step1", agent=agent1),
                PipelineStage(name="step2", agent=agent2),
            ],
            event_bus=event_bus,
        )

        result = await pipeline.run(initial_input={"message": "go"})

        assert result.success is False
        assert result.error == "Simulated failure"
        assert len(result.stages) == 2
        assert result.stages[0]["success"] is True
        assert result.stages[1]["success"] is False

    async def test_create_pipeline_collaborative(self, event_bus):
        agent1 = MockAgent("a1", event_bus)
        agent2 = MockAgent("a2", event_bus)

        pipeline = create_pipeline(
            name="collab",
            agents=[agent1, agent2],
            event_bus=event_bus,
            collaborative=True,
        )

        assert isinstance(pipeline, CollaborativePipeline)
        result = await pipeline.run(initial_input={"message": "hi"})
        assert result.success is True


@pytest.mark.asyncio
class TestStudioRunner:
    @staticmethod
    def _init_git_repo(path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init"],
            check=True,
            cwd=path,
            capture_output=True,
            text=True,
        )
        return path

    async def test_run_pipeline_completes_and_forwards_required_capability(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class FakeStudioAgent:
            def __init__(self):
                self.last_task = None

            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                self.last_task = task
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": []},
                )

        fake_agent = FakeStudioAgent()
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_agent",
            lambda *args, **kwargs: fake_agent,
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        artifact_dir = tmp_path / "demo"
        artifact_dir.mkdir()

        template = SimpleNamespace(
            title="Demo template",
            stages=[
                SimpleNamespace(
                    name="code",
                    agent="WriterAgent",
                    capability="copywriting",
                    handoff_to=None,
                    input_extra={},
                )
            ],
        )
        manifest = {"stages": [], "artifacts": [], "status": "running"}

        result = await runner._run_pipeline(
            template=template,
            template_key="demo",
            brief="Make a landing page",
            slug="demo",
            artifact_dir=artifact_dir,
            manifest=manifest,
            extra=None,
        )

        assert result["status"] == "done"
        assert fake_agent.last_task is not None
        assert fake_agent.last_task.input_data["required_capability"] == "copywriting"
        assert result["stages"][0]["task_id"] == fake_agent.last_task.task_id
        assert (artifact_dir / "project.json").exists()

    async def test_reserve_and_start_persist_workflow_summary_and_history(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class FakeStudioAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                from pathlib import Path

                out_dir = Path(task.input_data["artifact_dir"])
                (out_dir / "readme.md").write_text("# Demo\n", encoding="utf-8")
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": ["readme.md"], "summary": "Drafted the README."},
                )

        monkeypatch.setattr(
            "skyn3t.studio.runner.get_agent",
            lambda *args, **kwargs: FakeStudioAgent(),
        )

        template = SimpleNamespace(
            title="Demo template",
            description="Turn a brief into a README.",
            stages=[
                SimpleNamespace(
                    name="writer",
                    agent="WriterAgent",
                    capability="copywriting",
                    handoff_to=None,
                    input_extra={"kind": "readme"},
                )
            ],
        )
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: template,
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        reserved = runner.reserve_project("demo", "Make a landing page", slug="demo")

        assert reserved["slug"] == "demo"
        assert reserved["status"] == "queued"
        assert reserved["workflow_summary"]["stage_count"] == 1
        assert reserved["workflow_summary"]["expected_outputs"] == ["readme.md"]

        manifest = await runner.start("demo", "Make a landing page", slug="demo")
        events = [entry["event"] for entry in manifest["history"]]
        stage = manifest["stages"][0]

        assert manifest["status"] == "done"
        assert events[0] == "PROJECT_QUEUED"
        assert "PROJECT_STARTED" in events
        assert "PROJECT_STAGE_STARTED" in events
        assert "PROJECT_STAGE_COMPLETED" in events
        assert events[-1] == "PROJECT_COMPLETED"
        assert manifest["next_action"].startswith("Project finished")
        assert "readme.md" in manifest["artifacts"]
        assert stage["status"] == "done"
        assert stage["summary"] == "Drafted the README."
        assert stage["files"] == ["readme.md"]
        assert stage["capability"] == "copywriting"
        assert stage["expected_artifact"] == "readme.md"
        assert stage["started_at"] <= stage["completed_at"]

    async def test_run_pipeline_continues_when_critique_times_out(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class FakeStudioAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                out_dir = Path(task.input_data["artifact_dir"])
                (out_dir / "readme.md").write_text("# Demo\n", encoding="utf-8")
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": ["readme.md"], "summary": "Drafted the README."},
                )

        class TimeoutReviewer:
            async def initialize(self) -> None:
                return None

            async def critique(self, **kwargs):  # noqa: ARG002
                raise asyncio.TimeoutError

        def fake_get_agent(name, *args, **kwargs):  # noqa: ARG001
            if name == "ReviewerAgent":
                return TimeoutReviewer()
            return FakeStudioAgent()

        monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Demo template",
                description="Turn a brief into a README.",
                stages=[
                    SimpleNamespace(
                        name="writer",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start("demo", "Make a landing page", slug="critique-timeout")

        events = [entry["event"] for entry in manifest["history"]]
        stage = manifest["stages"][0]

        assert manifest["status"] == "done"
        assert stage["status"] == "done"
        assert stage["summary"] == "Drafted the README."
        assert "CRITIQUE_FAILED" in events
        critique_failure = next(
            entry for entry in manifest["history"] if entry["event"] == "CRITIQUE_FAILED"
        )
        assert "timed out" in critique_failure["message"]

    async def test_run_pipeline_continues_when_reviewer_llm_times_out(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.agents.reviewer import ReviewerAgent
        from skyn3t.studio.runner import StudioRunner

        class FakeStudioAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                out_dir = Path(task.input_data["artifact_dir"])
                (out_dir / "readme.md").write_text(
                    "# Demo\n\nA concise launch brief with enough detail to avoid stub heuristics.\n",
                    encoding="utf-8",
                )
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": ["readme.md"], "summary": "Drafted the README."},
                )

        class SlowLLMClient:
            async def complete(self, prompt, max_tokens, temperature):  # noqa: ARG002
                await asyncio.sleep(1)
                return "1. readme.md: this should never be returned"

        def fake_get_agent(name, *args, **kwargs):  # noqa: ARG001
            if name == "ReviewerAgent":
                return ReviewerAgent(event_bus=event_bus)
            return FakeStudioAgent()

        monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
        monkeypatch.setattr("skyn3t.adapters.LLMClient", lambda *args, **kwargs: SlowLLMClient())
        monkeypatch.setattr("skyn3t.agents.reviewer._LLM_CRITIQUE_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Demo template",
                description="Turn a brief into a README.",
                stages=[
                    SimpleNamespace(
                        name="writer",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start("demo", "Make a landing page", slug="critique-llm-timeout")

        stage = manifest["stages"][0]

        assert manifest["status"] == "done"
        assert stage["status"] == "done"
        assert stage["summary"] == "Drafted the README."
        assert "CRITIQUE_FAILED" not in [entry["event"] for entry in manifest["history"]]

    async def test_run_pipeline_continues_when_critique_window_times_out(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class FakeStudioAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                out_dir = Path(task.input_data["artifact_dir"])
                (out_dir / "readme.md").write_text(
                    "# Demo\n\nA concise launch brief with enough detail to avoid stub heuristics.\n",
                    encoding="utf-8",
                )
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": ["readme.md"], "summary": "Drafted the README."},
                )

        async def slow_critique(self, **kwargs):  # noqa: ARG001
            await asyncio.sleep(1)
            return kwargs["result"]

        monkeypatch.setattr("skyn3t.studio.runner.get_agent", lambda *args, **kwargs: FakeStudioAgent())
        monkeypatch.setattr(StudioRunner, "_critique_and_revise", slow_critique)
        monkeypatch.setattr(
            StudioRunner,
            "_critique_timeout_for",
            staticmethod(lambda stage_name, execution_profile="balanced": 0.01),
        )
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Demo template",
                description="Turn a brief into a README.",
                stages=[
                    SimpleNamespace(
                        name="writer",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start("demo", "Make a landing page", slug="critique-window-timeout")

        events = [entry["event"] for entry in manifest["history"]]
        stage = manifest["stages"][0]

        assert manifest["status"] == "done"
        assert stage["status"] == "done"
        assert stage["summary"] == "Drafted the README."
        assert "CRITIQUE_FAILED" in events
        critique_failure = next(
            entry for entry in manifest["history"] if entry["event"] == "CRITIQUE_FAILED"
        )
        assert "Critique window timed out" in critique_failure["message"]

    async def test_normalize_manifest_migrates_legacy_stage_records(
        self, event_bus, tmp_path
    ):
        from skyn3t.studio.runner import StudioRunner

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = runner._normalize_manifest(
            {
                "slug": "legacy-demo",
                "title": "Legacy demo",
                "template": "demo",
                "status": "done",
                "workflow_summary": {
                    "title": "Demo template",
                    "stage_count": 1,
                    "expected_outputs": ["readme.md"],
                    "stages": [
                        {
                            "name": "writer",
                            "agent": "WriterAgent",
                            "capability": "copywriting",
                            "expected_artifact": "readme.md",
                            "handoff_to": "ReviewerAgent",
                            "rationale": "Draft the first output.",
                        }
                    ],
                },
                "stages": [
                    {
                        "name": "writer",
                        "agent": "WriterAgent",
                        "ok": True,
                        "output": {
                            "files": ["readme.md"],
                            "summary": "Drafted the README.",
                        },
                    }
                ],
            }
        )

        stage = manifest["stages"][0]

        assert stage["status"] == "done"
        assert stage["summary"] == "Drafted the README."
        assert stage["files"] == ["readme.md"]
        assert stage["capability"] == "copywriting"
        assert stage["expected_artifact"] == "readme.md"
        assert stage["handoff_to"] == "ReviewerAgent"
        assert "output" not in stage

    async def test_run_pipeline_tracks_waiting_stage_state(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class ClarifyingAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                clarify_path = Path(task.input_data["artifact_dir"]) / "_clarifications.json"
                clarify_path.write_text(
                    json.dumps({"questions": ["Who is the audience?"]}),
                    encoding="utf-8",
                )
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={
                        "needs_clarification": True,
                        "questions": ["Who is the audience?"],
                        "files": [str(clarify_path)],
                        "summary": "Need audience guidance before writing the draft.",
                    },
                )

        monkeypatch.setattr(
            "skyn3t.studio.runner.get_agent",
            lambda *args, **kwargs: ClarifyingAgent(),
        )

        template = SimpleNamespace(
            title="Demo template",
            description="Draft, then review.",
            stages=[
                SimpleNamespace(
                    name="writer",
                    agent="WriterAgent",
                    capability="copywriting",
                    handoff_to="ReviewerAgent",
                    input_extra={"kind": "draft"},
                )
            ],
        )
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: template,
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start("demo", "Make a landing page", slug="demo")
        stage = manifest["stages"][0]

        assert manifest["status"] == "awaiting_clarification"
        assert manifest["current_stage"] == "writer"
        assert manifest["current_agent"] == "WriterAgent"
        assert manifest["next_action"].startswith("Answer 1 clarification question")
        assert stage["status"] == "waiting"
        assert stage["question_count"] == 1
        assert stage["summary"] == "Need audience guidance before writing the draft."
        assert stage["next_action"].startswith("Answer 1 clarification question")
        assert stage["files"] == ["_clarifications.json"]
        assert stage["handoff_to"] == "ReviewerAgent"
        saved_manifest = json.loads((tmp_path / "demo" / "project.json").read_text())
        assert saved_manifest["stages"][0]["files"] == ["_clarifications.json"]

    async def test_run_pipeline_tags_nested_agent_events_with_project_context(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.adapters.llm_client import LLMClient
        from skyn3t.studio.runner import StudioRunner

        class FakeLLMBackend:
            async def complete(self, _req) -> str:
                return "Structured draft ready."

        async def fake_get_impl(self):
            return FakeLLMBackend()

        monkeypatch.setattr(LLMClient, "_get_impl", fake_get_impl)

        class ContextAwareAgent(BaseAgent):
            def __init__(self):
                super().__init__(
                    name="WriterAgent",
                    agent_type="writer",
                    provider="test",
                    event_bus=event_bus,
                )

            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                await self.think("Scoping the project brief.")
                await self.share_learning("Audience and tone should stay aligned.", scope="project")
                await self.send_message(
                    "ReviewerAgent",
                    "Draft is ready for review.",
                    payload={"handoff": "review"},
                )
                summary = await self.llm.complete(
                    "Draft the launch brief.",
                    system="Keep it concise and business-friendly.",
                )
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": [], "summary": summary},
                )

            async def health_check(self) -> bool:
                return True

        monkeypatch.setattr(
            "skyn3t.studio.runner.get_agent",
            lambda *args, **kwargs: ContextAwareAgent(),
        )
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Demo template",
                description="Draft and review a launch brief.",
                stages=[
                    SimpleNamespace(
                        name="writer",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to="ReviewerAgent",
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start("demo", "Draft a launch brief", slug="demo")
        stage = manifest["stages"][0]

        thought = event_bus.get_history(EventType.AGENT_THOUGHT)[0]
        learning = event_bus.get_history(EventType.AGENT_LEARNING)[0]
        message = event_bus.get_history(EventType.AGENT_MESSAGE_SENT)[0]
        exchange = event_bus.get_history(EventType.LLM_EXCHANGE)[0]

        for event in (thought, learning, exchange):
            assert event.payload["project_slug"] == "demo"
            assert event.payload["project_stage"] == "writer"
            assert event.payload["project_template"] == "demo"
            assert event.payload["task_id"] == stage["task_id"]
            assert event.correlation_id == stage["task_id"]

        assert message.payload["payload"]["project_slug"] == "demo"
        assert message.payload["payload"]["project_stage"] == "writer"
        assert message.payload["payload"]["project_template"] == "demo"
        assert message.payload["payload"]["task_id"] == stage["task_id"]
        assert message.correlation_id == stage["task_id"]
        assert exchange.payload["agent"] == "WriterAgent"

    async def test_start_persists_mission_setup_and_stage_hints(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class FakeStudioAgent:
            def __init__(self):
                self.last_task = None

            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                self.last_task = task
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": [], "summary": "Draft ready."},
                )

        fake_agent = FakeStudioAgent()
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_agent",
            lambda *args, **kwargs: fake_agent,
        )
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Demo template",
                description="Draft a launch brief.",
                stages=[
                    SimpleNamespace(
                        name="code",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        repo_root = self._init_git_repo(tmp_path / "customer-portal")
        (repo_root / "src").mkdir()
        (repo_root / "src" / "login.tsx").write_text("export const Login = () => null;\n")

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start(
            "demo",
            "Draft a launch brief",
            slug="demo",
            mission_setup={"audience": "builders", "autonomy": "move_fast"},
            repo_target={
                "local_path": str(repo_root),
                "focus_file": "src/login.tsx",
            },
        )

        assert manifest["mission_setup"] == {
            "audience": "builders",
            "autonomy": "move_fast",
        }
        assert manifest["repo_target"] == {
            "local_path": repo_root.resolve().as_posix(),
            "focus_file": "src/login.tsx",
        }
        assert fake_agent.last_task is not None
        assert fake_agent.last_task.input_data["mission_setup"] == {
            "audience": "builders",
            "autonomy": "move_fast",
        }
        assert fake_agent.last_task.input_data["audience"] == "Builders / developers"
        assert fake_agent.last_task.input_data["clarifications"] is True
        assert fake_agent.last_task.input_data["repo_root"] == repo_root.resolve().as_posix()
        assert fake_agent.last_task.input_data["repo_label"] == "customer-portal"
        assert fake_agent.last_task.input_data["target_file"] == "src/login.tsx"
        assert fake_agent.last_task.input_data["repo_target"] == {
            "local_path": repo_root.resolve().as_posix(),
            "focus_file": "src/login.tsx",
        }
        assert "## Mission setup" in fake_agent.last_task.input_data["brief"]
        assert "## Codebase target" in fake_agent.last_task.input_data["brief"]
        assert "Primary audience: Builders / developers" in fake_agent.last_task.input_data["brief"]

    async def test_start_defaults_to_move_fast_without_explicit_mission_setup(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class FakeStudioAgent:
            def __init__(self):
                self.last_task = None

            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                self.last_task = task
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": [], "summary": "Draft ready."},
                )

        fake_agent = FakeStudioAgent()
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_agent",
            lambda *args, **kwargs: fake_agent,
        )
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Demo template",
                description="Draft a launch brief.",
                stages=[
                    SimpleNamespace(
                        name="code",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start("demo", "Draft a launch brief", slug="demo-defaults")

        assert manifest["mission_setup"] == {"audience": "", "autonomy": "move_fast"}
        assert fake_agent.last_task is not None
        assert fake_agent.last_task.input_data["mission_setup"] == {
            "audience": "",
            "autonomy": "move_fast",
        }
        assert fake_agent.last_task.input_data["clarifications"] is True
        assert "require_clarification" not in fake_agent.last_task.input_data

    async def test_reserve_project_rejects_focus_file_without_repo_path(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Demo template",
                description="Fix code in an existing project.",
                stages=[],
            ),
        )
        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)

        with pytest.raises(ValueError, match="focus file requires a repo path"):
            runner.reserve_project(
                "demo",
                "Fix the login flow",
                repo_target={"local_path": "", "focus_file": "src/login.tsx"},
            )

    async def test_start_marks_manifest_failed_when_planning_crashes(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Auto template",
                description="Auto plan the run.",
                stages=[],
            ),
        )

        async def fail_plan_pipeline(*args, **kwargs):
            raise RuntimeError("planner offline")

        monkeypatch.setattr(
            "skyn3t.studio.planner.plan_pipeline",
            fail_plan_pipeline,
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        runner.reserve_project("auto", "Plan a rollout", slug="demo-fail")

        with pytest.raises(RuntimeError, match="planner offline"):
            await runner.start("auto", "Plan a rollout", slug="demo-fail")

        manifest = runner.get_project("demo-fail")
        assert manifest is not None
        assert manifest["status"] == "failed"
        assert "planner offline" in manifest["error"]
        assert manifest["next_action"] == "Project stopped before the swarm could finish starting."
        assert manifest["completed_at"] is not None
        assert manifest["history"][-1]["event"] == "PROJECT_FAILED"

    async def test_resume_marks_manifest_failed_when_replanning_crashes(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Auto template",
                description="Auto plan the run.",
                stages=[],
            ),
        )
        manifest = runner.reserve_project("auto", "Ship the dashboard refresh", slug="demo-resume")
        manifest["status"] = "awaiting_clarification"
        manifest["clarification"] = {"questions": ["Which audience matters most?"]}
        runner._save_manifest(tmp_path / "demo-resume", manifest)

        def fail_template(_key):
            raise RuntimeError("planner reload failed")

        monkeypatch.setattr("skyn3t.studio.runner.get_template", fail_template)

        with pytest.raises(RuntimeError, match="planner reload failed"):
            await runner.resume("demo-resume", ["Operations teams."])

        resumed = runner.get_project("demo-resume")
        assert resumed is not None
        assert resumed["status"] == "failed"
        assert "planner reload failed" in resumed["error"]
        assert resumed["next_action"] == "Project stopped while applying clarification answers."
        assert resumed["completed_at"] is not None
        assert resumed["history"][-1]["event"] == "PROJECT_FAILED"

    async def test_resume_preserves_mission_setup_without_reasking(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class ClarifyThenCompleteAgent:
            def __init__(self):
                self.tasks = []

            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                self.tasks.append(task)
                if len(self.tasks) == 1:
                    return TaskResult(
                        task_id=task.task_id,
                        success=True,
                        output={
                            "needs_clarification": True,
                            "questions": ["Who is the buyer?"],
                            "files": ["brainstorm.md"],
                            "summary": "Need a quick confirmation pass before continuing.",
                        },
                    )
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": ["brief.md"], "summary": "Mission resumed and completed."},
                )

        fake_agent = ClarifyThenCompleteAgent()
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_agent",
            lambda *args, **kwargs: fake_agent,
        )
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Demo template",
                description="Draft, then review.",
                stages=[
                    SimpleNamespace(
                        name="writer",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        repo_root = self._init_git_repo(tmp_path / "customer-portal")
        (repo_root / "src").mkdir()
        (repo_root / "src" / "brief.md").write_text("# draft\n")

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start(
            "demo",
            "Build a launch brief",
            slug="demo",
            mission_setup={"audience": "leaders", "autonomy": "confirm_first"},
            repo_target={
                "local_path": str(repo_root),
                "focus_file": "src/brief.md",
            },
        )

        assert manifest["status"] == "awaiting_clarification"
        assert fake_agent.tasks[0].input_data["require_clarification"] is True
        assert fake_agent.tasks[0].input_data["audience"] == "Decision-makers"
        assert fake_agent.tasks[0].input_data["repo_root"] == repo_root.resolve().as_posix()
        assert fake_agent.tasks[0].input_data["target_file"] == "src/brief.md"

        resumed = await runner.resume("demo", ["Sales leaders evaluating budget impact."])

        assert resumed["status"] == "done"
        assert resumed["mission_setup"] == {
            "audience": "leaders",
            "autonomy": "confirm_first",
        }
        assert resumed["repo_target"] == {
            "local_path": repo_root.resolve().as_posix(),
            "focus_file": "src/brief.md",
        }
        assert fake_agent.tasks[1].input_data["clarifications"] is True
        assert "require_clarification" not in fake_agent.tasks[1].input_data
        assert fake_agent.tasks[1].input_data["audience"] == "Decision-makers"
        assert fake_agent.tasks[1].input_data["repo_root"] == repo_root.resolve().as_posix()
        assert fake_agent.tasks[1].input_data["repo_target"] == {
            "local_path": repo_root.resolve().as_posix(),
            "focus_file": "src/brief.md",
        }
        assert fake_agent.tasks[1].input_data["target_file"] == "src/brief.md"
        assert "## Mission setup" in fake_agent.tasks[1].input_data["brief"]
        assert "## Codebase target" in fake_agent.tasks[1].input_data["brief"]

    async def test_normalize_manifest_defaults_quality_summary_none(
        self, event_bus, tmp_path
    ):
        from skyn3t.studio.runner import StudioRunner

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = runner._normalize_manifest({"slug": "demo", "template": "demo"})

        assert manifest["quality_summary"] is None

    async def test_reviewer_quality_summary_persists_relative_review_file(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class ReviewerStageAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                from pathlib import Path

                review_path = Path(task.input_data["artifact_dir"]).resolve() / "review.md"
                review_path.write_text("# Review\n", encoding="utf-8")
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={
                        "verdict": "go-with-fixes",
                        "score": 78,
                        "summary": "Reviewer found a few issues before launch.",
                        "files": [str(review_path)],
                    },
                )

        monkeypatch.setattr(
            "skyn3t.studio.runner.get_agent",
            lambda *args, **kwargs: ReviewerStageAgent(),
        )
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Review template",
                description="Run a final review.",
                stages=[
                    SimpleNamespace(
                        name="reviewer",
                        agent="ReviewerAgent",
                        capability="review",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start("demo", "Review this project", slug="quality-demo")

        assert manifest["status"] == "needs_fixes"
        assert manifest["next_action"] == "Reviewer found a few issues before launch."
        assert manifest["quality_summary"] == {
            "source": "reviewer",
            "verdict": "go-with-fixes",
            "raw_verdict": "go-with-fixes",
            "score": 78,
            "summary": "Reviewer found a few issues before launch.",
            "review_file": "review.md",
            "updated_at": manifest["quality_summary"]["updated_at"],
        }

    async def test_reviewer_quality_summary_outranks_verifier(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class VerifierStageAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={
                        "verdict": "yes",
                        "score": 92,
                        "summary": "Verifier saw strong brief coverage.",
                        "artifact_path": "brief.md",
                    },
                )

        class ReviewerStageAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                from pathlib import Path

                review_path = Path(task.input_data["artifact_dir"]).resolve() / "review.md"
                review_path.write_text("# Review\n", encoding="utf-8")
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={
                        "verdict": "no-go",
                        "score": 41,
                        "summary": "Reviewer found launch-blocking gaps.",
                        "files": [str(review_path)],
                    },
                )

        def fake_get_agent(name, *args, **kwargs):
            if name == "VerifierAgent":
                return VerifierStageAgent()
            if name == "ReviewerAgent":
                return ReviewerStageAgent()
            if name in ("ConsistencyReviewerAgent", "ContractVerifierAgent", "PackagingAgent"):
                # Pass-through stub: report a clean verdict so the
                # injected pre-reviewer stages don't change pipeline behavior.
                class _PassThroughStageAgent:
                    async def initialize(self):
                        pass
                    async def execute(self, task, stdin_data=None):
                        from skyn3t.core.agent import TaskResult
                        return TaskResult(task_id=task.task_id, success=True, output={"verdict": "pass", "blocker_count": 0})
                return _PassThroughStageAgent()
            raise AssertionError(f"unexpected agent {name}")

        monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Quality template",
                description="Verify then review.",
                stages=[
                    SimpleNamespace(
                        name="verifier",
                        agent="VerifierAgent",
                        capability="verification",
                        handoff_to="ReviewerAgent",
                        input_extra={},
                    ),
                    SimpleNamespace(
                        name="reviewer",
                        agent="ReviewerAgent",
                        capability="review",
                        handoff_to=None,
                        input_extra={},
                    ),
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start("demo", "Ship a launch pack", slug="quality-priority")

        assert manifest["status"] == "failed"
        assert manifest["next_action"] == "Reviewer found launch-blocking gaps."
        assert manifest["error"] == "Reviewer found launch-blocking gaps."
        assert manifest["quality_summary"]["source"] == "reviewer"
        assert manifest["quality_summary"]["verdict"] == "no-go"
        assert manifest["quality_summary"]["score"] == 41
        assert manifest["quality_summary"]["review_file"] == "review.md"

    async def test_reviewer_no_go_still_runs_post_run_verifiers(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class ReviewerStageAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                artifact_dir = Path(task.input_data["artifact_dir"]).resolve()
                review_path = artifact_dir / "review.md"
                review_path.write_text("# Review\n", encoding="utf-8")
                scaffold_dir = artifact_dir / "scaffold"
                scaffold_dir.mkdir(parents=True, exist_ok=True)
                (scaffold_dir / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={
                        "verdict": "no-go",
                        "score": 41,
                        "summary": "Reviewer found launch-blocking gaps.",
                        "files": [str(review_path)],
                    },
                )

        class ConsistencyReviewerStageAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task, stdin_data=None):
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"verdict": "pass", "blocker_count": 0},
                )

        def fake_get_agent(name, *args, **kwargs):
            if name == "ReviewerAgent":
                return ReviewerStageAgent()
            if name in ("ConsistencyReviewerAgent", "ContractVerifierAgent", "PackagingAgent"):
                return ConsistencyReviewerStageAgent()
            raise AssertionError(f"unexpected agent {name}")

        monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Quality template",
                description="Review a scaffold.",
                stages=[
                    SimpleNamespace(
                        name="reviewer",
                        agent="ReviewerAgent",
                        capability="review",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        calls: list[str] = []

        async def fake_build(scaffold_dir: str, brief: str):
            calls.append("build")
            return {
                "verdict": "yes",
                "stack": "node",
                "summary": "build ok",
                "command": "npm run build",
            }

        async def fake_boot(scaffold_dir: str, brief: str):
            calls.append("boot")
            return {
                "verdict": "yes",
                "kind": "node-express",
                "summary": "boot ok",
                "command": "node index.js",
            }

        async def fake_integration(scaffold_dir: str, brief: str):
            calls.append("integration")
            return {
                "verdict": "yes",
                "kind": "node-express",
                "summary": "integration ok",
                "command": "node index.js",
            }

        async def fake_retry(manifest, brief, slug):
            calls.append("retry")
            return None

        async def fail_build_fix_round(*args, **kwargs):
            raise AssertionError("build fix loop should be skipped on reviewer no-go")

        async def fail_integration_fix_round(*args, **kwargs):
            raise AssertionError("integration fix loop should be skipped on reviewer no-go")

        monkeypatch.setattr(runner, "_run_build_verifier", fake_build)
        monkeypatch.setattr(runner, "_run_boot_verifier", fake_boot)
        monkeypatch.setattr(runner, "_run_integration_verifier", fake_integration)
        monkeypatch.setattr(runner, "_maybe_auto_retry", fake_retry)
        monkeypatch.setattr(runner, "_apply_build_fix_round", fail_build_fix_round)
        monkeypatch.setattr(runner, "_apply_integration_fix_round", fail_integration_fix_round)

        manifest = await runner.start("demo", "Build a dashboard", slug="quality-no-go")

        assert calls == ["build", "boot", "integration", "retry"]
        assert manifest["status"] == "failed"
        assert manifest["error"] == "Reviewer found launch-blocking gaps."
        assert manifest["quality_summary"]["verdict"] == "no-go"
        assert manifest["build_verification"]["verdict"] == "yes"
        assert manifest["boot_verification"]["verdict"] == "yes"
        assert manifest["integration_verification"]["verdict"] == "yes"

    async def test_boot_failure_uses_boot_retry_hint(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class WriterStageAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                artifact_dir = Path(task.input_data["artifact_dir"]).resolve()
                scaffold_dir = artifact_dir / "scaffold"
                scaffold_dir.mkdir(parents=True, exist_ok=True)
                (scaffold_dir / "package.json").write_text(
                    '{"name":"demo"}\n',
                    encoding="utf-8",
                )
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": [str(scaffold_dir / "package.json")]},
                )

        def fake_get_agent(name, *args, **kwargs):
            if name == "WriterAgent":
                return WriterStageAgent()
            raise AssertionError(f"unexpected agent {name}")

        monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Writer template",
                description="Create a scaffold.",
                stages=[
                    SimpleNamespace(
                        name="writer",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)

        async def fake_build(scaffold_dir: str, brief: str):
            return {
                "verdict": "yes",
                "stack": "node",
                "summary": "build ok",
                "command": "npm run build",
            }

        async def fake_boot(scaffold_dir: str, brief: str):
            return {
                "verdict": "no",
                "kind": "node-express",
                "summary": "server failed to start within 45s",
                "command": "node index.js",
                "failure_hint": "server/config-store.js is still a generated TODO stub",
            }

        async def should_not_run_integration(scaffold_dir: str, brief: str):
            raise AssertionError("integration verifier should not run after boot failure")

        async def no_fix_round(*args, **kwargs):
            return False

        async def fake_retry(manifest, brief, slug):
            return None

        monkeypatch.setattr(runner, "_run_build_verifier", fake_build)
        monkeypatch.setattr(runner, "_run_boot_verifier", fake_boot)
        monkeypatch.setattr(runner, "_run_integration_verifier", should_not_run_integration)
        monkeypatch.setattr(runner, "_apply_build_fix_round", no_fix_round)
        monkeypatch.setattr(runner, "_maybe_auto_retry", fake_retry)

        manifest = await runner.start("demo", "Build a dashboard", slug="boot-failure")

        assert manifest["status"] == "failed"
        assert manifest["error"] == "server failed to start within 45s"
        assert manifest["next_action"] == "Retrying with the boot failure as a hint."
        assert manifest["_retry_hint"] == "server/config-store.js is still a generated TODO stub"

    async def test_unresolved_stub_failure_uses_stub_retry_hint(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner, UnresolvedScaffoldStubError

        class WriterStageAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                artifact_dir = Path(task.input_data["artifact_dir"]).resolve()
                scaffold_dir = artifact_dir / "scaffold"
                scaffold_dir.mkdir(parents=True, exist_ok=True)
                (scaffold_dir / "package.json").write_text(
                    '{"name":"demo"}\n',
                    encoding="utf-8",
                )
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": [str(scaffold_dir / "package.json")]},
                )

        def fake_get_agent(name, *args, **kwargs):
            if name == "WriterAgent":
                return WriterStageAgent()
            raise AssertionError(f"unexpected agent {name}")

        monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Writer template",
                description="Create a scaffold.",
                stages=[
                    SimpleNamespace(
                        name="code",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)

        async def fake_retry(manifest, brief, slug):
            return None

        async def should_not_run_build(scaffold_dir: str, brief: str):
            raise AssertionError("build verifier should not run when unresolved stubs remain")

        async def fail_post_code_checks(*args, **kwargs):
            raise UnresolvedScaffoldStubError(
                "Generated scaffold still contains unresolved TODO stubs: "
                "server/index.js. Regenerate those files with real implementations; "
                "do not ship placeholders."
            )

        monkeypatch.setattr(runner, "_maybe_auto_retry", fake_retry)
        monkeypatch.setattr(runner, "_run_build_verifier", should_not_run_build)
        monkeypatch.setattr(runner, "_run_post_code_checks", fail_post_code_checks)

        manifest = await runner.start("demo", "Build a dashboard", slug="stub-failure")

        assert manifest["status"] == "failed"
        assert manifest["error"].startswith(
            "Generated scaffold still contains unresolved TODO stubs:"
        )
        assert manifest["next_action"] == "Retrying with the unresolved stub failure as a hint."
        assert manifest["_retry_hint"].startswith(
            "Generated scaffold still contains unresolved TODO stubs:"
        )

    async def test_missing_planned_files_failure_uses_witness_retry_hint(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class WriterStageAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                artifact_dir = Path(task.input_data["artifact_dir"]).resolve()
                scaffold_dir = artifact_dir / "scaffold"
                scaffold_dir.mkdir(parents=True, exist_ok=True)
                (scaffold_dir / "package.json").write_text(
                    '{"name":"demo"}\n',
                    encoding="utf-8",
                )
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={
                        "files": [str(scaffold_dir / "package.json")],
                        "missing_files": ["src/App.jsx", "src/hooks/useConfig.js"],
                    },
                )

        def fake_get_agent(name, *args, **kwargs):
            if name == "WriterAgent":
                return WriterStageAgent()
            raise AssertionError(f"unexpected agent {name}")

        monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Writer template",
                description="Create a scaffold.",
                stages=[
                    SimpleNamespace(
                        name="code",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)

        async def fake_retry(manifest, brief, slug):  # noqa: ARG001
            return None

        async def should_not_run_build(scaffold_dir: str, brief: str):  # noqa: ARG001
            raise AssertionError("build verifier should not run when planned files are still missing")

        async def fake_frontend_dryrun(**kwargs):
            return None

        monkeypatch.setattr(runner, "_maybe_auto_retry", fake_retry)
        monkeypatch.setattr(runner, "_run_build_verifier", should_not_run_build)
        monkeypatch.setattr(runner, "_run_frontend_build_dryrun", fake_frontend_dryrun)

        manifest = await runner.start("demo", "Build a dashboard", slug="missing-files")

        assert manifest["status"] == "failed"
        assert manifest["next_action"] == "Retrying with the missing file witness as a hint."
        assert manifest["_retry_hint"].startswith(
            "Generated scaffold is still missing planned files:"
        )
        assert "src/App.jsx" in manifest["_retry_hint"]

    async def test_stage_failure_clears_quality_summary(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class VerifierStageAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={
                        "verdict": "yes",
                        "score": 88,
                        "summary": "Verifier approved the current artifact.",
                    },
                )

        class FailingStageAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error="writer crashed",
                )

        def fake_get_agent(name, *args, **kwargs):
            if name == "VerifierAgent":
                return VerifierStageAgent()
            if name == "WriterAgent":
                return FailingStageAgent()
            raise AssertionError(f"unexpected agent {name}")

        monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Failure template",
                description="Verify, then fail.",
                stages=[
                    SimpleNamespace(
                        name="verifier",
                        agent="VerifierAgent",
                        capability="verification",
                        handoff_to="WriterAgent",
                        input_extra={},
                    ),
                    SimpleNamespace(
                        name="writer",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    ),
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = await runner.start("demo", "Ship the draft", slug="quality-failure")

        assert manifest["status"] == "failed"
        assert manifest["quality_summary"] is None

    async def test_resume_clears_stale_quality_summary(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        class CompleteAgent:
            async def initialize(self) -> None:
                return None

            async def execute(self, task: TaskRequest) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    output={"files": ["brief.md"], "summary": "Mission resumed cleanly."},
                )

        monkeypatch.setattr(
            "skyn3t.studio.runner.get_agent",
            lambda *args, **kwargs: CompleteAgent(),
        )
        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Resume template",
                description="Resume a queued mission.",
                stages=[
                    SimpleNamespace(
                        name="writer",
                        agent="WriterAgent",
                        capability="copywriting",
                        handoff_to=None,
                        input_extra={},
                    )
                ],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = runner.reserve_project("demo", "Build a brief", slug="quality-resume")
        manifest["status"] = "awaiting_clarification"
        manifest["clarification"] = {"questions": ["Who is the buyer?"]}
        manifest["quality_summary"] = {
            "source": "reviewer",
            "verdict": "go",
            "raw_verdict": "go",
            "score": 85,
            "summary": "Old quality signal.",
            "review_file": "review.md",
            "updated_at": 1.0,
        }
        runner._save_manifest(tmp_path / "quality-resume", manifest)

        resumed = await runner.resume("quality-resume", ["Operations leaders."])

        assert resumed["status"] == "done"
        assert resumed["quality_summary"] is None

    async def test_reap_orphans_clears_stale_quality_summary(
        self, event_bus, tmp_path
    ):
        from skyn3t.studio.runner import StudioRunner

        project_dir = tmp_path / "orphaned-quality"
        project_dir.mkdir(parents=True)
        (project_dir / "project.json").write_text(
            """
{
  "slug": "orphaned-quality",
  "template": "demo",
  "title": "Orphaned quality",
  "status": "running",
  "quality_summary": {
    "source": "reviewer",
    "verdict": "go",
    "raw_verdict": "go",
    "score": 90,
    "summary": "Old quality signal.",
    "review_file": "review.md",
    "updated_at": 1.0
  }
}
            """.strip(),
            encoding="utf-8",
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = runner.get_project("orphaned-quality")

        assert manifest is not None
        assert manifest["status"] == "interrupted"
        assert manifest["next_action"] == "Project was interrupted because the server restarted."
        assert manifest["quality_summary"] is None

    async def test_mark_project_failed_clears_quality_summary(
        self, event_bus, tmp_path, monkeypatch
    ):
        from skyn3t.studio.runner import StudioRunner

        monkeypatch.setattr(
            "skyn3t.studio.runner.get_template",
            lambda _key: SimpleNamespace(
                title="Failure template",
                description="Used for reserve_project only.",
                stages=[],
            ),
        )

        runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
        manifest = runner.reserve_project("demo", "Build a draft", slug="mark-failed")
        manifest["quality_summary"] = {
            "source": "reviewer",
            "verdict": "go",
            "raw_verdict": "go",
            "score": 86,
            "summary": "Should be cleared.",
            "review_file": "review.md",
            "updated_at": 1.0,
        }
        runner._save_manifest(tmp_path / "mark-failed", manifest)

        failed = runner.mark_project_failed("mark-failed", "runner exploded")

        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["quality_summary"] is None
