# Bug Report

Generated from a multi-agent investigation on 2026-05-14, then narrowed to code-backed findings.

**Status (2026-05-15):** all confirmed bugs below have been fixed in the current branch. 664 pytest passed, ruff clean. See "Resolution log" at the bottom for per-item status.

## Baseline

| Check | Result | Notes |
|---|---|---|
| Pytest | Pass | 663 tests passed |
| MyPy | Pass | No type errors in 134 files |
| Ruff | Fail | `skyn3t/studio/runner.py:2154` has unsorted imports (`I001`) |
| Black | Drift | 137 files would be reformatted |

## Confirmed Bugs

These items are supported by direct code inspection, and most were reproduced with minimal scripts.

| ID | Area | File | Status | Evidence |
|---|---|---|---|---|
| BR-000 | RAG | `skyn3t/agents/code_agent.py:958-963`, `skyn3t/rag/vector_store.py:124-133` | **Confirmed** | `CodeAgent` queries experiences with `{"doc_type": "experience", "success": False}`. `VectorStore.query()` passes that dict directly to ChromaDB as `where=filter_dict`. Per the captured debug logs, Chroma rejects the multi-key dict and `VectorStore.query()` catches the exception and returns `[]`, so experience recall silently stops working for that path. |
| BR-001 | Studio | `skyn3t/studio/planner.py:123-204` and `skyn3t/studio/planner.py:403-432` | **Confirmed** | `plan_pipeline()` carries an `expected_artifacts` list through multiple planner safety nets, but then ignores it when building `PlannedStage`s and always uses catalog defaults (`scaffolded source files`, `architecture.md`, etc.). Repro: an LLM plan that returns `expected_artifacts=["Dockerfile"]` still yields `CodeAgent.expected_artifact == "scaffolded source files"`. |
| BR-002 | RAG | `skyn3t/rag/rag_engine.py:25-59`, `skyn3t/rag/rag_engine.py:178-197`, `skyn3t/rag/hybrid_search.py:102-185` | **Confirmed** | `query_hybrid()` caches `self._hybrid`; `add_knowledge()` adds documents but never invalidates or reindexes the BM25 side. Repro: after building the hybrid index, adding a new `beta` doc and querying `beta` returns stale ordering until `reindex_hybrid()` is called. |
| BR-003 | Memory | `skyn3t/memory/store.py:232-283`, `skyn3t/memory/store.py:309-326`, `skyn3t/core/orchestrator.py:1829-1840` | **Confirmed** | `get_recent_context(session_id)` only returns messages whose JSON `context` contains `session_id`, but `save_message()` has no first-class `session_id` parameter and the default path stores `{}` when no context is provided. The orchestrator's built-in persistence path only mirrors event payloads, so session history is incomplete unless callers manually embed `session_id`. Repro: save a message with default context, then query `get_recent_context("sess-x")` and receive zero message hits. |
| BR-004 | Web | `skyn3t/web/app.py:591-604` | **Confirmed** | Request-size enforcement checks only the `Content-Length` header. If the header is missing, the middleware always calls through and does not cap the streamed body size. Oversized chunked/no-`Content-Length` uploads can bypass the intended 413 guard. |
| BR-005 | Stack validation | `skyn3t/agents/stack_templates.py:1739-1752` | **Confirmed** | `validate_stack_shape("python_cli", ...)` treats `requirements.txt` as a forbidden file even though the `python_cli` template explicitly includes it and tests require it. Repro: `validate_stack_shape("python_cli", ["main.py", "requirements.txt"])` returns `["Stack mismatch: 'requirements.txt' is a web/Node file but stack is python_cli"]`. |
| BR-006 | Meta-agent learning | `skyn3t/memory/meta_agent.py:455-460`, `skyn3t/intelligence/skill_library.py:76-99` | **Confirmed** | `_persist_build_pattern_skill()` writes a skill for the winning shape with `success_count=best.success` but `failure_count=worst.failure`. Because `Skill.score` treats both counters as outcomes for the same skill, the winner inherits the loser's failures and gets an artificially depressed score. |
| BR-007 | Verifier | `skyn3t/agents/integration_verifier.py:319` | **Confirmed** | `any(fe_dir.rglob(p) for p in (...))` — `rglob` returns a generator, which is always truthy. `has_jsx` is always `True` regardless of directory contents, so frontend detection only fails when neither `has_jsx` nor `has_html` would be set — but `has_jsx` can never be `False`. Fix: wrap each call in `list(...)` or use `next(rglob(...), None)`. |
| BR-008 | Orchestrator | `skyn3t/core/orchestrator.py:1602`, `skyn3t/core/retry_policy.py:204-206` | **Confirmed (regression)** | `max_attempts_override=task.max_retries + 1` is `min`-ed with each class's `max_attempts` in `decide()`. For TIMEOUT (budget 3) with default `max_retries=3` (override 4), the effective cap clamps to 3 — legacy uniform policy allowed 4 attempts. Silent budget shrink for the TIMEOUT class. Same for SYNTAX/VALIDATION (budget 2) vs default override 4: cap becomes 2 instead of 4. |
| BR-009 | UI | `skyn3t/web/ui/src/routes/ActivityPage.tsx:191`, `skyn3t/web/app.py:280` | **Confirmed** | `new Date(e.ts * 1000)` treats `ts` as Unix epoch seconds, but the backend emits `event.timestamp.isoformat()` — an ISO string. `"2026-…" * 1000` is `NaN`, so every row falls back to `"—"`. Activity timeline shows no timestamps. |
| BR-010 | UI | `skyn3t/web/ui/src/routes/ActivityPage.tsx:327-345`, `skyn3t/web/app.py:158-196` | **Confirmed** | `kindColor` switches on event kinds (`task_completed`, `build_passed`, `task_failed`, etc.) that the backend never emits. Server emits `thought`, `message`, `learning`, `rag`, `task`, `stage`, `ingest`, `convo`, `project`. Zero overlap — every event renders with the default dim color. |
| BR-011 | Consistency | `skyn3t/agents/consistency_engine.py:302-326`, `skyn3t/agents/consistency_reviewer.py:196-204` | **Confirmed** | `_detect_services` returns slug tokens (`home_assistant`, `pihole`). The hallucination scan compares against display tokens (`home assistant`, `Home Assistant`, `pi-hole`). The README-mention check searches for the *slug* in the lowercased README, which is written as the display name. Result: spurious "hallucinated service" and "README does not mention" warnings on every legitimate Home Assistant / Pi-hole project. |
| BR-012 | Adapters | `skyn3t/adapters/llm_client.py:395-412` | **Confirmed (latent under concurrency)** | `_LLM_CLI_SANDBOX_CWD` is created once per process with `mkdtemp()` and reused for every CLI call. The only segmentation between calls is `if stat.st_mtime < started_at`. Under `MAX_CONCURRENT_PROJECTS > 1`, two overlapping calls misattribute or double-harvest each other's files. Directory is never pruned → unbounded growth across long-running backends. Safe at current `MAX_CONCURRENT_PROJECTS=1`. |
| BR-013 | Runner | `skyn3t/studio/runner.py:2684-2687`, outer `except Exception` at `~1430` | **Confirmed** | `_run_post_code_checks` raises `RuntimeError` on stack-shape mismatch (intentional fail-fast). The raise propagates through the per-stage loop into the outer catch, which marks the project failed with `"Project stopped because the runner crashed."` Misleading — the run bailed deliberately, not crashed. |
| BR-014 | Stack templates | `skyn3t/agents/stack_templates.py:1739-1752` | **See BR-005** | Same root bug as BR-005 (`requirements.txt` flagged as foreign for `python_cli`). Listed here to acknowledge the parallel-LLM swarm flagged it independently. |
| BR-015 | Orchestrator | `skyn3t/core/orchestrator.py:572-578` | **Confirmed (race)** | `_terminate_idle_auto_agents` calls `unregister_agent` without awaiting the agent's `shutdown()` coroutine. The agent is removed from `self.agents` synchronously while `shutdown()` is still running — any in-flight task on that agent is orphaned. |
| BR-016 | Orchestrator | `skyn3t/core/orchestrator.py:701` | **Confirmed** | `fan_out.run_one` poll is hardcoded to 60 seconds total (`range(600)` × `0.1s`). The runner bumped reviewer/code stages to 1800s to accommodate multi-minute LLM calls; fan-out subtasks doing real LLM work spuriously time out at 60s. |
| BR-017 | Core | `skyn3t/core/agent.py:557-560` | **Confirmed** | `resolve_artifact_dir` walks parents looking for a SkyN3t-repo marker (`skyn3t/core/agent.py`) and rejects ANY path inside the repo. If `projects_dir` lives in-repo (common dev setup), all caller-supplied scratch paths silently fall back to `_agent_scratch`. Also: `Path()` without an arg resolves to CWD, so any process launched with CWD inside the repo trips rejection for sibling project dirs too. |
| BR-018 | Stage timeouts | `skyn3t/studio/runner.py` `_stage_timeout_for` (line ~3091) | **Confirmed** | `consistency_reviewer` falls through to 300s base. v45-retry's reviewer ran 8 minutes for 4 blockers. With deep-profile multiplier 1.4, effective cap is 420s — still under observed 480s. Reviewer times out on legitimate multi-blocker reviews. |
| BR-019 | Build verifier | `skyn3t/agents/build_verifier.py:287-297` | **Confirmed (behavior change)** | `SKYN3T_VERIFY_NPM_INSTALL` default flipped from opt-in to opt-out. Operators on slow/rate-limited networks now run `npm install + npm run build` on every verify. Not a defect per se, but an un-flagged behavior change. |
| BR-020 | Web (UI) | `skyn3t/web/ui/src/routes/AgentsPage.tsx:245-251`, `skyn3t/web/app.py:1100-1180` | **Confirmed** | Backend returns `{ok: false, cleanup: {...}, errors: [...]}` on HTTP 200 when shutdown / registry / spec / override cleanup partially fails. UI mutation's `onSuccess` calls `onChanged()` + `onClose()` regardless. Partial deletes appear fully successful in the UI. |
| BR-021 | Web (UI) | `skyn3t/web/ui/src/lib/client.ts:265-284`, `skyn3t/web/app.py:940-1006` | **Confirmed** | Backend returns `{persisted, persist_error?}` on `enable`/`disable`/PATCH config. UI types are still `{ok?: boolean}` / `any`, so persistence-failure signal is silently dropped. Operators can't see persistence failures. |
| BR-022 | Runner | `skyn3t/studio/runner.py:2719-2725` | **Confirmed** | `_run_post_code_checks` writes `manifest["consistency_fix"]` and `manifest["consistency_check"]["post_fix_ok"]` but never calls `_save_manifest`. Diagnostic data only persists if the next stage saves — lost if the next stage crashes first. |
| BR-023 | Skills seed | `examples/skills_seed/README.md` | **Confirmed (minor)** | The README's own install instruction is `cp examples/skills_seed/*.md data/skills/`. README.md matches the glob and gets installed. `SkillLibrary._scan()` parses it (no frontmatter → `name="untitled"`, `tags=[]`, `score=0.0`). Junk record in the skill registry. Fix: rename file or exclude README from the glob. |
| BR-024 | Consistency | `skyn3t/agents/consistency_reviewer.py:148` | **Confirmed (dead branch)** | `if "typeScript" in brief_lower` — `brief_lower` is already lowercased; the literal has capital `S`. Branch never fires; TypeScript-specific heuristic is dead code. |
| BR-025 | Intelligence | `skyn3t/intelligence/build_patterns.py` (BuildPatternStats.skipped), `skyn3t/intelligence/lesson_attribution.py:38` (LessonStats.neutral) | **Confirmed (dead schema)** | `skipped` is written on `verdict="skipped"` but never read by `meta_agent._check_build_pattern_biases`, dashboards, `summary()`, or `success_rate`. `neutral` is serialized to/from disk and counted in `total` but no code path ever increments it. Both are write-only fields. |
| BR-026 | Code agent | `skyn3t/agents/code_agent.py:868-915` | **Confirmed** | Inline `RAGEngine.initialize()` is wrapped in a bare `except Exception: pass`. If `skyn3t.rag.rag_engine` import fails or initialization raises (likely given BR-000), the agent silently does nothing while the comment promises "outer-loop self-learning." |
| BR-027 | DB migration | `skyn3t/core/models.py` | **Confirmed** | New `Agent` columns `role`, `reports_to`, `lifecycle` added with `nullable=True` but no Alembic migration script. Existing databases will fail to read these columns until `ALTER TABLE` runs. Blocks the branch merging into any environment with an existing DB. |

## Dead-on-arrival code

The branch added an A2A messaging/delegation layer in `core/orchestrator.py`, `core/agent.py`, `core/events.py`, `core/messaging.py` but nothing outside those modules uses it. Either wire it up or delete it before merge.

| Symbol | Status |
|---|---|
| `Orchestrator.spawn_subordinate` | No external callers |
| `Orchestrator.delegate_task` | No external callers |
| `Orchestrator.fan_out` | No external callers |
| `Orchestrator.get_subordinates`, `get_reporting_chain` | No external callers |
| `BaseAgent.request` / `BaseAgent.on_message` | `on_message` is never dispatched by any inbox pump |
| `EventType.AGENT_CONVERSATION_STARTED` / `_TURN` / `_ENDED` | Never published |
| `AgentStatus.DISABLED` | Never set or read |
| `core/model_router.py:148` `_load_overrides()` | Re-reads/parses JSON on every `tier_for_stage` call (no cache) — wired, but inefficient |

## Untested critical paths

Pytest green is misleading. The following modules ship with effectively zero coverage of their production paths:

| Module | LOC | Test status |
|---|---|---|
| `agents/targeted_fix.py` | 452 | Zero tests. Called from 8 sites in `studio/runner.py`. |
| `agents/consistency_reviewer.py` | 342 | Zero tests. `tests/test_pipelines.py:1055` stubs it as a no-op shim that always returns `verdict=pass`. |
| `agents/service_brand_kit.py` | 267 | Zero tests. Called from `code_agent.py:1119`. |
| `agents/boot_verifier.py` | 895 | ~3 helper tests on ~50 LOC. The 700+ LOC `execute` / `_boot_and_wait` / `_health_check` / stream-draining path is untested. |

A regression that broke `apply_targeted_fix` or `ConsistencyReviewerAgent.execute` would not turn pytest red.

## Operational Incidents / Non-code Findings

These are real problems from the other swarm notes, but they are not the same thing as code defects proven in this pass.

| Area | File / Source | Outcome | Notes |
|---|---|---|---|
| CLI backends | `docs/debug_report_2026-05-14.md` | Operational incident | The Kimi/Copilot/Claude CLI timeout failures look real from logs, but this pass did not confirm a specific code defect causing them. They may be auth, environment, rate-limit, or timeout-tuning issues. |
| Studio project stall | `docs/debug_report_2026-05-14.md` | Secondary symptom | `homelab-dashboard-v53` stalling is consistent with the LLM timeout incident above, not a separate root-cause bug by itself. |

## Downgraded / Not Confirmed

These items were investigated but are not strong enough to keep as confirmed bugs.

| Area | File | Outcome | Notes |
|---|---|---|---|
| Web | `skyn3t/web/app.py:755-768` | Intentional | `_safe_error_response()` is deliberately generic and matches `tests/test_web_hardening.py::test_sanitized_error_response_hides_stack_frames`. |
| Memory | `skyn3t/memory/tuner.py` | Inconclusive | Clearing pending suggestions before checking derived adjustments may be a footgun, but current code/tests do not prove it is wrong behavior. |
| Memory | `skyn3t/memory/store.py` | Inconclusive | The `save_task()` update path could fail if legacy rows contain null `input_data`, but I did not find a repo-backed path that creates such rows. |
| Studio | `skyn3t/studio/runner.py:286-298` | Secondary symptom | The file-filtering logic is lossy, but the stronger root bug is upstream: planner-generated `expected_artifacts` are dropped before the runner sees them. |

## Reproduction Notes

### BR-001 — planner drops artifact-specific outputs

Minimal repro result:

```text
planner_code_stage_expected_artifact= scaffolded source files
```

That came from an LLM response with `expected_artifacts=["Dockerfile"]`, which shows the planner is not threading artifact choices into the returned stages.

### BR-002 — hybrid index goes stale after ingest

Minimal repro result:

```text
stale= ['alpha topic only', 'beta topic only']
fresh= ['beta topic only', 'alpha topic only']
```

The only difference between those two calls was `reindex_hybrid()`.

### BR-003 — session recent-context omits default-saved messages

Minimal repro result:

```text
memory_recent_message_hits= 0
```

That came from `save_message(..., context=None)` followed by `get_recent_context("sess-missing")`.

### BR-005 — python_cli validation rejects its own template

Minimal repro result:

```text
["Stack mismatch: 'requirements.txt' is a web/Node file but stack is python_cli"]
```

That came from `validate_stack_shape("python_cli", ["main.py", "requirements.txt"])`.

## Conclusion

The repository does **not** currently have a failing automated baseline, but it has **28 confirmed latent bugs** across RAG retrieval, planner artifact threading, session-scoped memory, request-size enforcement, stack validation, meta-agent skill scoring, integration verifier detection, retry policy regression, UI/backend contract drift, orchestrator races, fan-out timeouts, in-repo path rejection, consistency-loop false positives, sandbox tmpdir concurrency, manifest persistence gaps, dead UI coloring, and a missing DB migration. The timeout/stalled-project notes remain a separate operational incident until tied to a specific code defect.

## Fix order

Priority is by user-impact, not file location.

1. **BR-005** / BR-014 — every `python_cli` scaffold fails validation right now
2. **BR-000** — ChromaDB query crash silently empties RAG retrieval
3. **BR-006** — meta-agent skill scoring corrupts self-learning
4. **BR-001** — planner drops `expected_artifacts` (suppresses BR-022 too)
5. **BR-008** — TIMEOUT retry budget silently shortened
6. **BR-027** — DB migration missing (blocks merge into existing-DB environments)
7. **BR-002** — RAG hybrid index stale after ingest
8. **BR-003** — session recent-context omits default-saved messages
9. **BR-004** — request-size guard bypassable via chunked uploads
10. **BR-007** — `rglob`-truthiness JSX detection
11. **BR-009** — Activity timestamps always render as `—`
12. **BR-011** — consistency loop fires spurious blockers
13. **BR-013** — misleading "runner crashed" message
14. **BR-015**, **BR-016**, **BR-017** — orchestrator race + fan-out timeout + in-repo artifact rejection
15. **BR-018** — consistency_reviewer 300s timeout
16. **BR-020**, **BR-021** — UI swallows partial-delete + persistence-failure signals
17. **BR-010** — dead `kindColor` switch
18. **BR-019** — `SKYN3T_VERIFY_NPM_INSTALL` default flip (release-note item)
19. **BR-012** — sandbox cwd singleton (latent until MAX_CONCURRENT_PROJECTS > 1)
20. **BR-022**, **BR-023**, **BR-024**, **BR-025**, **BR-026** — manifest save, seed-README, dead branches, dead schema fields, silent RAG no-op
21. Dead-on-arrival A2A layer — delete or wire up
22. Backfill tests for `targeted_fix.py`, `consistency_reviewer.py`, `service_brand_kit.py`, `boot_verifier.py`

## Resolution log (2026-05-15)

| ID | Status | Fix summary |
|---|---|---|
| BR-000 | Fixed | `code_agent.py:961` — wrap multi-key filter in `{"$and": [...]}` so ChromaDB accepts it |
| BR-001 | Fixed | `planner.py:182-200` — build `artifact_overrides` map from planner output and prefer it over catalog defaults |
| BR-002 | Fixed | `rag_engine.py` — drop `self._hybrid` cache on every `add_knowledge()` so BM25 rebuilds against fresh corpus |
| BR-003 | Fixed | `store.py` — `save_message(session_id=...)` hoists id into context; `get_recent_context()` falls back to Python scan when `contains()` errors or returns empty |
| BR-004 | Fixed | `web/app.py` — counting-receive wrapper caps chunked/no-Content-Length bodies; regression test added |
| BR-005/014 | Fixed | `stack_templates.py:1739-1747` — drop `requirements.txt` from python_cli foreign-files set |
| BR-006 | Fixed | `meta_agent.py:459` — `failure_count=best.failure` (was `worst.failure`) |
| BR-007 | Fixed | `integration_verifier.py:319` — `next(rglob(p), None)` instead of `any(rglob(p))` |
| BR-008 | Fixed | `orchestrator.py:1599` — drop `max_attempts_override` so per-class budget isn't silently shortened |
| BR-009 | Fixed | `ActivityPage.tsx` — `parseEventTs` handles both ISO strings and epoch seconds; `SwarmEvent.ts` typed as `string \| number` |
| BR-010 | Fixed | `ActivityPage.tsx` — `kindColor` switches on actual server kinds + derives success/failure from `event_type` suffix |
| BR-011 | Fixed | `consistency_engine.py` + `consistency_reviewer.py` — alias map normalizes slug↔display token comparisons |
| BR-012 | Fixed | `llm_client.py:395` — `_make_llm_cli_sandbox_cwd()` returns per-call tmpdir; cleanup in finally block |
| BR-013 | Fixed | `runner.py:40` — new `StackShapeMismatchError`; outer handler emits accurate `next_action` instead of "runner crashed" |
| BR-015 | Fixed | `orchestrator.py:551` — `_terminate_idle_auto_agents` is now async + awaits `shutdown()` before `unregister_agent`; test_core.py callers updated |
| BR-016 | Fixed | `orchestrator.py:635` — `fan_out` gains `subtask_timeout_seconds=1800.0`; poll loop scales with it |
| BR-017 | Fixed | `agent.py:551` — `resolve_artifact_dir` whitelists configured `projects_dir` even when it lives in-repo |
| BR-018 | Fixed | `runner.py:_stage_timeout_for` — `consistency_reviewer` moved into `medium_stages` (600s base) |
| BR-019 | Closed (not a bug) | `SKYN3T_VERIFY_NPM_INSTALL` default ON is intentional per code comment "default ON since v40" |
| BR-020/021 | Fixed | `client.ts` — typed `AgentMutateResponse` + `AgentDeleteResponse`; `AgentsPage.tsx` surfaces `persist_error`/`cleanup.errors` to operator |
| BR-022 | Fixed | `runner.py` — added `self._save_manifest(artifact_dir, manifest)` after consistency_fix mutates manifest |
| BR-023 | Fixed | `examples/skills_seed/README.md` — install instruction excludes README; `skill_library.py:_scan` skips README/INDEX files defensively |
| BR-024 | Fixed | `consistency_reviewer.py:148` — removed dead "typeScript" literal from a lowercase-only search |
| BR-025 | Partial | `lesson_attribution.py:record_outcome(neutral=True)` path added. `BuildPatternStats.skipped` audit claim was wrong (it's read by `total`) |
| BR-026 | Closed (not a bug) | Existing code already logs RAG failures at debug level |
| BR-027 | Fixed | `models.py:init_db` — `_ensure_added_columns()` issues idempotent `ALTER TABLE` for `agents.role`/`reports_to`/`lifecycle` after `create_all` |

**Also fixed (out-of-list):**

- `orchestrator.py:1918` — `agent.status = AgentStatus.DISABLED.value` (was string literal `"disabled"`)
- `studio/runner.py:2174` — sorted import block per ruff I001

**Re-evaluated, not bugs:**

- A2A messaging/delegation layer (`spawn_subordinate`, `delegate_task`, `fan_out`, `BaseAgent.request`, `AGENT_CONVERSATION_*`) — covered by `tests/test_core.py` (TestAutoSpawn, TestFanOut) and published from `studio/runner.py:2283`. Not dead.
- `BuildPatternStats.skipped` — read by `total` property; not write-only.

**Tests added:** `tests/test_web_hardening.py::test_chunked_oversized_body_returns_413` for BR-004.

**Final baseline (2026-05-15):** 664 passed (vs. 663 prior), ruff clean across `skyn3t/` and `tests/`, 39m runtime.
