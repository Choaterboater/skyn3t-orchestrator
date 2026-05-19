"""Tests for agent implementations."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


class TestGitHubExplorerAgent:
    @pytest.mark.asyncio
    async def test_initialization(self):
        pytest.importorskip("github", reason="PyGithub not installed")
        from skyn3t.agents.github_explorer import GitHubExplorerAgent

        with patch("github.Github") as mock_gh:
            mock_gh.return_value.get_user.return_value = MagicMock()
            agent = GitHubExplorerAgent("gh", EventBus())
            await agent.initialize()
            assert agent.metadata.get("initialized") is True

    @pytest.mark.asyncio
    async def test_health_check(self):
        pytest.importorskip("github", reason="PyGithub not installed")
        from skyn3t.agents.github_explorer import GitHubExplorerAgent

        with patch("github.Github") as mock_gh:
            mock_gh.return_value.get_rate_limit.return_value = MagicMock()
            agent = GitHubExplorerAgent("gh", EventBus())
            await agent.initialize()
            assert await agent.health_check() is True

    @pytest.mark.asyncio
    async def test_unknown_task(self):
        from skyn3t.agents.github_explorer import GitHubExplorerAgent

        agent = GitHubExplorerAgent("gh", EventBus())
        task = TaskRequest(title="Test", input_data={"task_type": "unknown"})
        result = await agent.execute(task)
        assert result.success is False


class TestCodeAgent:
    @pytest.mark.asyncio
    async def test_initialization(self):
        from skyn3t.agents.code_agent import CodeAgent

        agent = CodeAgent("code", EventBus())
        await agent.initialize()
        assert agent.metadata.get("initialized") is True

    @pytest.mark.asyncio
    async def test_code_execution(self):
        from skyn3t.agents.code_agent import CodeAgent

        agent = CodeAgent("code", EventBus())
        await agent.initialize()

        task = TaskRequest(
            title="Run code",
            input_data={
                "task_type": "code_execution",
                "code": "x = 2 + 2\nprint(x)",
            },
        )
        result = await agent.execute(task)
        assert result.success is True
        assert "4" in str(result.output)

    @pytest.mark.asyncio
    async def test_scaffold_task_type_uses_scaffold_flow(self, tmp_path):
        from skyn3t.agents.code_agent import CodeAgent

        agent = CodeAgent("code", EventBus())
        await agent.initialize()

        task = TaskRequest(
            title="Scaffold app",
            input_data={
                "task_type": "scaffold",
                "brief": "Build a tiny hello world app",
                "artifact_dir": str(tmp_path),
            },
        )
        result = await agent.execute(task)
        assert result.success is True
        assert result.task_id == task.task_id
        assert result.output["files"]
        assert (tmp_path / "scaffold").exists()

    @pytest.mark.asyncio
    async def test_scaffold_fallback_writes_code_files(self, tmp_path, monkeypatch):
        from skyn3t.agents.code_agent import CodeAgent

        class StubLLM:
            async def complete(self, *args, **kwargs):
                return "[deterministic-stub]"

        agent = CodeAgent("code", EventBus())
        await agent.initialize()
        monkeypatch.setattr(agent, "get_llm", lambda: StubLLM())

        task = TaskRequest(
            title="Scaffold todo app",
            input_data={
                "task_type": "scaffold",
                "brief": "Build a small todo app",
                "artifact_dir": str(tmp_path),
            },
        )
        result = await agent.execute(task)

        assert result.success is True
        assert {Path(path).name for path in result.output["files"]} >= {
            "index.html",
            "styles.css",
            "app.js",
        }
        assert (tmp_path / "scaffold" / "app.js").exists()

    @pytest.mark.asyncio
    async def test_scaffold_fallback_writes_minesweeper_files(self, tmp_path, monkeypatch):
        from skyn3t.agents.code_agent import CodeAgent

        class StubLLM:
            async def complete(self, *args, **kwargs):
                return "[deterministic-stub]"

        agent = CodeAgent("code", EventBus())
        await agent.initialize()
        monkeypatch.setattr(agent, "get_llm", lambda: StubLLM())

        task = TaskRequest(
            title="Scaffold minesweeper",
            input_data={
                "task_type": "scaffold",
                "brief": "Build new version of minesweeper",
                "artifact_dir": str(tmp_path),
            },
        )
        result = await agent.execute(task)

        assert result.success is True
        assert {Path(path).name for path in result.output["files"]} >= {
            "index.html",
            "styles.css",
            "app.js",
        }
        app_js = (tmp_path / "scaffold" / "app.js").read_text(encoding="utf-8")
        index_html = (tmp_path / "scaffold" / "index.html").read_text(encoding="utf-8")
        assert "placeMines" in app_js
        assert "Minesweeper" in index_html
        assert "todo-form" not in index_html

    @pytest.mark.asyncio
    async def test_scaffold_ignores_paths_outside_scaffold_dir(self, tmp_path, monkeypatch):
        from skyn3t.agents.code_agent import CodeAgent

        class StubLLM:
            async def complete(self, *args, **kwargs):
                return (
                    '{"files": ['
                    '{"path": "../scaffold_log.txt", "content": "bad"}, '
                    '{"path": "app.py", "content": "print(1)"}'
                    "]} "
                )

        agent = CodeAgent("code", EventBus())
        await agent.initialize()
        monkeypatch.setattr(agent, "get_llm", lambda: StubLLM())

        task = TaskRequest(
            title="Scaffold app safely",
            input_data={
                "task_type": "scaffold",
                "brief": "Build a tiny python app",
                "artifact_dir": str(tmp_path),
            },
        )
        result = await agent.execute(task)

        assert result.success is True
        assert result.output["files"] == [str(tmp_path / "scaffold" / "app.py")]
        assert (tmp_path / "scaffold" / "app.py").exists()
        assert not (tmp_path / "scaffold_log.txt").exists()

    @pytest.mark.asyncio
    @pytest.mark.skip(
        reason="Timing-sensitive: relies on 50ms budgets to assert the "
               "retry backend ALSO times out and falls back to placeholder, "
               "but the retry sometimes completes inside the window on "
               "fast hosts (CI included). Needs a deterministic fake "
               "backend that explicitly times out rather than relying on "
               "wall-clock thresholds."
    )
    async def test_scaffold_times_out_hung_file_without_wedging(self, tmp_path, monkeypatch):
        from skyn3t.adapters.llm_client import LLMClient
        from skyn3t.agents import code_agent as code_agent_module
        from skyn3t.agents.code_agent import CodeAgent

        class EmptySkillLibrary:
            def find(self, *args, **kwargs):
                return []

        class PrimaryBackend:
            async def complete(self, req):
                if "Now write the COMPLETE contents of: `src/App.jsx`" in req.prompt:
                    await asyncio.sleep(0.2)
                    return "too slow"
                if "Now write the COMPLETE contents of: `src/styles.css`" in req.prompt:
                    return "[deterministic-stub]"
                return "[deterministic-stub]"

        class RetryBackend:
            async def complete(self, req):
                if "Now write the COMPLETE contents of: `src/App.jsx`" in req.prompt:
                    return "export default function App() {\n  return <main>Recovered</main>;\n}\n"
                if "Now write the COMPLETE contents of: `src/styles.css`" in req.prompt:
                    return ""
                return "[deterministic-stub]"

        async def fake_get_impl(self):
            if "kimi_cli" in self._skip_backends:
                self._backend_name = "copilot_cli"
                return RetryBackend()
            self._backend_name = "kimi_cli"
            return PrimaryBackend()

        monkeypatch.setattr(LLMClient, "_get_impl", fake_get_impl)
        monkeypatch.setattr(code_agent_module, "_FILE_BUILD_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(code_agent_module, "_FILE_RETRY_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(
            "skyn3t.intelligence.skill_library.get_default_library",
            lambda: EmptySkillLibrary(),
        )
        monkeypatch.setattr(
            "skyn3t.config.settings.get_settings",
            lambda: SimpleNamespace(vector_db_path=str(tmp_path / "missing-vector-db")),
        )

        agent = CodeAgent("code", EventBus(), config={"backend": "kimi_cli"})
        await agent.initialize()

        task = TaskRequest(
            title="Scaffold app",
            input_data={
                "task_type": "scaffold",
                "brief": "Build a React Vite dashboard",
                "artifact_dir": str(tmp_path),
            },
        )
        result = await asyncio.wait_for(agent.execute(task), timeout=5.0)

        scaffold_dir = tmp_path / "scaffold"
        assert result.success is True
        assert (scaffold_dir / "README.md").exists()
        assert (scaffold_dir / "package.json").exists()
        assert "// TODO[skyn3t]" in (scaffold_dir / "src" / "App.jsx").read_text(
            encoding="utf-8"
        )
        assert "// TODO[skyn3t]" in (scaffold_dir / "src" / "styles.css").read_text(
            encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_scaffold_uses_architect_handoff_for_generic_dashboard_brief(self, tmp_path, monkeypatch):
        from skyn3t.agents.code_agent import CodeAgent

        class StubLLM:
            async def complete(self, *args, **kwargs):
                return "[deterministic-stub]"

        class EmptySkillLibrary:
            def find(self, *args, **kwargs):
                return []

        (tmp_path / "architecture.md").write_text(
            "A Vite + React SPA frontend with a Node/Express backend, "
            "persistent config store, CRUD API, and health endpoint.",
            encoding="utf-8",
        )
        (tmp_path / "tech_stack.json").write_text(
            json.dumps(
                {
                    "frontend": "Next.js 15 App Router",
                    "backend": "Next.js Route Handlers",
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(
            "skyn3t.intelligence.skill_library.get_default_library",
            lambda: EmptySkillLibrary(),
        )
        monkeypatch.setattr(
            "skyn3t.config.settings.get_settings",
            lambda: SimpleNamespace(vector_db_path=str(tmp_path / "missing-vector-db")),
        )

        agent = CodeAgent("code", EventBus())
        await agent.initialize()
        monkeypatch.setattr(agent, "get_llm", lambda: StubLLM())

        task = TaskRequest(
            title="Scaffold service dashboard",
            input_data={
                "task_type": "scaffold",
                "brief": (
                    "Build a polished dark-mode service dashboard with a "
                    "persistent backend config store, CRUD API, and health endpoint."
                ),
                "artifact_dir": str(tmp_path),
            },
        )
        result = await agent.execute(task)

        scaffold = tmp_path / "scaffold"
        assert result.success is True
        assert (scaffold / "server" / "index.js").exists()
        assert (scaffold / "server" / "config-store.js").exists()
        assert (scaffold / "server" / "routes" / "config.js").exists()
        assert (scaffold / "server" / "data" / "user-config.json").exists()

    @pytest.mark.asyncio
    async def test_scaffold_writes_deterministic_serviceboard_files_for_generic_brief(
        self, tmp_path, monkeypatch
    ):
        from skyn3t.agents.code_agent import CodeAgent

        class StubLLM:
            async def complete(self, *args, **kwargs):
                return "[deterministic-stub]"

        class EmptySkillLibrary:
            def find(self, *args, **kwargs):
                return []

        monkeypatch.setattr(
            "skyn3t.intelligence.skill_library.get_default_library",
            lambda: EmptySkillLibrary(),
        )
        monkeypatch.setattr(
            "skyn3t.config.settings.get_settings",
            lambda: SimpleNamespace(vector_db_path=str(tmp_path / "missing-vector-db")),
        )

        agent = CodeAgent("code", EventBus())
        await agent.initialize()
        monkeypatch.setattr(agent, "get_llm", lambda: StubLLM())

        brief = (
            "Build a polished dark-mode service dashboard with a premium glassmorphism feel. "
            "Scope it to 6-8 integrations and a simple builder/settings flow. "
            "The app must include a real persistent backend config store from the first boot, "
            "server-side CRUD for integrations, a health endpoint, and a UI that saves and "
            "reloads configuration across restarts."
        )
        task = TaskRequest(
            title="Scaffold service dashboard",
            input_data={
                "task_type": "scaffold",
                "brief": brief,
                "artifact_dir": str(tmp_path),
            },
        )

        result = await agent.execute(task)

        scaffold = tmp_path / "scaffold"
        assert result.success is True
        assert "TODO[skyn3t]" not in (scaffold / "src" / "App.jsx").read_text(encoding="utf-8")
        assert "TODO[skyn3t]" not in (scaffold / "src" / "styles.css").read_text(encoding="utf-8")
        assert "TODO[skyn3t]" not in (
            scaffold / "server" / "config-store.js"
        ).read_text(encoding="utf-8")
        assert "TODO[skyn3t]" not in (scaffold / "server" / "index.js").read_text(encoding="utf-8")
        assert "TODO[skyn3t]" not in (
            scaffold / "server" / "routes" / "config.js"
        ).read_text(encoding="utf-8")
        assert "TODO[skyn3t]" not in (
            scaffold / "src" / "components" / "SettingsModal.jsx"
        ).read_text(encoding="utf-8")
        assert "TODO[skyn3t]" not in (
            scaffold / "src" / "components" / "ServiceEditor.jsx"
        ).read_text(encoding="utf-8")
        assert "useConfig" in (scaffold / "src" / "App.jsx").read_text(encoding="utf-8")


    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_scaffold_rag_recall_injects_past_failures(self, tmp_path, monkeypatch):
        """RAG recall should query past experiences and inject failure
        lessons into the build system prompt without crashing."""
        from skyn3t.agents.code_agent import CodeAgent

        class StubLLM:
            async def complete(self, *args, **kwargs):
                return "[deterministic-stub]"

        agent = CodeAgent("code", EventBus())
        await agent.initialize()
        monkeypatch.setattr(agent, "get_llm", lambda: StubLLM())

        # Mock RAG to return a fake failure experience.
        fake_doc = {
            "content": "Previous react_vite build failed because vite.config.js "
                       "missed the server.proxy entry for /api. Always add it.",
            "metadata": {"doc_type": "experience", "success": False},
        }

        async def mock_rag_query(*args, **kwargs):
            return {"documents": [fake_doc], "context": fake_doc["content"]}

        monkeypatch.setattr(
            "skyn3t.rag.rag_engine.RAGEngine.query", mock_rag_query
        )
        async def mock_init(self): pass
        monkeypatch.setattr(
            "skyn3t.rag.rag_engine.RAGEngine.initialize", mock_init
        )

        task = TaskRequest(
            title="Scaffold app",
            input_data={
                "task_type": "scaffold",
                "brief": "Build a React Vite dashboard",
                "artifact_dir": str(tmp_path),
            },
        )
        result = await agent.execute(task)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_scaffold_rag_recall_graceful_when_empty(self, tmp_path, monkeypatch):
        """RAG recall should not fail when the vector store is empty."""
        from skyn3t.agents.code_agent import CodeAgent

        class StubLLM:
            async def complete(self, *args, **kwargs):
                return "[deterministic-stub]"

        agent = CodeAgent("code", EventBus())
        await agent.initialize()
        monkeypatch.setattr(agent, "get_llm", lambda: StubLLM())

        async def mock_rag_query(*args, **kwargs):
            return {"documents": [], "context": ""}

        monkeypatch.setattr(
            "skyn3t.rag.rag_engine.RAGEngine.query", mock_rag_query
        )
        async def mock_init(self): pass
        monkeypatch.setattr(
            "skyn3t.rag.rag_engine.RAGEngine.initialize", mock_init
        )

        task = TaskRequest(
            title="Scaffold app",
            input_data={
                "task_type": "scaffold",
                "brief": "Build a tiny hello world app",
                "artifact_dir": str(tmp_path),
            },
        )
        result = await agent.execute(task)
        assert result.success is True


class TestBrainstormAgent:
    @pytest.mark.asyncio
    async def test_require_clarification_forces_kickoff_questions(self, tmp_path, monkeypatch):
        from skyn3t.agents.brainstorm import BrainstormAgent

        agent = BrainstormAgent(event_bus=EventBus())
        await agent.initialize()

        async def no_questions(_brief: str, *, force: bool = False):
            assert force is True
            return []

        monkeypatch.setattr(agent, "_maybe_ask_clarifications", no_questions)

        task = TaskRequest(
            title="Brainstorm",
            input_data={
                "brief": "Build a launch brief for our new product",
                "artifact_dir": str(tmp_path),
                "require_clarification": True,
            },
        )

        result = await agent.execute(task)

        assert result.success is True
        assert result.output["needs_clarification"] is True
        assert len(result.output["questions"]) == 3
        assert (tmp_path / "_clarifications.json").exists()


class TestSchedulerAgent:
    @pytest.mark.asyncio
    async def test_initialization(self):
        from skyn3t.agents.scheduler_agent import SchedulerAgent

        agent = SchedulerAgent("scheduler", EventBus())
        await agent.initialize()
        assert agent.metadata.get("initialized") is True


class TestExplorerAgent:
    @pytest.mark.asyncio
    async def test_budget_gate_preserves_task_id(self, tmp_path):
        from skyn3t.agents.explorer import ExplorationBudget, ExplorerAgent

        agent = ExplorerAgent(
            event_bus=EventBus(),
            budget=ExplorationBudget(cooldown_seconds=600),
            state_path=tmp_path / "explorer_state.json",
        )
        await agent.initialize()
        agent._state["last_run_ts"] = time.time()

        task = TaskRequest(title="Gap scan", input_data={"mode": "gap_scan"})
        result = await agent.execute(task)

        assert result.success is True
        assert result.task_id == task.task_id
        assert result.output["skipped"] is True


class TestSlackBot:
    @pytest.mark.asyncio
    async def test_handle_dm_event_defaults_missing_channel_and_thread(self, monkeypatch):
        from skyn3t.integrations.slack_bot import SlackBot

        bot = SlackBot(EventBus(), bot_token="test-token")
        captured: list[tuple[str, str, str]] = []

        async def fake_process_message(text: str, channel: str, thread_ts: str) -> None:
            captured.append((text, channel, thread_ts))

        monkeypatch.setattr(bot, "_process_message", fake_process_message)

        await bot._handle_event({"type": "message", "text": "hello", "channel_type": "im"})

        assert captured == [("hello", "", "")]


class TestCodeImproverAgent:
    @pytest.mark.asyncio
    async def test_execute_skips_studio_debug_without_actionable_risks(self, monkeypatch):
        from skyn3t.agents.code_improver import CodeImproverAgent

        agent = CodeImproverAgent(event_bus=EventBus())
        monkeypatch.setattr(agent, "_register_handler", lambda: None)

        async def fail_draft_patch(*args, **kwargs):
            raise AssertionError("_draft_patch should not run for non-actionable reviewer risks")

        monkeypatch.setattr(agent, "_draft_patch", fail_draft_patch)

        result = await agent.execute(
            TaskRequest(
                title="studio_debug retry",
                input_data={
                    "target_file": "projects/demo-project/architecture.md",
                    "intent": "studio_debug",
                    "rationale": (
                        "Address Reviewer's critique on `projects/demo-project/architecture.md`.\n\n"
                        "Verdict: Verdict: no-go\n\n"
                        "Risks to address:\n"
                        "- None detected.\n\n"
                        "Produce a unified diff that resolves these risks while keeping existing structure."
                    ),
                },
            )
        )

        assert result.success is True
        assert result.output["proposed"] is False
        assert result.output["skipped"] is True
        assert result.output["reason"] == "review flagged no actionable risks"

    @pytest.mark.asyncio
    async def test_do_apply_uses_target_repo_root(self, tmp_path, monkeypatch):
        from skyn3t.agents.code_improver import CodeImproverAgent

        repo_root = tmp_path / "customer-portal"
        (repo_root / "src").mkdir(parents=True)
        (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

        agent = CodeImproverAgent(event_bus=EventBus())
        git_call_roots: list[Path] = []
        apply_call_roots: list[Path] = []

        def fake_run_git(args, cwd):
            git_call_roots.append(Path(cwd))
            command = tuple(args)
            if command[:2] == ("rev-parse", "--is-inside-work-tree"):
                return {"ok": True, "stdout": "true\n", "stderr": ""}
            if command[:2] == ("rev-parse", "--abbrev-ref"):
                return {"ok": True, "stdout": "main\n", "stderr": ""}
            if command[:2] == ("rev-parse", "HEAD"):
                return {"ok": True, "stdout": "abcdef1234567890\n", "stderr": ""}
            return {"ok": True, "stdout": "", "stderr": ""}

        def fake_subprocess_run(
            cmd, input=None, text=None, capture_output=None, cwd=None, timeout=None
        ):
            apply_call_roots.append(Path(cwd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(agent, "_run_git", fake_run_git)
        monkeypatch.setattr(
            agent,
            "_run_repo_checks",
            lambda root: {
                "ran": False,
                "ok": True,
                "command": None,
                "stdout": "",
                "stderr": "",
                "note": "skipped",
            },
        )
        monkeypatch.setattr(
            "skyn3t.agents.code_improver.subprocess.run",
            fake_subprocess_run,
        )

        result = await agent._do_apply(
            {
                "target_file": "src/app.py",
                "patch": "@@ -1 +1 @@\n-print('hello')\n+print('fixed')\n",
                "repo_root": str(repo_root),
                "rationale": "Fix greeting",
            }
        )

        assert result["ok"] is True
        assert result["applied"] is True
        assert apply_call_roots
        assert all(root == repo_root for root in git_call_roots)
        assert all(root == repo_root for root in apply_call_roots)

    @pytest.mark.asyncio
    async def test_do_apply_fails_when_commit_fails(self, tmp_path, monkeypatch):
        from skyn3t.agents.code_improver import CodeImproverAgent

        repo_root = tmp_path / "customer-portal"
        (repo_root / "src").mkdir(parents=True)
        (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

        agent = CodeImproverAgent(event_bus=EventBus())

        def fake_run_git(args, cwd):
            command = tuple(args)
            if command[:2] == ("rev-parse", "--is-inside-work-tree"):
                return {"ok": True, "stdout": "true\n", "stderr": ""}
            if command[:2] == ("rev-parse", "--abbrev-ref"):
                return {"ok": True, "stdout": "main\n", "stderr": ""}
            if command[:1] == ("commit",):
                return {"ok": False, "stdout": "", "stderr": "missing identity"}
            return {"ok": True, "stdout": "", "stderr": ""}

        monkeypatch.setattr(agent, "_run_git", fake_run_git)
        monkeypatch.setattr(
            agent,
            "_run_repo_checks",
            lambda root: {
                "ran": False,
                "ok": True,
                "command": None,
                "stdout": "",
                "stderr": "",
                "note": "skipped",
            },
        )
        monkeypatch.setattr(
            "skyn3t.agents.code_improver.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        result = await agent._do_apply(
            {
                "target_file": "src/app.py",
                "patch": "@@ -1 +1 @@\n-print('hello')\n+print('fixed')\n",
                "repo_root": str(repo_root),
                "rationale": "Fix greeting",
            }
        )

        assert result["ok"] is False
        assert result["applied"] is False
        assert result["branch"] is not None
        assert "git commit failed" in result["error"]

    @pytest.mark.asyncio
    async def test_do_apply_reports_rollback_failure(self, tmp_path, monkeypatch):
        from skyn3t.agents.code_improver import CodeImproverAgent

        repo_root = tmp_path / "customer-portal"
        (repo_root / "src").mkdir(parents=True)
        (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

        agent = CodeImproverAgent(event_bus=EventBus())

        def fake_run_git(args, cwd):
            command = tuple(args)
            if command[:2] == ("rev-parse", "--is-inside-work-tree"):
                return {"ok": True, "stdout": "true\n", "stderr": ""}
            if command[:2] == ("rev-parse", "--abbrev-ref"):
                return {"ok": True, "stdout": "main\n", "stderr": ""}
            if command[:2] == ("rev-parse", "HEAD"):
                return {"ok": True, "stdout": "abcdef1234567890\n", "stderr": ""}
            if command[:2] == ("checkout", "main"):
                return {"ok": False, "stdout": "", "stderr": "worktree dirty"}
            return {"ok": True, "stdout": "", "stderr": ""}

        monkeypatch.setattr(agent, "_run_git", fake_run_git)
        monkeypatch.setattr(
            agent,
            "_run_repo_checks",
            lambda root: {
                "ran": True,
                "ok": False,
                "command": "python3 -m pytest -q --tb=line",
                "stdout": "1 failed",
                "stderr": "",
                "note": None,
            },
        )
        monkeypatch.setattr(
            "skyn3t.agents.code_improver.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        result = await agent._do_apply(
            {
                "target_file": "src/app.py",
                "patch": "@@ -1 +1 @@\n-print('hello')\n+print('fixed')\n",
                "repo_root": str(repo_root),
                "rationale": "Fix greeting",
            }
        )

        assert result["ok"] is False
        assert result["applied"] is False
        assert result["branch"] is not None
        assert "rollback failed" in result["error"]

    def test_run_repo_checks_detects_node_scripts(self, tmp_path, monkeypatch):
        from skyn3t.agents.code_improver import CodeImproverAgent

        repo_root = tmp_path / "frontend"
        repo_root.mkdir()
        (repo_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "frontend",
                    "packageManager": "pnpm@9.0.0",
                    "scripts": {
                        "test": "vitest run",
                        "build": "vite build",
                    },
                }
            ),
            encoding="utf-8",
        )

        commands = []

        def fake_run(cmd, capture_output, text, cwd, timeout):
            commands.append((cmd, cwd, timeout))
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        monkeypatch.setattr("skyn3t.agents.code_improver.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr("skyn3t.agents.code_improver.subprocess.run", fake_run)

        result = CodeImproverAgent._run_repo_checks(repo_root)

        assert result["ran"] is True
        assert result["ok"] is True
        assert result["command"] == "pnpm test && pnpm build"
        assert "validated Node repo with pnpm" == result["note"]
        assert commands == [
            (["pnpm", "test"], str(repo_root), 240),
            (["pnpm", "build"], str(repo_root), 240),
        ]

    def test_run_repo_checks_detects_go_modules(self, tmp_path, monkeypatch):
        from skyn3t.agents.code_improver import CodeImproverAgent

        repo_root = tmp_path / "service"
        repo_root.mkdir()
        (repo_root / "go.mod").write_text("module example.com/service\n", encoding="utf-8")

        calls = []

        def fake_run(cmd, capture_output, text, cwd, timeout):
            calls.append((cmd, cwd, timeout))
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        monkeypatch.setattr("skyn3t.agents.code_improver.subprocess.run", fake_run)

        result = CodeImproverAgent._run_repo_checks(repo_root)

        assert result["ran"] is True
        assert result["ok"] is True
        assert result["command"] == "go test ./..."
        assert calls == [(["go", "test", "./..."], str(repo_root), 240)]


class TestReviewerAgent:
    @pytest.mark.asyncio
    async def test_execute_reviews_nested_scaffold_files_and_sanitizes_llm_scorecard(
        self, tmp_path, monkeypatch
    ):
        from skyn3t.agents.reviewer import ReviewerAgent

        artifact_dir = tmp_path / "todo-app"
        artifact_dir.mkdir()
        (artifact_dir / "architecture.md").write_text(
            "## Overview\n\nA lightweight todo app with persistent storage and a simple CRUD UI.\n",
            encoding="utf-8",
        )
        (artifact_dir / "scaffold").mkdir()
        (artifact_dir / "scaffold" / "index.html").write_text(
            "<!doctype html><html><body><main id='app'></main></body></html>\n",
            encoding="utf-8",
        )

        agent = ReviewerAgent(event_bus=EventBus())

        async def noop(*args, **kwargs):
            return None

        async def fake_llm_review(*, brief, contents, **_kwargs):
            assert brief == ""
            assert "scaffold/index.html" in contents
            return (
                "## Summary\n\nThe nested scaffold files are present.\n\n"
                "Score: 90/100\n"
                "Verdict: no-go\n",
                90,
                None,
            )

        monkeypatch.setattr(agent, "think", noop)
        monkeypatch.setattr(agent, "share_learning", noop)
        monkeypatch.setattr(agent, "_llm_review", fake_llm_review)

        result = await agent.execute(
            TaskRequest(title="Review scaffold output", input_data={"artifact_dir": str(artifact_dir)})
        )

        review_text = (artifact_dir / "review.md").read_text(encoding="utf-8")

        assert result.success is True
        assert result.output["verdict"] == "go"
        assert "scaffold/index.html" in review_text
        assert "Score: 90/100" not in review_text
        assert "Verdict: no-go" not in review_text

    @pytest.mark.asyncio
    async def test_execute_does_not_promote_llm_score_into_higher_verdict_bucket(
        self, tmp_path, monkeypatch
    ):
        from skyn3t.agents.reviewer import ReviewerAgent

        artifact_dir = tmp_path / "habit-tracker"
        artifact_dir.mkdir()
        (artifact_dir / "architecture.md").write_text(
            "## Overview\n\nA habit tracker with streaks and local persistence.\n",
            encoding="utf-8",
        )
        (artifact_dir / "scaffold").mkdir()
        (artifact_dir / "scaffold" / "index.html").write_text(
            "<!doctype html><html><body><main id='app'></main></body></html>\n",
            encoding="utf-8",
        )

        agent = ReviewerAgent(event_bus=EventBus())

        async def noop(*args, **kwargs):
            return None

        async def fake_llm_review(*, brief, contents, **_kwargs):
            assert "scaffold/index.html" in contents
            return ("## Summary\n\nStill has important gaps.\n", 69, None)

        monkeypatch.setattr(agent, "think", noop)
        monkeypatch.setattr(agent, "share_learning", noop)
        monkeypatch.setattr(agent, "_llm_review", fake_llm_review)
        monkeypatch.setattr(agent, "_verdict", lambda completeness, consistency, risks: ("go", 100))

        result = await agent.execute(
            TaskRequest(title="Review scaffold output", input_data={"artifact_dir": str(artifact_dir)})
        )

        assert result.success is True
        assert result.output["score"] == 74
        assert result.output["verdict"] == "go-with-fixes"

    @pytest.mark.asyncio
    async def test_execute_keeps_go_bucket_when_llm_score_is_already_go(
        self, tmp_path, monkeypatch
    ):
        from skyn3t.agents.reviewer import ReviewerAgent

        artifact_dir = tmp_path / "go-review"
        artifact_dir.mkdir()
        (artifact_dir / "architecture.md").write_text(
            "## Overview\n\nA production-ready analytics dashboard with a clear implementation plan.\n",
            encoding="utf-8",
        )
        (artifact_dir / "scaffold").mkdir()
        (artifact_dir / "scaffold" / "index.html").write_text(
            "<!doctype html><html><body><main id='app'></main></body></html>\n",
            encoding="utf-8",
        )

        agent = ReviewerAgent(event_bus=EventBus())

        async def noop(*args, **kwargs):
            return None

        async def fake_llm_review(*, brief, contents, **_kwargs):
            assert "scaffold/index.html" in contents
            return ("## Summary\n\nLooks good overall.\n", 82, None)

        monkeypatch.setattr(agent, "think", noop)
        monkeypatch.setattr(agent, "share_learning", noop)
        monkeypatch.setattr(agent, "_llm_review", fake_llm_review)
        monkeypatch.setattr(agent, "_verdict", lambda completeness, consistency, risks: ("go", 100))

        result = await agent.execute(
            TaskRequest(title="Review scaffold output", input_data={"artifact_dir": str(artifact_dir)})
        )

        assert result.success is True
        assert result.output["score"] == 85
        assert result.output["verdict"] == "go"

    @pytest.mark.asyncio
    async def test_critique_times_out_and_returns_no_issues(
        self, tmp_path, monkeypatch
    ):
        from skyn3t.agents.reviewer import ReviewerAgent

        artifact_dir = tmp_path / "designer-pack"
        artifact_dir.mkdir()
        (artifact_dir / "brand.md").write_text(
            "# Brand\n\nA polished dashboard brand pack with clear hierarchy and palette guidance.\n",
            encoding="utf-8",
        )
        (artifact_dir / "palette.json").write_text(
            json.dumps(
                {
                    "primary": "#112233",
                    "secondary": "#223344",
                    "accent": "#334455",
                    "bg": "#0B1020",
                    "text": "#F8FAFC",
                }
            ),
            encoding="utf-8",
        )
        (artifact_dir / "components.md").write_text(
            "# Components\n\nUse the palette consistently across the dashboard shell, cards, and settings drawer.\n",
            encoding="utf-8",
        )

        calls = {"count": 0}

        class SlowLLMClient:
            async def complete(self, prompt, max_tokens, temperature, **_kwargs):  # noqa: ARG002
                calls["count"] += 1
                await asyncio.sleep(1)
                return "1. brand.md: this should never land"

        agent = ReviewerAgent(event_bus=EventBus())
        monkeypatch.setattr("skyn3t.adapters.LLMClient", lambda *args, **kwargs: SlowLLMClient())
        monkeypatch.setattr("skyn3t.agents.reviewer._LLM_CRITIQUE_TIMEOUT_SECONDS", 0.01)

        critique = await agent.critique(
            brief="Build a polished dashboard.",
            artifact_dir=artifact_dir,
            stage_name="designer",
            produced_files=["brand.md", "palette.json", "components.md"],
        )

        assert critique["has_issues"] is False
        assert critique["issues"] == []
        assert critique["critique_text"] == ""
        assert calls["count"] == 1
