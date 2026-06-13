# Plan: Skill Registry + Test Health + Sandboxed Execution

## Context

The user approved all three improvement areas from the audit:
1. **User-visible value** → Skill registry (we already have `SkillLibrary` in `skyn3t/intelligence/skill_library.py`, but no discoverability layer)
2. **Engineering health** → Fix test suite slowness (~300s total, `test_agents.py` scaffold tests take 60–90s each) + mypy errors (6 errors across 3 files)
3. **Ops reliability** → Sandboxed code execution (currently runs inline; no isolation)

---

## Phase 1: Quick Wins — Mypy + Test Speed (est. 1–2h)

### 1a. Fix mypy errors

| File | Error | Fix |
|------|-------|-----|
| `skyn3t/studio/runner.py:707` | `int(Any\|None)` | Guard with `isinstance(raw_starter, (int, str))` before int() |
| `skyn3t/agents/code_improver.py` | `_prevalidate_diff` undefined | Add missing helper or remove dead call |
| `skyn3t/agents/code_improver.py` | `self` param missing | Add `@staticmethod` or fix signature |
| `skyn3t/agents/code_improver.py` | `no-any-return` (×2) | Add explicit casts or narrow types |
| `skyn3t/agents/reviewer_fixes.py:77` | Unused coroutine | Add `await` or remove call |

### 1b. Fix test suite slowness

**Root cause:** `test_agents.py` scaffold tests call `CodeAgent._scaffold_from_brief()` which runs the full Studio pipeline including real LLM adapter calls. Each scaffold test does 2–3 LLM round-trips.

**Fix:** Mock the LLM adapter at the `StudioRunner` level so scaffold tests run in <1s each.

- Add a `FakeStudioRunner` or monkeypatch `StudioRunner.start()` to return a pre-built `StudioResult` with deterministic files.
- Keep *one* integration test that exercises the real scaffold path (marked `@pytest.mark.slow`) so we still have coverage.
- Target: `test_agents.py` drops from ~180s to <10s.

---

## Phase 2: Skill Registry — Discovery + Install + CLI (est. 2–3h)

The `SkillLibrary` already reads/writes markdown skills to `data/skills/`. We need:

1. **Remote skill source** — A `SkillSource` dataclass pointing at a git URL or local path with a `SKILL.md` + manifest.
2. **`skyn3t skills` CLI subcommand**:
   - `list` — show installed skills with scores
   - `install <url>` — clone/fetch a skill directory and `SkillLibrary.import_agent_skill()`
   - `search <query>` — `find_relevant()`
   - `remove <name>` — `delete()`
3. **Web API endpoints** (in `skyn3t/web/app.py`):
   - `GET /api/skills` — list
   - `POST /api/skills/install` — install from URL
   - `DELETE /api/skills/{name}` — remove
4. **Default skill seed** — Ship `examples/skills_seed/` with a couple of starter skills (FastAPI health-check pattern, React useConfig hook pattern) so the registry isn't empty on first run.

---

## Phase 3: Sandboxed Execution — Docker Backend (est. 3–4h)

**Current state:** `CodeAgent._execute_code()` runs code inline in the SkyN3t process. No isolation.

**Goal:** Add a pluggable execution backend with a Docker-based sandbox as the default.

1. **Abstract `ExecutionBackend` protocol**:
   - `execute(code: str, language: str, timeout: int) -> ExecutionResult`
2. **`InlineBackend`** — current behavior, renamed for clarity.
3. **`DockerBackend`** — spins up a lightweight container per execution:
   - Reuse a pool of warm containers for speed
   - Network disabled by default
   - Memory/CPU limits via docker run flags
   - Read-only rootfs + tmpfs for /tmp
4. **Config hook** — `settings.EXECUTION_BACKEND = "inline" | "docker"`
5. **Graceful fallback** — if Docker is unavailable, fall back to InlineBackend with a warning.

---

## Execution Order

| Phase | Task | Est. Time |
|-------|------|-----------|
| 1a | Fix mypy errors | 15 min |
| 1b | Mock LLM in scaffold tests | 45 min |
| 2 | Skill registry CLI + API | 2–3 h |
| 3 | Docker sandbox backend | 3–4 h |

**Total: ~6–8 hours of focused work.**

We can ship Phase 1+2 as one PR (fast, high value) and Phase 3 as a follow-up (larger, safety-critical).
