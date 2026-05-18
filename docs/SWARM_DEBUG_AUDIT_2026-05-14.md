# Swarm Debug Audit — 2026-05-14

**Branch:** `skyn3t/auto/ui-rebuild`
**Scope:** ~60 modified files + 9 new untracked files across core / agents / studio / web / intelligence / tests.
**Method:** 6 parallel audit agents, each scoped to one layer. Findings deduplicated and severity-ranked below.
**Test suite:** `pytest` runs 663 passed / 0 failed in 5m41s. **Every bug below is real and uncaught by the green suite.**

---

## HIGH severity

### H1 — `python_cli` scaffolds always fail validation
**File:** `skyn3t/agents/stack_templates.py:1743`

`validate_stack_shape` lists `requirements.txt` as a "foreign" file for the `python_cli` stack, but the `python_cli` template plan (line 67) and its manifest generator (line 1593) ship `requirements.txt`. Every `python_cli` scaffold trips a validation flag. The comment on line 1743 explicitly admits the issue and ships it anyway.

**Impact:** Validation noise on every Python CLI project; risk of downstream tooling treating valid scaffolds as broken.

---

### H2 — Build-pattern Skill score uses the loser's failure count
**File:** `skyn3t/memory/meta_agent.py:459`

`_persist_build_pattern_skill` sets `failure_count = worst.failure` (the *losing* shape's failure count) on the *winning* shape's `Skill` record. The score formula becomes `(best.success - worst.failure) / (best.success + worst.failure)` — a meaningless mix of two different shapes. A clear winner can end up with a negative score and be demoted or curated away.

Compounded by `SkillLibrary.upsert` (skill_library.py:255-256) taking `max(new, existing)` on counts, so the bad failure count is sticky once written.

**Impact:** Self-learning loop poisons its own scoreboard; winning shapes get downranked.

---

### H3 — Activity page renders every timestamp as `—`
**File:** `skyn3t/web/ui/src/routes/ActivityPage.tsx:191`

Client does `new Date(e.ts * 1000)` (Unix epoch seconds). Backend (`skyn3t/web/app.py:280`) emits `ts = event.timestamp.isoformat()` — an ISO date string. `"2026-05-14T…" * 1000` is `NaN`, so every row's timestamp falls back to `"—"`.

**Impact:** Activity timeline UI is silently broken; no event has a visible timestamp.

---

### H4 — TIMEOUT retries silently shortened
**File:** `skyn3t/core/orchestrator.py:1602`

`max_attempts_override = task.max_retries + 1` is `min`-ed with the per-class retry budget inside `decide`. For TIMEOUT (budget 3) with default `max_retries=3` (override 4), the cap clamps to 3 — legacy code allowed 4 attempts. Silent behavioral regression.

**Impact:** Tasks that used to recover on attempt 4 now fail at attempt 3.

---

## MEDIUM severity

### M1 — Integration verifier's JSX check always passes
**File:** `skyn3t/agents/integration_verifier.py:319`

```python
any(fe_dir.rglob(p) for p in ("*.jsx", "*.tsx", "*.js", "*.ts"))
```

`rglob` returns a generator object, which is always truthy regardless of whether it produces files. The `has_jsx` branch in `_detect_project` never actually checks for JSX/TS presence. Detection only succeeds through the parallel `has_html` check.

**Impact:** Front-end detection is broken whenever no HTML file exists in the frontend dir.

---

### M2 — Consistency scans flag valid services as hallucinations
**Files:**
- `skyn3t/agents/consistency_engine.py:302-326`
- `skyn3t/agents/consistency_reviewer.py:196-204`

`_detect_services` returns slug tokens (`home_assistant`, `pihole`). The hallucination scan and README-mention scan compare those slugs against display tokens (`home assistant`, `Home Assistant`, `pi-hole`). When the brief legitimately asks for Home Assistant or Pi-hole, every README/code mention is flagged as a hallucination, and the README check reports `home_assistant` is missing even when "Home Assistant" appears.

**Impact:** Consistency loop emits spurious blockers, burns critique rounds on non-issues, may demote valid scaffolds.

---

### M3 — Dead event-color coding in Activity page
**File:** `skyn3t/web/ui/src/routes/ActivityPage.tsx:327-345`

`kindColor` switches on `task_completed`, `stage_completed`, `build_passed`, `task_failed`, `stage_failed`, `build_failed`, `error`, `task_started`, `stage_started`, `agent_message`. None of those are produced by the backend. The actual server emits `thought`, `message`, `learning`, `rag`, `task`, `stage`, `ingest`, `convo`, `project` (`web/app.py:158-196`). Zero overlap — every event falls through to the default dim color.

**Impact:** Success/failure visual coding is entirely non-functional.

---

### M4 — Sandbox tmpdir is a process-singleton under concurrency
**File:** `skyn3t/adapters/llm_client.py:395-412`

`_LLM_CLI_SANDBOX_CWD` is created once per process and reused for every CLI call. The only segmentation between calls is `if stat.st_mtime < started_at`. Under `MAX_CONCURRENT_PROJECTS > 1`, two overlapping calls have similar `started_at` and either misattribute or double-harvest each other's files. The directory is also never pruned and grows unboundedly across long-running backends.

**Impact:** Latent. Safe at current `MAX_CONCURRENT_PROJECTS=1`. Breaks the moment concurrency is enabled.

---

### M5 — `SKYN3T_VERIFY_NPM_INSTALL` default flipped
**File:** `skyn3t/agents/build_verifier.py:287-297`

Previously `SKYN3T_VERIFY_NPM_INSTALL=1` was opt-in for `npm install + npm run build`. New default is **ON**, and the env var now only disables it (`0/false/no/off`). Operators on slow or rate-limited networks now wait for `npm install` on every verify.

**Impact:** Behavior change without release note; CI wall-time and rate-limit risk.

---

### M6 — "Runner crashed" message for intentional fail-fast
**File:** `skyn3t/studio/runner.py:2684-2687` (called from `:985-991`)

`_run_post_code_checks` raises `RuntimeError` on stack-shape mismatch (intentional fail-fast). It propagates through the per-stage loop into the outer `except Exception as exc:` (~line 1430), which marks the project failed with `"Project stopped because the runner crashed."` That's misleading — the run didn't crash, it bailed deliberately.

**Impact:** Operator-facing error message blames "crash" for what is correct behavior.

---

### M7 — `consistency_reviewer` 300s timeout too tight for deep profile
**File:** `skyn3t/studio/runner.py` `_stage_timeout_for` (line 3091)

`medium_stages = {"designer", "architect"}` gets 600s base. `consistency_reviewer` falls through to 300s. Observed runs (per `docs/ISSUE_codegen_reliability.md`): v45-retry's reviewer took 8 minutes for 4 blockers; with deep-profile multiplier 1.4, the effective cap is 420s — still under the observed 480s.

**Impact:** Reviewer stage spuriously times out on legitimate multi-blocker reviews.

---

### M8 — Idle-agent termination races with in-flight shutdown
**File:** `skyn3t/core/orchestrator.py:572-578`

`_terminate_idle_auto_agents` calls `unregister_agent` without awaiting the agent's `shutdown()` coroutine. `unregister_agent` removes the agent from `self.agents` synchronously while shutdown is still running; any in-flight task on that agent is orphaned.

**Impact:** Race condition; possible silent task loss when auto-agents idle out mid-task.

---

### M9 — `fan_out.run_one` hardcoded 60s poll timeout
**File:** `skyn3t/core/orchestrator.py:701`

`range(600)` × 0.1s sleep = 60s total. The runner just bumped reviewer/code stages to 1800s to accommodate multi-minute LLM calls. Fan-out subtasks doing real LLM work will spuriously time out.

**Impact:** Subtasks fail at 60s even when budget is much higher.

---

### M10 — `resolve_artifact_dir` rejects all in-repo paths
**File:** `skyn3t/core/agent.py:557-560`

The walk-up dangerous-path check uses `(parent / "skyn3t" / "core" / "agent.py").exists()` as a SkyN3t-repo marker, then rejects ANY path inside the repo. If a developer's `projects_dir` lives inside the repo (a common dev setup, e.g. `<repo>/data/projects/<proj>/...`), the configured artifact path is silently rejected and falls back to `_agent_scratch`.

Also: `Path()` without an arg resolves to CWD — any process launched with CWD inside the repo trips the rejection for sibling project dirs.

**Impact:** Silent fallback hides operator intent; artifacts land somewhere unexpected.

---

## LOW severity

### L1 — Seed-skills install drops `README.md` into the skills dir
**File:** `examples/skills_seed/README.md`

The install instruction inside the README is literally `cp examples/skills_seed/*.md data/skills/`. The README itself matches the glob. After install, `SkillLibrary._scan()` parses README.md (no frontmatter → `name="untitled"`, `tags=[]`, `score=0.0`). It surfaces in `library.all()` / `summary()` and becomes a curator-deletion candidate after 30 days.

**Impact:** Junk record in the skill registry; clutters dashboards.

---

### L2 — Dead TypeScript-detect branch
**File:** `skyn3t/agents/consistency_reviewer.py:148`

```python
if "typeScript" in brief_lower:
```

`brief_lower` is already lowercased. The literal has a capital `S`. Branch is unreachable.

**Impact:** TypeScript-specific heuristic never fires.

---

### L3 — Partial agent-delete swallowed by UI
**File:** `skyn3t/web/ui/src/routes/AgentsPage.tsx:245-251`

Backend now returns `{ok: false, cleanup: {...}, errors: [...]}` on HTTP 200 when shutdown / registry / spec / override cleanup partially fails (`skyn3t/web/app.py:1100-1180`). The mutation's `onSuccess` calls `onChanged()` + `onClose()` regardless. Partial deletes appear fully successful in the UI.

**Impact:** Operators can't see partial-failure state.

---

### L4 — Backend persistence signals dropped by UI types
**File:** `skyn3t/web/ui/src/lib/client.ts:265-284`

Backend now returns `{persisted, persist_error?}` on enable/disable (`app.py:984-1006`) and PATCH config (`app.py:940-961`). UI types are `{ok?: boolean}` / `any`, so the persistence-failure signal is silently dropped — callers can't surface it.

**Impact:** Persistence failures go unnoticed in the UI.

---

### L5 — `consistency_fix` / `post_fix_ok` written without manifest save
**File:** `skyn3t/studio/runner.py:2719-2725`

`_run_post_code_checks` writes `manifest["consistency_fix"]` and `manifest["consistency_check"]["post_fix_ok"]` but never calls `_save_manifest`. Diagnostic data only persists if the next stage saves; lost if the next stage crashes first.

**Impact:** Forensic data lost on stage-failure transitions.

---

### L6 — Write-only fields in scoreboard / lessons
**Files:**
- `skyn3t/intelligence/build_patterns.py` — `BuildPatternStats.skipped`
- `skyn3t/intelligence/lesson_attribution.py:38` — `LessonStats.neutral`

`skipped` is written on `verdict="skipped"` but never read anywhere (not by `meta_agent._check_build_pattern_biases`, dashboards, `summary()`, or `success_rate`). `neutral` is serialized to/from disk and counted in `total` but **no code path ever increments it**.

**Impact:** Dead schema fields; ongoing maintenance noise.

---

### L7 — `BootVerifier` cleanup may truncate output
**File:** `skyn3t/agents/boot_verifier.py:270-273`

After `_kill_proc`, `server_proc.communicate()` runs while the `_drain_streams` task may still hold readers on the same pipes. A bare `except Exception: pass` masks failures.

**Impact:** Output tail may silently truncate; diagnostics lost on kill.

---

### L8 — `RAGEngine.initialize()` in code_agent silently no-ops
**File:** `skyn3t/agents/code_agent.py:868-915`

Inline `RAGEngine.initialize()` depends on `skyn3t.rag.rag_engine` import. Bare `except Exception: pass` swallows ImportError/AttributeError quietly. Comment promises "outer-loop self-learning" while the code silently does nothing on first run if RAG isn't wired.

**Impact:** Silent feature disablement; behavior diverges from comment.

---

### L9 — `_strip_cli_prelude` gate may reject Markdown
**File:** `skyn3t/agents/code_agent.py:1037-1050`

`_syntax_ok` rejects content where ``body.startswith("```") or body.endswith("```")`` upfront for ALL file types — including `.md` and `.txt` where opening/closing with a fence may be legitimate.

**Impact:** Some legitimate Markdown content rejected.

---

### L10 — Reviewer duplicates planner's `_brief_implies_code`
**File:** `skyn3t/agents/reviewer.py:117-125`, `:365-394`

Mirrors `_brief_implies_code` from `studio/planner.py` "so the reviewer doesn't import from studio.planner." Duplicated logic can drift from the canonical planner version over time.

**Impact:** Future drift risk; not a bug today.

---

### L11 — Duplicate `TaskRequest` imports in runner
**File:** `skyn3t/studio/runner.py:2065, 2097, 2128`

Each `_run_*_verifier` method imports `TaskRequest` locally despite the module-level import on line 21.

**Impact:** Harmless. Noise.

---

### L12 — `acp_server.py` sessionId coercion silently breaks numeric IDs
**File:** `skyn3t/integrations/acp_server.py:236-239`

`_handle_session_prompt` now coerces `sessionId` to a string before refusing empties. Previously, a numeric sessionId (if any host sent one) would hit the dict lookup directly. Now `str(123)` → `"123"` and `_sessions.get("123")` always misses, so every prompt returns `refusal`.

**Impact:** Edge case; only matters if an ACP client uses non-string session tokens (spec allows opaque).

---

### L13 — `messaging.py` redundant `or ""` after `os.getenv(KEY, "")`
**File:** `skyn3t/integrations/messaging.py:363, 469, 584`

No-ops. `os.getenv` with a string default never returns None.

**Impact:** Cosmetic.

---

### L14 — `model_router._load_overrides()` re-reads JSON every call
**File:** `skyn3t/core/model_router.py:148`

No caching on the overrides file. Re-parsed on every `tier_for_stage` call.

**Impact:** Repeated I/O during busy runs.

---

### L15 — Orchestrator `merge_event_payload` correlation id discarded
**File:** `skyn3t/core/agent.py:498-518`

`BaseAgent.request()` uses `merge_event_payload(payload)` to inject correlation context, but `MessageBus.request()` immediately generates its own `correlation_id` and overrides it. Plumbing is unused.

**Impact:** Cosmetic; correlation context never flows through.

---

### L16 — `model_router.brief` parameter is documented dead
**File:** `skyn3t/core/model_router.py:157`

Comment: `# Not used today`. Dead parameter on a public API.

**Impact:** Cosmetic.

---

## Dead-on-arrival code (added but no callers)

### A2A messaging / delegation layer
The following were added across `core/orchestrator.py`, `core/agent.py`, `core/events.py`, `core/messaging.py` but are not referenced anywhere outside their own module:

- `Orchestrator.spawn_subordinate`
- `Orchestrator.delegate_task`
- `Orchestrator.fan_out`
- `Orchestrator.get_subordinates`
- `Orchestrator.get_reporting_chain`
- `BaseAgent.request` / `BaseAgent.on_message` — `on_message` is never dispatched by any inbox pump
- `EventType.AGENT_CONVERSATION_STARTED` / `_TURN` / `_ENDED` — never published
- `AgentStatus.DISABLED` — never set or read

**Recommendation:** Either wire it up or delete it. Currently it's surface area without value.

---

### Missing migration for new `Agent` columns
**File:** `skyn3t/core/models.py`

New columns `role`, `reports_to`, `lifecycle` added to the SQLAlchemy `Agent` model with `nullable=True`, but no Alembic migration script was added. Existing databases will fail to read these columns until `ALTER TABLE` runs.

**Recommendation:** Add migration before this branch merges.

---

## Untested critical paths (pytest green is misleading)

| Module | LOC | Test status |
|---|---|---|
| `agents/targeted_fix.py` | 452 | **Zero tests.** Called from 8 sites in `studio/runner.py`. |
| `agents/consistency_reviewer.py` | 342 | **Zero tests.** `tests/test_pipelines.py:1055` stubs it as a no-op shim that always returns `verdict=pass`. |
| `agents/service_brand_kit.py` | 267 | **Zero tests.** Called from `code_agent.py:1119`. |
| `agents/boot_verifier.py` | 895 | ~3 helper tests on ~50 LOC. The 700+ LOC `execute` / `_boot_and_wait` / `_health_check` / stream-draining path is untested. |
| `agents/product_categories.py` | 318 | Partial — only `expand_sparse_brief` is exercised. `detect_category`, `enrich_brief`, `defaults_for` have no direct assertions. |

A regression that broke `apply_targeted_fix` or `ConsistencyReviewerAgent.execute` would not turn pytest red.

---

## Test-quality smell

`tests/test_pipelines.py` has **96 occurrences of mocks/shims across 27 tests.** Many tests build minimal `class XStageAgent` shims that return hard-coded `TaskResult(success=True, output={"verdict": "pass"})`. Example: `ConsistencyReviewerStageAgent` at line 1055 is a no-op that always passes — pipeline tests cannot catch the actual consistency reviewer producing wrong findings or crashing. They only verify orchestration plumbing.

`docs/ISSUE_codegen_reliability.md` failure classes vs. test coverage:
- **Class 1 (missing router mount):** `test_consistency_engine.py::test_consistency_flags_unmounted_router` covers this with a real fixture. ✓
- **Class 2 (frontend build failure):** No test exercises the proposed in-loop vite-build dry-run.
- **Class 3 (sandbox content loss):** No tests exist for sandbox harvesting in `test_llm_client.py`.

---

## Stale documentation

`docs/ISSUE_codegen_reliability.md` Class 3 (sandbox content lost) is **already mitigated** — `skyn3t/adapters/llm_client.py:559` calls `_collect_sandbox_artifacts` and appends them to stdout via `// === <relpath> ===` markers. The doc still describes the pre-fix state.

Residual hazards from the partial fix:
- The singleton-cwd concurrency issue (M4 above).
- The `// === path ===` comment marker is JS/TS-syntax-only — invalid when the appended content is Python or HTML; downstream parsers that try to interpret merged stdout as a single file body will break.

**Recommendation:** Update the doc to reflect the partial fix and the residual concerns.

---

## Suggested triage order

1. **H1** — every python_cli scaffold is failing validation right now
2. **H2** — build-pattern scoreboard is producing wrong winners; corrupts self-learning
3. **H3** — UI never shows timestamps
4. **H4** — silent retry regression
5. **M1** — JSX detection broken
6. **M2** — false hallucination warnings poisoning the consistency loop
7. **M3** — dead UI coloring
8. Dead-code cleanup (A2A layer + DB migration)
9. Backfill tests for `targeted_fix.py` / `consistency_reviewer.py` / `service_brand_kit.py`
10. Update `docs/ISSUE_codegen_reliability.md` to reflect current state

The pre-existing open issue's mount-checker proposal (`docs/ISSUE_codegen_reliability.md` proposal #1) is independent and orthogonal to this audit.
