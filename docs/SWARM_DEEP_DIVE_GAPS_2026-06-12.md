# SkyN3t Orchestrator — Swarm Deep-Dive Gap Analysis

**Date:** 2026-06-12  
**Scope:** Full Skyn3t project at `/Users/stephenchoate/Documents/Skyn3t/repo` plus the empty runtime data directory at `/Users/stephenchoate/Documents/Skyn3t/data`.  
**Method:** Six concurrent read-only explore agents (critical bugs, architecture/dead-code, tests, security, autonomy/learning, ops/UI) plus automated baseline checks (pytest, ruff, mypy).  
**Codebase scale:** ~212 Python files / ~108,715 LOC, 172 test files / ~48,247 LOC, 36 UI source files.

---

## Executive Summary

SkyN3t is an ambitious, genuinely running multi-agent orchestrator. The previous audit’s two CRITICAL issues (default in-process RCE and worktree merge-back hollow builds) **are fixed**, and most HIGH items from `SKYN3T_PLAN.md` have been addressed. The build-pattern → skill graduation loop is alive and producing real artifacts.

However, the codebase still has **severe gaps** in four areas:

1. **Security:** The default path is safer, but an authenticated attacker can still downgrade execution to in-process RCE via the web API, path traversal is possible, the Linux sandbox is weak, and live API tokens sit in a world-readable `.env`.
2. **Silent correctness failures:** Verification often proves “syntax OK” or “built without error,” not “actually works.” Failed upstream tasks can cascade into downstream agents. Resource leaks (clients, zombies, Docker pool races) accumulate silently.
3. **Self-learning is half-wired:** One loop (build patterns) works; the richer lesson-attribution, skill-grading, and RAG-recall loops are captured-then-discarded or wired to dead ends.
4. **Architecture by accretion:** Large subsystems (`distributed/`, remote execution backends, ACP server, browser agent) are production-dead, and the backend exposes 130 endpoints while the UI uses only ~36.

**Current automated baseline:**

| Check | Result |
|---|---|
| `ruff check skyn3t tests` | ✅ Passed |
| `mypy skyn3t` | ❌ 25 errors in 8 files |
| `pytest tests/ --ignore=tests/test_observability.py` | ❌ 34 failed, 2670 passed, 1 skipped |

The test failures are almost all stale routing/model-name assertions, not logic bugs, but they make the CI baseline red and mask real regressions.

---

## 1. Critical Gaps (Fix Before Any Wider Exposure)

### CRIT-1 — Authenticated RCE via `/api/exec` downgrade to `InlineBackend`
- **Files:** `skyn3t/web/app.py:1520-1568`, `skyn3t/security/sandbox.py:1074-1108`
- **Issue:** `PATCH /api/execution/backend` lets a token holder persist `backend=inline` without requiring `SKYN3T_ALLOW_INLINE_EXEC=1`. Once persisted, `POST /api/exec` runs submitted code through `InlineBackend.execute()` → Python `exec()` in the main process.
- **Evidence:** `get_backend("inline")` bypasses the opt-in guard entirely. The default `"auto"` path is fixed, but the explicit `"inline"` path is not.
- **Fix:** Refuse to persist `inline` unless `SKYN3T_ALLOW_INLINE_EXEC` is set; gate `get_backend("inline")` itself behind the same flag (or remove `InlineBackend`).

### CRIT-2 — Path traversal in Studio project slug
- **Files:** `skyn3t/studio/runner.py:119-137`, `3522-3547`; `skyn3t/web/app.py:3817-3868`
- **Issue:** Caller-supplied `slug` is used directly as a path component under `projects_root`. No validation rejects `..`, absolute paths, or traversal sequences.
- **Exploit:** `POST /api/studio/start {"slug":"../evil",...}` creates projects outside the intended tree.
- **Fix:** Strict slug regex (`^[a-z0-9_-]{1,64}$`), reject `..`, canonicalize with `resolve()` + `relative_to(projects_root)`.

### CRIT-3 — World-readable `.env` contains live API tokens
- **File:** `.env` (repo root)
- **Issue:** `.env` is `644` and contains real `SKYN3T_TELEGRAM_TOKEN`, `OPENROUTER_API_KEY`, and `GITHUB_TOKEN`.
- **Fix:** `chmod 600 .env`, move credentials to encrypted store/keychain, rotate exposed tokens.

---

## 2. High Severity Gaps

### Security

| # | File:Line | Issue | Evidence |
|---|---|---|---|
| HIGH-1 | `skyn3t/security/sandbox.py:278-444` | Linux CLI sandbox has no namespace/seccomp/network enforcement; only POSIX rlimits. | `preexec_fn = self._setup_resource_limits` only. |
| HIGH-2 | `skyn3t/security/sandbox.py:739-752`, `851-868` | Docker containers run as root with full capabilities. | No `--user`, `--cap-drop`, or `--security-opt no-new-privileges`. |
| HIGH-3 | `skyn3t/security/sandbox.py:53-58` | Secret scrubbing misses common provider keys and over-strips `TOKEN*`. | Missing `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `AZURE_OPENAI_API_KEY`. |
| HIGH-4 | `skyn3t/security/permissions.py:153-401` | Permission engine exists but is never enforced before task dispatch or agent execution. | Zero production callers to `can_execute_task()`. |
| HIGH-5 | `skyn3t/adapters/mcp_tools.py:22-27`, `mcp_client.py:34-41` | Path checks use `startswith` or no check, allowing prefix-confusion traversal. | `str(target).startswith(str(REPO_ROOT.resolve()))`. |

### Correctness / Silent Failures

| # | File:Line | Issue | Evidence |
|---|---|---|---|
| HIGH-6 | `skyn3t/agents/build_verifier.py:355-364` | Python verifier only runs `py_compile`; missing imports pass as `verdict: yes`. | No venv/requirements install or import test. |
| HIGH-7 | `skyn3t/agents/build_verifier.py:449-464` | npm network failures silently promote to `yes` in fast/offline mode. | 404s containing `"ENOTFOUND"` are misclassified. |
| HIGH-8 | `skyn3t/agents/build_verifier.py:1188` | Forced `CI=1` turns warnings into errors for CRA/Vite. | `env={**os.environ, "CI": "1"}` unconditionally. |
| HIGH-9 | `skyn3t/agents/code_agent.py:2479-2525` | Per-file routed `LLMClient` is constructed but never `aclose()`'d. | Socket/FD leak on long builds. |
| HIGH-10 | `skyn3t/cli/main.py:2215-2258` | `task --pipe-to` forwards upstream output even when upstream failed. | Only checks `status != "pending"`. |
| HIGH-11 | `skyn3t/adapters/llm_client.py:579-598` | `_probe_version` leaves zombie children on timeout. | `proc.kill()` without `await proc.wait()`. |
| HIGH-12 | `skyn3t/security/sandbox.py:842-878` | `DockerPoolBackend._ensure_pool` is unsynchronized; double-start/name collisions. | No `asyncio.Lock` guards check-then-init. |

### Self-Learning / Autonomy

| # | File:Line | Issue | Evidence |
|---|---|---|---|
| HIGH-13 | `skyn3t/intelligence/learning_loop.py` + `studio/runner.py:1297-1305` | Lesson attribution is broken for Studio builds (the dominant path). | `data/lesson_scores.json` does not exist; Studio bypasses orchestrator `TASK_ROUTED`. |
| HIGH-14 | `skyn3t/memory/ingestor.py:285-317` | Reflection lessons are persisted to SQL but **never embedded into RAG**. | `doc_type=lesson` count in vector DB is 0. |
| HIGH-15 | `skyn3t/agents/code_agent.py:1869-1909` | Skill grading only fires for a hard-coded tag list; most skills accumulate no evidence. | `fastapi-health-check.md` has `success_count: 0`. |
| HIGH-16 | `skyn3t/memory/store.py` / `studio/runner.py:1428-1435` | Stage-failure events omit `stack`; 80% of `experience_index` rows have `NULL` stack. | SQL query: 136/170 rows `stack IS NULL`. |

### Operations / UI

| # | File:Line | Issue | Evidence |
|---|---|---|---|
| HIGH-17 | `docker-compose.yml:5-7` | References `Dockerfile` that does not exist. | `docker compose up --build` fails. |
| HIGH-18 | `skyn3t/web/app.py:781-801`, `646-662` | `/health` and `/metrics` are not exempt from web-token auth. | Healthchecks/Prometheus fail in tokenized deploys. |
| HIGH-19 | `skyn3t/web/app.py:651`, `682` | Web token compared with non-constant-time `!=`. | Timing side-channel risk. |
| HIGH-20 | `skyn3t/web/app.py:1258`, `1659`, `1872` | Raw `str(e)` leaks in 500 responses. | Internal paths/exceptions exposed. |

---

## 3. Medium Severity Gaps

### Security
- Telegram webhook unauthenticated when `TELEGRAM_WEBHOOK_SECRET` unset.
- Discord admin-secret compared with `!=` (timing side-channel).
- CORS defaults to `["*"]`, `allow_credentials=False` but no CSRF protection.
- Rate-limiter trusts leftmost `X-Forwarded-For` without trusted-proxy list.
- `/api/skills/install` accepts arbitrary local paths and git URLs (SSRF/file enumeration).
- `data/secrets.json` written with default permissions (predicted `644`).

### Generated-App Quality / UI
- Visual verification gate is Node-only; static/Python/Swift stacks get no visual check.
- No enforcement of SkyN3t design-token contract in generated apps.
- Screenshot rubric is optional and degrades to crude heuristics.
- Static render gate only catches console errors, not styling/layout.

### Operations
- Token tracker is an in-memory singleton → wrong with uvicorn workers > 1, resets on restart.
- Trajectory logger writes synchronously inside event-bus handlers, stalling the bus.
- Trajectory token keys (`prompt_tokens`) never match published `LLM_EXCHANGE` keys (`prompt_chars`).
- `scripts/setup.sh` advertises non-existent `daemon` mode and does not build the SPA.
- `python-dotenv` is used in CLI but missing from `requirements.txt` / `pyproject.toml`.
- CORS `allow_methods` omits `PUT`.

---

## 4. Architecture & Dead Code

### Entirely unwired / production-dead subsystems

| Subsystem | Size | Evidence |
|---|---|---|
| `skyn3t/distributed/` | ~464 LOC | Zero imports outside its own directory. |
| `skyn3t/intelligence/backends/` (e2b/modal/daytona/ssh) | ~600 LOC | Only `/api/backends` reports status; no execution path uses them. |
| `skyn3t/integrations/acp_server.py` | ~399 LOC | Only exercised by `tests/test_acp_server.py`. |
| `skyn3t/agents/browser_agent.py` | ~570 LOC | Only `/api/browser/status` instantiates it; no run path. |

### Backend API surface vs UI consumption
- Backend exposes **~130 routes**; React UI calls **~36**.
- Unused families: `/api/users/*`, `/api/schedule/*`, `/api/cron`, `/api/pipeline/*`, `/api/conversation`, most `/api/tasks/*`, `/api/memory/drafts/*`, `/api/trajectories/*`, `/api/exec`, `/api/skills/candidates`, many integration endpoints.

### Circular dependencies
- `core/model_router ↔ core/openrouter_catalog ↔ core/model_evolution` (via `_TIERS`).
- `cli/main ↔ cli/repl`.
- `cortex/never_stop ↔ cortex/continuous_improvement ↔ cortex/autonomous_loop`.
- `adapters/llm_client ↔ adapters/openrouter`.

### Multiple overlapping “run code somewhere else” abstractions
- `security/sandbox.py` — `ExecutionBackend` (actually used).
- `intelligence/subagent_runner.py` — `SubagentRunner` (tests only).
- `intelligence/docker_backend.py` — `DockerSubagentRunner` (tests only).
- `intelligence/backends/base.py` — `BaseRemoteBackend` (status only).

### Event bus noise
- 45 `EventType` values; many are published with no subscriber or subscribed with no publisher.
- `AGENT_CONVERSATION_*`, `AGENT_MESSAGE_SENT/RECEIVED`, `MESSAGE`, `COLLECTIVE_INSIGHT`, `CORTEX_DECISION`, and many RAG events are effectively dead.

---

## 5. Testing Gaps

### Current automated baseline
```
34 failed, 2670 passed, 1 skipped, 1 deselected, 1 warning in 183.84s
```

### Failure root cause
The 34 failures are concentrated in **stale routing/model-name tests** caused by uncommitted production changes in `model_router.py` / `project_type_router.py`:

- `test_model_router_adaptive.py` — 4 failures
- `test_router_cost_weighted.py` — 6 failures
- `test_phase5a_routing.py` — 3 failures
- `test_core.py::TestOrgChart` — 3 failures
- `test_web_app.py` — 5 failures
- `test_llm_client.py` — 3 failures
- plus one each in `test_cheap_smart.py`, `test_cli_main.py`, `test_cortex_decisions.py`, `test_repo_scout.py`, `test_agent_fleet.py`, `test_routing_observations.py`, `test_sandbox.py`, `test_web_hardening.py`

### Lowest-coverage critical modules (>20 statements)

| Coverage | File | Risk |
|---|---|---|
| 10.4% | `agents/research_agent.py` | Untested critical agent |
| 13.7% | `rag/web_scraper.py` | Untested |
| 15.4% | `agents/github_ingestor.py` | Untested critical learning ingest |
| 16.1% | `agents/file_ops_agent.py` | Untested |
| 20.8% | `integrations/email_agent.py` | Untested critical integration |
| 29.4% | `intelligence/planner.py` | Partial critical |
| 38.8% | `cli/main.py` | Large partial |
| 41.4% | `cli/repl.py` | Large partial |
| 41.9% | `agents/code_improver.py` | Partial critical |
| 45.1% | `agents/boot_verifier.py` | **Execute path mostly untested** |
| 53.0% | `web/app.py` | Large partial |
| 59.8% | `agents/code_agent.py` | Partial critical |
| 62.6% | `studio/runner.py` | Partial critical |

### Test anti-patterns
- ~2,576 mock/patch references; many tests monkeypatch internal helpers rather than inject dependencies.
- 83 `sleep`/`asyncio.sleep` calls in tests → potential flakes.
- No true end-to-end test of CodeAgent → BuildVerifier → BootVerifier → PackagingAgent on a real scaffold.

---

## 6. What Is Actually Working Well

- **CRITICAL fixes from prior audit landed:** default RCE closed, worktree merge-back works, most HIGH correctness bugs fixed.
- **Build-pattern self-learning loop is alive:** `data/build_patterns.json` (1.9 MB), `node-winning-shape.md` (127 successes), `build_pattern_preferences.json`.
- **Autonomous loop runs end-to-end:** 51–84 builds/day, proof runs, retry queues.
- **Large test suite exists:** 2,670 passing tests, strong regression coverage for recent audit fixes.
- **Ruff clean.**
- **React dashboard is real:** shared `SwarmProvider`, Cortex brain graph, live build console, settings.

---

## 7. Recommended Priority Order

### Phase 0 — Safety & correctness (must land before wider exposure)
1. Close the `/api/exec` inline downgrade (CRIT-1).
2. Sanitize Studio project slugs (CRIT-2).
3. Lock down `.env` and rotate tokens (CRIT-3).
4. Constant-time token compare; exempt `/health`/`/metrics` from auth.
5. Harden Linux sandbox (namespaces/seccomp/network) or require Docker.
6. Run Docker containers non-root with dropped caps.
7. Enforce `PermissionEngine` before agent execution.
8. Fix MCP path containment (`relative_to`, not `startswith`).

### Phase 1 — Silent correctness failures
9. Make Python verifier actually install deps/run imports or downgrade verdict.
10. Tighten npm “network error” classification; never pass on misclassified 404s.
11. Remove unconditional `CI=1` or make it opt-in.
12. Close per-file `LLMClient` leak in `code_agent.py`.
13. Fix `--pipe-to` to inspect upstream success.
14. Fix `_probe_version` zombie and `DockerPoolBackend` start race.

### Phase 2 — Self-learning feedback edges
15. Wire Studio builds into lesson attribution (publish `TASK_ROUTED`/`TASK_COMPLETED` or call `LessonScoreboard` directly).
16. Embed reflection lessons into RAG (`ingest_lesson` → `rag.add_knowledge_one`).
17. Broaden skill-grading tag matching and record use for all injected skills.
18. Include `stack` in all stage-failure events; backfill `unknown` bucket.
19. Consume RAG observability metrics in a health gate/dashboard.

### Phase 3 — Architecture cleanup
20. Delete or fully wire `skyn3t/distributed/`.
21. Delete or wire remote execution backends; consolidate on one `ExecutionBackend` protocol.
22. Remove status-only `browser_agent.py` and `acp_server.py` unless UI/commands are built.
23. Audit and delete/document the ~94 unused API endpoints.
24. Break model-router circular dependency by moving `_TIERS` to `core/routing_constants.py`.

### Phase 4 — Testing & baseline
25. Fix the 34 stale routing tests so CI is green.
26. Backfill `boot_verifier.execute()` with a real scaffold boot test.
27. Add an E2E scaffold pipeline test (mocked LLM, real FS/subprocess).
28. Add tests for `research_agent.py`, `github_ingestor.py`, `email_agent.py`.
29. Fix the 25 mypy errors.
30. Reduce timing-sensitive tests.

### Phase 5 — Ops/UI polish
31. Add a working `Dockerfile`.
32. Make setup script build the SPA; fix `daemon` mode advertisement.
33. Add `python-dotenv` to dependencies; add `PUT` to CORS.
34. Move token tracker/trajectory persistence off the hot event loop.
35. Consume `/api/insights` and `/api/usage/stage-latency` in the dashboard.
36. Extend visual verification to static/Python stacks and enforce design-token contract.

---

## 8. Bottom Line

SkyN3t is **not a paper architecture** — it runs, learns from build patterns, and ships real (sometimes working) scaffolds. But it is still a **dangerous半成品 (dangerous half-product)** for any non-local use:

- An authenticated web user can still get host RCE.
- Generated apps can pass verification while being unrunnable.
- The system practices constantly but throws away most of what it learns.
- Large subsystems are dead weight, and the API surface is ~3× larger than the UI can use.

The highest-leverage next step is **Phase 0**: close the remaining RCE/path-traversal/secrets issues, because everything else is moot if the orchestrator can be trivially compromised or leak cloud credentials. After safety, the next biggest win is **closing the lesson-attribution and RAG-embedding loops** so that the “self-improving” claim is supported by more than one working feedback edge.
