"""Code Agent - executes, analyzes, refactors, and tests code."""

import ast
import io
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any, Awaitable, Callable, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.code_agent")


class CodeAgent(BaseAgent):
    """Agent for safe code execution, analysis, refactoring, and testing."""

    def __init__(
        self,
        name: str = "code_agent",
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="code",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="code_execution",
                description=(
                    "Execute Python code in-process with a restricted-builtins shim. "
                    "NOT a real sandbox: an attacker who controls the code can escape "
                    "via __subclasses__ or imports. Only use with trusted input."
                ),
                parameters={"code": "str", "timeout": "int"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="code_analysis",
                description="Analyze code quality, complexity, and style",
                parameters={"code": "str", "analysis_type": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="refactoring",
                description="Refactor and improve code structure",
                parameters={"code": "str", "refactor_type": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="test_runner",
                description="Run tests and report results",
                parameters={"test_code": "str", "test_framework": "str"},
            )
        )
        self._sandbox_dir = self.config.get("sandbox_dir", tempfile.gettempdir())
        self._max_output_size = self.config.get("max_output_size", 10000)
        self._execution_timeout = self.config.get("execution_timeout", 30)

    async def initialize(self) -> None:
        """Initialize the code agent."""
        os.makedirs(self._sandbox_dir, exist_ok=True)
        self.metadata["sandbox_dir"] = self._sandbox_dir
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        """Check if the code execution environment is healthy."""
        try:
            test_code = "print('health_check_ok')"
            result = await self._execute_code(test_code)
            return bool(result.get("success", False))
        except Exception:
            return False

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        """Execute a code-related task."""
        # Studio context: brief but no code → scaffold from brief instead of failing.
        d = task.input_data or {}
        if not d.get("code") and not d.get("task_type") and d.get("brief"):
            return await self._scaffold_from_brief(task)
        task_type = d.get("task_type", "code_execution")

        if task_type == "scaffold":
            return await self._scaffold_from_brief(task)

        handlers: Dict[str, Callable[[TaskRequest], Awaitable[Dict[str, Any]]]] = {
            "code_execution": self._execute_code,
            "code_analysis": self._analyze_code,
            "refactoring": self._refactor_code,
            "test_runner": self._run_tests,
        }

        handler = handlers.get(task_type)
        if not handler:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Unknown task type: {task_type}",
            )

        try:
            result: Dict[str, Any] = await handler(task)
            return TaskResult(
                task_id=task.task_id,
                success=result.get("success", True),
                output=result,
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=str(e),
            )

    async def _scaffold_from_brief(self, task: TaskRequest) -> TaskResult:
        """Generate new code from a brief into artifact_dir/scaffold/.

        Two-phase build so the model isn't trying to fit an entire project
        into a single LLM response:

          Phase 1 — Plan: ask for a JSON file plan (path + one-line purpose
                          per file). Cheap, structured.
          Phase 2 — Build: loop the plan; for each file, ask the model to
                          emit JUST that file's contents. Each file gets
                          its own 8000-token budget — a 10-file project
                          now gets ~10x the headroom of the old single-
                          call scaffold, and on subscription-backed CLI
                          providers (claude/copilot/kimi) the cap is
                          ignored entirely.
        """
        from pathlib import Path as _Path
        import json as _json
        import re as _re
        d = task.input_data or {}
        brief = (d.get("brief") or "").strip()
        artifact_dir = _Path(d.get("artifact_dir") or ".")
        out_dir = artifact_dir / "scaffold"
        out_dir.mkdir(parents=True, exist_ok=True)
        resolved_out_dir = out_dir.resolve()
        files_written: List[str] = []

        # Hard cap on plan size so a runaway model can't generate 1000 files.
        MAX_FILES = 25

        try:
            client = self.get_llm() if hasattr(self, "get_llm") else None
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(default_model=self.config.get("model"),
                                   backend=self.config.get("backend"),
                                   event_bus=self.event_bus, caller_name=self.name)

            # ── Phase 1: plan ───────────────────────────────────────────
            plan_system = (
                "You are a senior engineer planning a small, runnable project. "
                "Output a JSON object: {\"stack\": \"...\", \"files\": [{\"path\": "
                "\"relative/path\", \"purpose\": \"one-line description\"}, ...]}. "
                "Pick a tech stack matching the brief — HTML+JS for browser games "
                "and static UIs, FastAPI/Flask for Python APIs, Express/Node for "
                "JS APIs. Aim for 3-12 files: source, config, README, and a tiny "
                "test when relevant. JSON only, no preamble."
            )
            plan_prompt = f"Brief:\n{brief}\n\nReturn the JSON plan."
            await self.think("planning project structure")
            plan_out = await client.complete(
                plan_prompt, system=plan_system, max_tokens=4000, temperature=0.3,
            )
            plan: Dict[str, Any] = {}
            if plan_out and "[deterministic-stub]" not in plan_out:
                m = _re.search(r"\{[\s\S]*\}", plan_out)
                if m:
                    try:
                        plan = _json.loads(m.group(0))
                    except Exception:
                        plan = {}

            file_specs = plan.get("files") if isinstance(plan, dict) else None
            stack = (plan.get("stack") if isinstance(plan, dict) else None) or "minimal"
            if not isinstance(file_specs, list) or not file_specs:
                file_specs = []
            else:
                file_specs = file_specs[:MAX_FILES]

            # ── Phase 2: build one file at a time ───────────────────────
            file_index = "\n".join(
                f"- {(s.get('path') or '').strip()}: {(s.get('purpose') or '').strip()}"
                for s in file_specs
                if isinstance(s, dict) and s.get("path")
            )
            build_system = (
                "You are implementing one file of a small project. Given the "
                "brief, the full file plan, the stack, and which specific file "
                "to write, output ONLY that file's raw contents — no JSON "
                "wrapper, no fenced code block, no preamble, no explanation. "
                "Just the contents that should be written to disk verbatim."
            )
            for i, spec in enumerate(file_specs, start=1):
                if not isinstance(spec, dict):
                    continue
                rel = (spec.get("path") or "").lstrip("/").strip()
                purpose = (spec.get("purpose") or "").strip()
                if not rel:
                    continue
                target = (out_dir / rel).resolve()
                try:
                    target.relative_to(resolved_out_dir)
                except ValueError:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                await self.think(f"building file {i}/{len(file_specs)}: {rel}")
                file_prompt = (
                    f"Brief:\n{brief}\n\nStack: {stack}\n\n"
                    f"Full file plan:\n{file_index}\n\n"
                    f"Now write the COMPLETE contents of: `{rel}`\n"
                    f"Purpose: {purpose}\n\n"
                    "Return ONLY the file's raw contents (no JSON, no fences)."
                )
                try:
                    body = await client.complete(
                        file_prompt, system=build_system,
                        max_tokens=8000, temperature=0.3,
                    )
                except Exception:
                    body = ""
                if not body or "[deterministic-stub]" in body:
                    continue
                body = body.strip()
                # Strip a leftover fenced block if the model wrapped one anyway.
                fence = _re.match(r"^```[a-zA-Z0-9_+\-]*\n([\s\S]*?)\n```\s*$", body)
                if fence:
                    body = fence.group(1)
                try:
                    target.write_text(body, encoding="utf-8")
                    files_written.append(str(target))
                except Exception:
                    continue
        except Exception:
            logger.exception("scaffold-from-brief failed; falling back to deterministic stub")

        if not files_written:
            files_written = self._write_fallback_scaffold(out_dir, brief)

        try:
            await self.share_learning(
                f"scaffold: {len(files_written)} files for brief",
                scope="studio",
            )
        except Exception:
            logger.debug("share_learning(scaffold) failed", exc_info=True)

        return TaskResult(
            task_id=task.task_id, success=True,
            output={"files": files_written,
                    "summary": f"Scaffolded {len(files_written)} file(s) for the brief.",
                    "scaffold_dir": str(out_dir)})

    def _write_fallback_scaffold(self, out_dir, brief: str) -> list[str]:
        brief_lower = (brief or "").lower()
        if "minesweeper" in brief_lower:
            return self._write_minesweeper_scaffold(out_dir, brief)
        if any(
            token in brief_lower
            for token in ("todo", "frontend", "ui", "website", "site", "landing", "dashboard", "app")
        ):
            return self._write_frontend_scaffold(out_dir, brief)
        if any(
            token in brief_lower
            for token in ("api", "backend", "service", "server", "webhook", "docker", "container")
        ):
            return self._write_backend_scaffold(out_dir, brief)
        return self._write_script_scaffold(out_dir, brief)

    def _write_frontend_scaffold(self, out_dir, brief: str) -> list[str]:
        files = {
            "index.html": """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>SkyN3t Starter</title>
    <link rel="stylesheet" href="styles.css">
  </head>
  <body>
    <main class="app-shell">
      <section class="card">
        <header class="card-header">
          <p class="eyebrow">SkyN3t scaffold</p>
          <h1>Todo starter</h1>
          <p class="lede">""" + brief + """</p>
        </header>
        <form id="todo-form" class="todo-form">
          <input id="todo-input" type="text" placeholder="Add a task" autocomplete="off">
          <button type="submit">Add</button>
        </form>
        <ul id="todo-list" class="todo-list"></ul>
      </section>
    </main>
    <script src="app.js"></script>
  </body>
</html>
""",
            "styles.css": """:root {
  color-scheme: dark;
  font-family: Inter, system-ui, sans-serif;
}

body {
  margin: 0;
  min-height: 100vh;
  background: linear-gradient(180deg, #0f172a, #111827 60%, #020617);
  color: #e5eefb;
}

.app-shell {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem;
}

.card {
  width: min(560px, 100%);
  background: rgba(15, 23, 42, 0.88);
  border: 1px solid rgba(148, 163, 184, 0.22);
  border-radius: 20px;
  padding: 1.5rem;
  box-shadow: 0 24px 80px rgba(15, 23, 42, 0.45);
}

.eyebrow {
  margin: 0 0 0.35rem;
  color: #38bdf8;
  font-size: 0.78rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.lede {
  color: #cbd5e1;
}

.todo-form {
  display: flex;
  gap: 0.75rem;
  margin: 1.25rem 0;
}

.todo-form input {
  flex: 1;
  border: 1px solid rgba(148, 163, 184, 0.26);
  border-radius: 999px;
  padding: 0.8rem 1rem;
  background: rgba(15, 23, 42, 0.75);
  color: inherit;
}

.todo-form button,
.todo-item button {
  border: 0;
  border-radius: 999px;
  background: #38bdf8;
  color: #0f172a;
  padding: 0.8rem 1rem;
  font-weight: 700;
  cursor: pointer;
}

.todo-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.75rem;
}

.todo-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.9rem 1rem;
  border-radius: 14px;
  background: rgba(30, 41, 59, 0.92);
  border: 1px solid rgba(148, 163, 184, 0.18);
}

.todo-item.done span {
  text-decoration: line-through;
  color: #94a3b8;
}
""",
            "app.js": """const form = document.getElementById('todo-form');
const input = document.getElementById('todo-input');
const list = document.getElementById('todo-list');

const todos = [
  { id: crypto.randomUUID(), text: 'Sketch the happy path', done: false },
  { id: crypto.randomUUID(), text: 'Wire the UI state', done: false },
];

function renderTodos() {
  list.innerHTML = '';
  todos.forEach((todo) => {
    const item = document.createElement('li');
    item.className = `todo-item${todo.done ? ' done' : ''}`;

    const label = document.createElement('span');
    label.textContent = todo.text;
    label.addEventListener('click', () => {
      todo.done = !todo.done;
      renderTodos();
    });

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => {
      const index = todos.findIndex((entry) => entry.id === todo.id);
      if (index >= 0) {
        todos.splice(index, 1);
        renderTodos();
      }
    });

    item.append(label, remove);
    list.append(item);
  });
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const value = input.value.trim();
  if (!value) return;
  todos.unshift({ id: crypto.randomUUID(), text: value, done: false });
  input.value = '';
  renderTodos();
});

renderTodos();
""",
        }
        return self._write_scaffold_files(out_dir, files)

    def _write_minesweeper_scaffold(self, out_dir, brief: str) -> list[str]:
        files = {
            "index.html": """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Minesweeper</title>
    <link rel="stylesheet" href="styles.css">
  </head>
  <body>
    <main class="app-shell">
      <section class="game-panel">
        <header class="hero">
          <p class="eyebrow">SkyN3t scaffold</p>
          <h1>Minesweeper</h1>
          <p class="lede">""" + brief + """</p>
        </header>

        <section class="toolbar" aria-label="Game controls">
          <div class="difficulty-group" role="group" aria-label="Difficulty">
            <button type="button" class="difficulty is-active" data-difficulty="beginner">Beginner</button>
            <button type="button" class="difficulty" data-difficulty="intermediate">Intermediate</button>
            <button type="button" class="difficulty" data-difficulty="expert">Expert</button>
          </div>
          <button type="button" id="reset-btn" class="reset-btn">New game</button>
        </section>

        <section class="status-bar" aria-label="Game status">
          <div class="stat">
            <span class="stat-label">Mines</span>
            <strong id="mine-count">10</strong>
          </div>
          <div class="stat">
            <span class="stat-label">Time</span>
            <strong id="timer">0</strong>
          </div>
          <div class="stat">
            <span class="stat-label">State</span>
            <strong id="status-text">Ready</strong>
          </div>
        </section>

        <section class="board-shell">
          <div id="board" class="board" role="grid" aria-label="Minesweeper board"></div>
        </section>

        <p class="hint">Left click to reveal. Right click to flag. First click is always safe.</p>
      </section>
    </main>

    <script src="app.js"></script>
  </body>
</html>
""",
            "styles.css": """:root {
  color-scheme: dark;
  font-family: Inter, system-ui, sans-serif;
  --bg: #081c15;
  --panel: #1b4332;
  --panel-border: rgba(116, 198, 157, 0.24);
  --text: #f8f9fa;
  --muted: #b7e4c7;
  --accent: #74c69d;
  --accent-strong: #52b788;
  --danger: #ef476f;
  --cell-size: 40px;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-height: 100vh;
  background: radial-gradient(circle at top, #2d6a4f, var(--bg) 58%);
  color: var(--text);
}

.app-shell {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem 1rem;
}

.game-panel {
  width: min(720px, 100%);
  background: rgba(8, 28, 21, 0.88);
  border: 1px solid var(--panel-border);
  border-radius: 24px;
  padding: 1.5rem;
  box-shadow: 0 24px 70px rgba(0, 0, 0, 0.28);
}

.hero h1,
.hero p {
  margin: 0;
}

.eyebrow {
  margin-bottom: 0.4rem;
  color: var(--accent);
  font-size: 0.76rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.lede {
  margin-top: 0.55rem;
  color: var(--muted);
}

.toolbar,
.status-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  align-items: center;
  justify-content: space-between;
  margin-top: 1.25rem;
}

.difficulty-group {
  display: inline-flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

button {
  border: 0;
  border-radius: 999px;
  font: inherit;
  cursor: pointer;
}

.difficulty,
.reset-btn {
  padding: 0.7rem 1rem;
  background: rgba(116, 198, 157, 0.12);
  color: var(--text);
  border: 1px solid rgba(116, 198, 157, 0.22);
}

.difficulty.is-active,
.reset-btn {
  background: var(--accent);
  color: #081c15;
  font-weight: 700;
}

.status-bar {
  padding: 0.9rem 1rem;
  background: rgba(27, 67, 50, 0.66);
  border-radius: 18px;
}

.stat {
  min-width: 110px;
}

.stat-label {
  display: block;
  color: var(--muted);
  font-size: 0.78rem;
  margin-bottom: 0.2rem;
}

.board-shell {
  margin-top: 1rem;
  overflow-x: auto;
}

.board {
  display: grid;
  gap: 6px;
  justify-content: start;
}

.cell {
  width: var(--cell-size);
  height: var(--cell-size);
  border-radius: 12px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(248, 249, 250, 0.08);
  color: var(--text);
  font-weight: 700;
  transition: transform 120ms ease, background 120ms ease;
}

.cell:hover {
  transform: translateY(-1px);
  background: rgba(248, 249, 250, 0.14);
}

.cell.revealed {
  background: rgba(183, 228, 199, 0.18);
  border-color: rgba(183, 228, 199, 0.2);
}

.cell.mine {
  background: rgba(239, 71, 111, 0.2);
}

.cell.flagged {
  color: #ffb703;
}

.cell[data-count="1"] { color: #8ecae6; }
.cell[data-count="2"] { color: #74c69d; }
.cell[data-count="3"] { color: #ffd166; }
.cell[data-count="4"] { color: #f78c6b; }
.cell[data-count="5"] { color: #ff99c8; }
.cell[data-count="6"] { color: #cdb4db; }
.cell[data-count="7"] { color: #f8f9fa; }
.cell[data-count="8"] { color: #dee2e6; }

.hint {
  margin: 1rem 0 0;
  color: var(--muted);
  font-size: 0.92rem;
}
""",
            "app.js": """const boardEl = document.getElementById('board');
const statusEl = document.getElementById('status-text');
const mineCountEl = document.getElementById('mine-count');
const timerEl = document.getElementById('timer');
const resetBtn = document.getElementById('reset-btn');
const difficultyButtons = [...document.querySelectorAll('[data-difficulty]')];

const difficulties = {
  beginner: { rows: 8, cols: 8, mines: 10 },
  intermediate: { rows: 12, cols: 12, mines: 24 },
  expert: { rows: 16, cols: 16, mines: 40 },
};

let difficultyKey = 'beginner';
let state = null;
let timerId = null;

function neighbors(row, col) {
  const points = [];
  for (let y = row - 1; y <= row + 1; y += 1) {
    for (let x = col - 1; x <= col + 1; x += 1) {
      if (y === row && x === col) continue;
      if (y < 0 || x < 0 || y >= state.rows || x >= state.cols) continue;
      points.push([y, x]);
    }
  }
  return points;
}

function createCell(row, col) {
  return {
    row,
    col,
    mine: false,
    flagged: false,
    revealed: false,
    adjacent: 0,
  };
}

function buildState() {
  const settings = difficulties[difficultyKey];
  const cells = Array.from({ length: settings.rows }, (_, row) =>
    Array.from({ length: settings.cols }, (_, col) => createCell(row, col))
  );
  state = {
    ...settings,
    cells,
    firstClick: true,
    gameOver: false,
    revealedSafeCells: 0,
    flagsUsed: 0,
    seconds: 0,
  };
  boardEl.style.gridTemplateColumns = `repeat(${state.cols}, var(--cell-size))`;
  stopTimer();
  timerEl.textContent = '0';
  mineCountEl.textContent = String(state.mines);
  statusEl.textContent = 'Ready';
}

function placeMines(safeRow, safeCol) {
  const forbidden = new Set([`${safeRow}:${safeCol}`]);
  neighbors(safeRow, safeCol).forEach(([row, col]) => forbidden.add(`${row}:${col}`));
  const openSpots = [];
  state.cells.forEach((row) => {
    row.forEach((cell) => {
      if (!forbidden.has(`${cell.row}:${cell.col}`)) openSpots.push(cell);
    });
  });
  for (let i = openSpots.length - 1; i > 0; i -= 1) {
    const swapIndex = Math.floor(Math.random() * (i + 1));
    [openSpots[i], openSpots[swapIndex]] = [openSpots[swapIndex], openSpots[i]];
  }
  openSpots.slice(0, state.mines).forEach((cell) => {
    cell.mine = true;
  });
  state.cells.forEach((row) => {
    row.forEach((cell) => {
      cell.adjacent = neighbors(cell.row, cell.col).filter(([y, x]) => state.cells[y][x].mine).length;
    });
  });
}

function startTimer() {
  stopTimer();
  timerId = window.setInterval(() => {
    state.seconds += 1;
    timerEl.textContent = String(state.seconds);
  }, 1000);
}

function stopTimer() {
  if (timerId) {
    window.clearInterval(timerId);
    timerId = null;
  }
}

function revealCell(row, col) {
  const cell = state.cells[row][col];
  if (cell.revealed || cell.flagged || state.gameOver) return;
  if (state.firstClick) {
    placeMines(row, col);
    state.firstClick = false;
    statusEl.textContent = 'Playing';
    startTimer();
  }
  cell.revealed = true;
  if (cell.mine) {
    finishGame(false);
    return;
  }
  state.revealedSafeCells += 1;
  if (cell.adjacent === 0) {
    neighbors(row, col).forEach(([y, x]) => revealCell(y, x));
  }
  if (state.revealedSafeCells === state.rows * state.cols - state.mines) {
    finishGame(true);
  }
}

function toggleFlag(row, col) {
  const cell = state.cells[row][col];
  if (cell.revealed || state.gameOver) return;
  cell.flagged = !cell.flagged;
  state.flagsUsed += cell.flagged ? 1 : -1;
  mineCountEl.textContent = String(Math.max(state.mines - state.flagsUsed, 0));
  renderBoard();
}

function finishGame(won) {
  state.gameOver = true;
  stopTimer();
  statusEl.textContent = won ? 'Cleared!' : 'Boom!';
  state.cells.forEach((row) => {
    row.forEach((cell) => {
      if (cell.mine) cell.revealed = true;
    });
  });
  renderBoard();
}

function renderBoard() {
  boardEl.innerHTML = '';
  state.cells.forEach((row) => {
    row.forEach((cell) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'cell';
      button.setAttribute('role', 'gridcell');
      button.dataset.row = String(cell.row);
      button.dataset.col = String(cell.col);
      if (cell.revealed) {
        button.classList.add('revealed');
        if (cell.mine) {
          button.classList.add('mine');
          button.textContent = 'X';
        } else if (cell.adjacent > 0) {
          button.dataset.count = String(cell.adjacent);
          button.textContent = String(cell.adjacent);
        } else {
          button.textContent = '';
        }
      } else if (cell.flagged) {
        button.classList.add('flagged');
        button.textContent = '!';
      } else {
        button.textContent = '';
      }
      button.addEventListener('click', () => {
        revealCell(cell.row, cell.col);
        renderBoard();
      });
      button.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        toggleFlag(cell.row, cell.col);
      });
      boardEl.append(button);
    });
  });
}

function resetGame(nextDifficulty = difficultyKey) {
  difficultyKey = nextDifficulty;
  difficultyButtons.forEach((button) => {
    button.classList.toggle('is-active', button.dataset.difficulty === difficultyKey);
  });
  buildState();
  renderBoard();
}

difficultyButtons.forEach((button) => {
  button.addEventListener('click', () => resetGame(button.dataset.difficulty));
});

resetBtn.addEventListener('click', () => resetGame(difficultyKey));

resetGame('beginner');
""",
        }
        return self._write_scaffold_files(out_dir, files)

    def _write_backend_scaffold(self, out_dir, brief: str) -> list[str]:
        files = {
            "main.py": """from fastapi import FastAPI

app = FastAPI(title="SkyN3t Starter API")


@app.get('/health')
async def health() -> dict[str, str]:
    return {'status': 'ok'}


@app.get('/brief')
async def brief() -> dict[str, str]:
    return {'brief': """ + repr(brief) + """}
""",
            "requirements.txt": "fastapi==0.116.1\nuvicorn==0.35.0\n",
        }
        return self._write_scaffold_files(out_dir, files)

    def _write_script_scaffold(self, out_dir, brief: str) -> list[str]:
        files = {
            "main.py": """def main() -> None:
    print('SkyN3t starter scaffold')
    print(""" + repr(brief) + """)


if __name__ == '__main__':
    main()
""",
        }
        return self._write_scaffold_files(out_dir, files)

    @staticmethod
    def _write_scaffold_files(out_dir, files: Dict[str, str]) -> list[str]:
        written: list[str] = []
        for rel_path, content in files.items():
            target = out_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(str(target))
        return written

    async def _execute_code(self, task_or_code) -> Dict[str, Any]:
        """Execute Python code with a restricted-builtins shim.

        WARNING: This is not a real sandbox. The restricted-builtins dict can be
        escaped (e.g. ``().__class__.__bases__[0].__subclasses__()``). It only
        limits accidental use of dangerous names; it does not contain hostile code.
        For untrusted code, route through ``skyn3t.security.sandbox`` instead.
        """
        if isinstance(task_or_code, TaskRequest):
            code = task_or_code.input_data.get("code", "")
        else:
            code = task_or_code

        if not code:
            return {"success": False, "error": "No code provided"}

        # Restricted builtins shim (not a real sandbox; see method docstring).
        safe_builtins = {
            "abs": abs,
            "all": all,
            "any": any,
            "ascii": ascii,
            "bin": bin,
            "bool": bool,
            "bytearray": bytearray,
            "bytes": bytes,
            "chr": chr,
            "complex": complex,
            "dict": dict,
            "dir": dir,
            "divmod": divmod,
            "enumerate": enumerate,
            "filter": filter,
            "float": float,
            "format": format,
            "frozenset": frozenset,
            "hasattr": hasattr,
            "hash": hash,
            "hex": hex,
            "id": id,
            "int": int,
            "isinstance": isinstance,
            "issubclass": issubclass,
            "iter": iter,
            "len": len,
            "list": list,
            "map": map,
            "max": max,
            "min": min,
            "next": next,
            "oct": oct,
            "ord": ord,
            "pow": pow,
            "print": print,
            "range": range,
            "repr": repr,
            "reversed": reversed,
            "round": round,
            "set": set,
            "slice": slice,
            "sorted": sorted,
            "str": str,
            "sum": sum,
            "tuple": tuple,
            "type": type,
            "zip": zip,
        }

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()

        try:
            sys.stdout = stdout_buffer
            sys.stderr = stderr_buffer

            compiled_code = compile(code, "<sandbox>", "exec")
            exec_globals = {"__builtins__": safe_builtins}
            exec(compiled_code, exec_globals)

            output = stdout_buffer.getvalue()
            errors = stderr_buffer.getvalue()

            if len(output) > self._max_output_size:
                output = output[: self._max_output_size] + "\n...[truncated]"

            return {
                "success": True,
                "output": output,
                "errors": errors,
                "truncated": len(stdout_buffer.getvalue()) > self._max_output_size,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    async def _analyze_code(self, task: TaskRequest) -> Dict[str, Any]:
        """Analyze code quality and structure."""
        code = task.input_data.get("code", "")
        analysis_type = task.input_data.get("analysis_type", "general")

        if not code:
            return {"success": False, "error": "No code provided"}

        result = {"analysis_type": analysis_type, "issues": []}

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return {"success": False, "error": f"Syntax error: {e}"}

        if analysis_type in ("general", "complexity"):
            # Simple complexity metrics
            func_count = len([n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)])
            class_count = len([n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)])
            import_count = len([n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))])

            lines = code.splitlines()
            blank_lines = len([line for line in lines if not line.strip()])
            comment_lines = len([line for line in lines if line.strip().startswith("#")])

            result["metrics"] = {
                "functions": func_count,
                "classes": class_count,
                "imports": import_count,
                "total_lines": len(lines),
                "blank_lines": blank_lines,
                "comment_lines": comment_lines,
                "code_lines": len(lines) - blank_lines - comment_lines,
            }

        if analysis_type in ("general", "style"):
            # Simple style checks
            lines = code.splitlines()
            for i, line in enumerate(lines, 1):
                if len(line) > 120:
                    result["issues"].append({
                        "line": i,
                        "type": "style",
                        "message": f"Line too long ({len(line)} > 120 characters)",
                    })
                if line.rstrip() != line:
                    result["issues"].append({
                        "line": i,
                        "type": "style",
                        "message": "Trailing whitespace",
                    })

        result["success"] = True
        return result

    async def _refactor_code(self, task: TaskRequest) -> Dict[str, Any]:
        """Refactor code based on specified type."""
        code = task.input_data.get("code", "")
        refactor_type = task.input_data.get("refactor_type", "format")

        if not code:
            return {"success": False, "error": "No code provided"}

        refactored = code
        changes = []

        if refactor_type in ("format", "all"):
            # Simple formatting: normalize whitespace
            lines = code.splitlines()
            formatted_lines = []
            prev_blank = False
            for line in lines:
                stripped = line.rstrip()
                if not stripped:
                    if not prev_blank:
                        formatted_lines.append("")
                        prev_blank = True
                else:
                    formatted_lines.append(stripped)
                    prev_blank = False
            refactored = "\n".join(formatted_lines)
            changes.append("Normalized whitespace and removed trailing whitespace")

        if refactor_type in ("imports", "all"):
            # Sort and deduplicate the leading import block. Use ast end_lineno
            # to track the *line span* of imports, not their *node count*; a
            # single multi-line `from x import (a, b, c)` is one node spanning
            # several lines, so slicing by len(imports) corrupts the file.
            try:
                tree = ast.parse(refactored)
                imports: List[str] = []
                last_import_line = 0  # 1-based, inclusive
                for node in tree.body:
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        imports.append(ast.unparse(node))
                        end = getattr(node, "end_lineno", node.lineno)
                        if end and end > last_import_line:
                            last_import_line = end
                    else:
                        break
                if imports and last_import_line > 0:
                    sorted_imports = sorted(set(imports))
                    rest_lines = refactored.splitlines()[last_import_line:]
                    refactored_lines = sorted_imports + [""] + rest_lines
                    refactored = "\n".join(refactored_lines)
                    changes.append("Sorted and deduplicated imports")
            except Exception:
                pass

        return {
            "success": True,
            "original": code,
            "refactored": refactored,
            "changes": changes,
            "refactor_type": refactor_type,
        }

    async def _run_tests(self, task: TaskRequest) -> Dict[str, Any]:
        """Run tests using pytest or unittest."""
        test_code = task.input_data.get("test_code", "")
        test_framework = task.input_data.get("test_framework", "pytest")
        target_code = task.input_data.get("target_code", "")

        if not test_code:
            return {"success": False, "error": "No test code provided"}

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write target code if provided
            if target_code:
                target_path = os.path.join(tmpdir, "target_module.py")
                with open(target_path, "w") as f:
                    f.write(target_code)

            # Write test code
            test_path = os.path.join(tmpdir, "test_module.py")
            with open(test_path, "w") as f:
                if target_code:
                    f.write("import sys\nsys.path.insert(0, '{}')\n".format(tmpdir))
                f.write(test_code)

            try:
                if test_framework == "pytest":
                    cmd = [sys.executable, "-m", "pytest", test_path, "-v", "--tb=short"]
                else:
                    cmd = [sys.executable, "-m", "unittest", "-v", test_path]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._execution_timeout,
                    cwd=tmpdir,
                )

                return {
                    "success": result.returncode == 0,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "framework": test_framework,
                }
            except subprocess.TimeoutExpired:
                return {"success": False, "error": "Tests timed out"}
            except Exception as e:
                return {"success": False, "error": str(e)}
