# SkyN3t Deep-Dive Debug Report
**Date:** 2026-05-20  
**Triggered by:** `run deep dive debug report`  
**Status:** Active issues identified — fixes required

---

## Executive Summary

The SkyN3t orchestrator is **online** but the codebase has **regressed from the 2026-05-15 green baseline**. A new `self_healing` module was introduced with type-safety and import issues, existing tests are failing, the full test suite is too slow to complete, and the runtime environment is missing critical API credentials. The system is stable in idle mode but would fail under active Studio workloads.

| Priority | Issue | Count / Severity |
|---|---|---|
| P0 | Test regressions (asyncio import, packaging scorer) | 4 failures |
| P0 | Full pytest suite runtime | >300 s (timeout) |
| P0 | Missing LLM API credentials | 3 keys absent |
| P1 | New `self_healing` module — mypy + ruff violations | 10 type errors, import drift |
| P1 | `code_improver` generates corrupt git patches | Log-confirmed |
| P1 | Uvicorn shutdown leaks semaphores | Recurring warning |
| P2 | `SECRET_KEY` is placeholder | Security warning |
| P2 | HF_TOKEN missing | Rate-limit warning |

---

## 1. Test Suite Health

### 1.1 Collection
- **Total tests collected:** 1,621
- **Baseline (2026-05-15):** 664 passed
- **Current full run:** Killed after 300 s (timeout)

### 1.2 Confirmed Failures

#### `tests/test_telegram_bot.py` — 2 failures
```
test_build_intent_kicks_off_project  NameError: name 'asyncio' is not defined
test_slash_build_with_brief          NameError: name 'asyncio' is not defined
```
**Root cause:** Missing `import asyncio` in `tests/test_telegram_bot.py` (lines 143, 238). This is a recent regression — the file was modified in the latest commit but the import was not added.

#### `tests/test_reviewer_packaging_axis.py` — 2 failures
```
TestEdgeCases.test_nonexistent_path_returns_zero
    assert 5 == 0
TestEdgeCases.test_fullstack_zero_config_not_docked
    AssertionError: assert 'web' == 'fullstack'
```
**Root cause:** The `test_nonexistent_path` expects a score of 0 for a missing artifact directory, but the scorer awards a 5-point baseline for README + .gitignore even when the path does not exist. The `test_fullstack_zero_config` expects `StackDetector` to return `"fullstack"` but the heuristic classifies it as `"web"` when the backend manifest is empty.

### 1.3 Timeout Risk
- `tests/test_agents.py` — exceeds 120 s (killed by timeout)
- `tests/test_core.py` — passes (51 tests in ~63 s)
- `tests/test_memory.py` + `test_web_hardening.py` — pass (78 tests in ~114 s)

**Assessment:** The suite has become slow enough that CI or local runs will time out. The bottleneck appears to be in `test_agents.py` and the overall 1,621-test collection.

---

## 2. Static Analysis

### 2.1 Ruff — 14 errors
| Code | Count | Files |
|---|---|---|
| I001 (unsorted imports) | 12 | `skyn3t/self_healing/*.py`, `skyn3t/studio/runner.py` |
| F821 (undefined name) | 2 | `tests/test_telegram_bot.py` (`asyncio`) |

**Note:** 11 of the 14 are auto-fixable with `ruff check --fix`.

### 2.2 MyPy — 10 errors in 5 files
| File | Errors | Summary |
|---|---|---|
| `skyn3t/self_healing/learned_generators.py` | 4 | `ModuleSpec | None` not narrowed; `Any` returned from typed function |
| `skyn3t/self_healing/retry_manager.py` | 1 | Implicit `Optional` default prohibited (`file_path: str = None`) |
| `skyn3t/self_healing/budget.py` | 0 (notes only) | PEP 484 `no_implicit_optional` notes |
| `skyn3t/studio/runner.py` | 2 | `int()` called on `Any \| None`; `list[str] - int` operand mismatch |
| `skyn3t/web/app.py` | 0 (notes only) | Bodies of untyped functions not checked |

**Assessment:** The new `self_healing` package is the primary source of type errors. It was added after the 2026-05-15 baseline and has not been cleaned up.

---

## 3. Uncommitted Changes

```
 M skyn3t/agents/code_agent.py
 M skyn3t/agents/targeted_fix.py
 M skyn3t/studio/runner.py
 M tests/test_reviewer_packaging_axis.py
 M tests/test_telegram_bot.py
?? skyn3t/self_healing/
```

### 3.1 `code_agent.py` — learned-generator integration
A new backfill path was added to `_backfill_unresolved_imports`:
- Queries `LearnedGeneratorManager` for permanently learned generators.
- Falls back to LLM call via `llm_client.complete()` when no deterministic generator exists.
- Uses `asyncio.run(coro)` inside a synchronous method, with a `RuntimeError` fallback to `run_until_complete`.

**Risk:** Nested event loops. If `_backfill_unresolved_imports` is called from within an already-running async context (likely inside `CodeAgent.execute`), `asyncio.run()` will raise `RuntimeError`. The fallback tries `get_event_loop().run_until_complete()`, which can also fail if the loop is already running. This is a latent crash.

### 3.2 `skyn3t/self_healing/` — new untracked package
Four new modules:
- `__init__.py` — exports budget, taxonomy, learned generators, retry manager
- `budget.py` — `ProjectIterationBudget` with burn-rate tracking
- `error_taxonomy.py` — `ErrorClass`, `RecoveryHint`, `ErrorTaxonomy`
- `learned_generators.py` — `LearnedGeneratorManager` with dynamic module loading
- `retry_manager.py` — `AdaptiveRetryManager` with backoff strategies

**Risk:** Dynamic module loading (`module_from_spec`, `exec_module`) is untested and lacks safety guards for malicious or broken generator code.

---

## 4. Environment & Configuration

### 4.1 Missing API Keys
The `.env` file is missing standard LLM credentials required by adapters:
- `OPENAI_API_KEY` — absent
- `ANTHROPIC_API_KEY` — absent
- `KIMI_API_KEY` — absent
- `HF_TOKEN` — absent (causes HF Hub unauthenticated warnings)

**Present:** `OPENROUTER_API_KEY`, `SKYN3T_TELEGRAM_TOKEN`, `SKYN3T_TELEGRAM_USER_ID`, `PROJECTS_DIR`, `SKYN3T_MAX_BUILD_COST_USD`.

**Impact:** Any task routed to OpenAI, Anthropic, or Kimi backends will fail. The system can only operate via OpenRouter or local providers.

### 4.2 Security
```
SECRET_KEY is empty or set to the default placeholder; set a strong SECRET_KEY before running in production.
```
This warning fires on every test and server startup.

---

## 5. Runtime Issues from Logs

### 5.1 Code Improver — corrupt patches
Source: `logs/recovery-uvicorn-6660.log`
```
skyn3t.agents.code_improver.CodePatchApplyError: git commit failed:
_llm_draft attempt 1 rejected by git apply --check: error: corrupt patch at line 15
_llm_draft attempt 2 rejected by git apply --check: error: patch fragment without header at line 32
```
**Frequency:** Multiple occurrences. The LLM-generated patches are malformed enough that `git apply --check` rejects them. The retry loop attempts twice but does not recover.

### 5.2 Uvicorn — leaked semaphores on shutdown
```
UserWarning: resource_tracker: There appear to be 1 leaked semaphore objects to clean up at shutdown: {'/loky-91419-...'}
```
**Frequency:** Every server shutdown. Likely caused by `joblib`/`loky` multiprocessing backend used by sentence-transformers or ChromaDB embedding pipelines.

### 5.3 HF Hub — unauthenticated requests
```
Warning: You are sending unauthenticated requests to the HF Hub.
```
**Impact:** Slower model downloads and lower rate limits for embedding models.

---

## 6. Database Snapshot

```
sqlite3 data/skyn3t.db
  agents:   31 rows
  tasks:    11 rows
  messages: 256 rows
```

No corruption detected. The database is small and operational.

---

## 7. Proposal Queue

Four pending proposals (all from 14–15 hours ago):

| ID | Type | Status | Summary |
|---|---|---|---|
| `92a9b71485eb` | ingest | pending | Ingest open-source agent frameworks |
| `4f7daff5f067` | ingest | pending | Ingest agentic RAG examples |
| `91cd1725473e` | feature | pending | Build pattern: prefer winning shape for node scaffolds |
| `84964a3ccd77` | feature | pending | Build pattern: prefer winning shape for node scaffolds |

No operator approval or rejection recorded. The swarm is waiting on human gates.

---

## 8. Swarm Health Snapshot

```
Status:         Online
Total Agents:   19
Running Tasks:  0
Completed:      0
Pipelines:      0
```

All agents idle, zero queue depth. The scheduler agent is **disabled**.

---

## Recommended Fix Order

1. **Add missing `import asyncio` to `tests/test_telegram_bot.py`** — unblocks 2 test failures immediately.
2. **Resolve `test_reviewer_packaging_axis.py` assertions** — either update tests to match new scorer behavior or fix scorer logic.
3. **Run `ruff check --fix` across `skyn3t/self_healing/`** — resolves 11 of 14 lint errors.
4. **Fix mypy errors in `self_healing/`** — add `Optional[str]`, narrow `ModuleSpec`, fix `list[str] - int` in `studio/runner.py`.
5. **Audit `code_agent.py` nested event loop** — replace `asyncio.run()` inside sync method with a proper async call chain or thread-pool offload.
6. **Investigate test suite slowness** — profile `tests/test_agents.py` for blocking I/O or long timeouts.
7. **Add missing API keys to `.env`** — or document that the deployment is OpenRouter-only.
8. **Set `SECRET_KEY` and `HF_TOKEN`** — security + performance.
9. **Investigate code-improver patch corruption** — add patch sanitization/validation before `git apply`.
10. **Address loky semaphore leak** — ensure embedding pipelines call `loky.get_reusable_executor().shutdown()` on app shutdown.

---

*Report generated by systematic inspection of test results, static analysis, server logs, git state, and runtime configuration.*
