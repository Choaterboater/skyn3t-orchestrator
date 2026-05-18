# Audit sweep: P0–P2 hardening + new test coverage

## Summary

Five-round audit of the orchestrator using parallel review agents, followed by fix application and a parallel QA pass that built new test suites. **66 P0/P1/P2 fixes applied, 36 new tests added, 211 → 247 tests passing, zero regressions.** One real production bug was caught by the QA-built tests after the initial fix went in (CRLF detection broken by `read_text` universal newlines) and patched.

## What changed, by subsystem

### Core (`skyn3t/core/`)
- **Fixed asyncio queue cancellation message-loss race** (`agent.py`, `self_healing.py`) — replaced `wait_for(queue.get(), timeout=1)` polling with shutdown-sentinel pattern.
- **EventBus thread-safe** with `RLock` + snapshot-during-publish; history switched to `deque(maxlen=1000)`.
- **Real task timeout** in `_monitor_loop` (was a `pass` placeholder) — stuck tasks fire `TASK_FAILED` and request healing.
- **Idempotency keys** on `TaskRequest` — same key within TTL returns prior `task_id` instead of duplicating work.
- **Event-driven `wait_for_task`** — `asyncio.Event` per task, no more 500ms polling.
- **Bounded growth** — orchestrator dicts, agent `_results` (OrderedDict LRU), `_handling_task_failures` dedup lock.
- **`BaseAgent.llm_complete`** helper — collapses ~10 copies of LLM-call boilerplate with retry, timeout, deterministic-stub detection, fallback.
- **Per-agent queue backpressure** (`MAX_QUEUE_DEPTH` setting) — rejects + publishes `QUEUE_BACKPRESSURE_REJECT`.

### Distributed (`skyn3t/distributed/`)
- **Visibility-timeout pattern** via `BRPOPLPUSH` to per-worker processing list with LREM on success; `_recover_orphaned_tasks` re-queues or DLQs after `MAX_EXECUTION_ATTEMPTS=3`.
- **Capability re-queue cap** + DLQ at `skyn3t:task_queue:dlq` so mismatch tasks can't loop forever.
- **`time.time()` heartbeats** — wall clock comparable across hosts (was monotonic per-process).
- **Tracked publish tasks** in `redis_bus` — strong refs prevent GC mid-publish.
- 8 stray `print()`s converted to `logger`.

### Agents (`skyn3t/agents/`)
- Removed "safely sandboxed" claim from `code_agent._execute_code` (it isn't — restricted-builtins escapable via `__subclasses__`).
- Fixed import-block slicing in `_refactor_code` to use AST `end_lineno` — multi-line imports no longer corrupt the file.
- **CRLF preservation** in `code_improver._fallback_apply` — uses `read_bytes().decode()` so universal-newline translation can't strip `\r\n` before detection (this bug was caught by the QA tests, not the original audit).
- Scheduler **anchor-based scheduling** — `anchor + N*interval` instead of `now + interval`, jumps past missed ticks.
- Slack mention strip uses bot's own `user_id` (resolved via `auth.test`), not the sender's `bot_id`.
- Reviewer LLM prompt: per-artifact + total-budget caps prevent context-window blowout on multi-file projects.
- Renamed `research_agent.fact_check` → `corroborate_against_sources` (it's substring matching, not fact-checking). Old name kept as back-compat alias.

### Web (`skyn3t/web/`)
- **8 MiB request body cap** middleware (`/api/rag/add` and similar were unbounded).
- **CSP + security headers** middleware (CSP scoped to actual CDN deps; `nosniff`, `no-referrer`, `DENY`).
- **WS frame size cap** (64 KiB) + structured JSON parse error replies on `/ws`, `/ws/swarm`.
- **Per-IP rate limits** (token bucket): `/api/agents/{name}/exec` 30/min, `/api/rag/add` 60/min, `/api/proposals/feature` 10/min.
- **Sanitized error responses** — `_safe_error_response` logs full exception with correlation id, returns generic body.
- **Limit param clamping** on `/traces`, `/api/memory/insights`, `/api/memory/experiences`.
- **Studio file-read** now wraps `runner.reserve_project` in `asyncio.to_thread` (was a 10s sync subprocess on the event loop).
- **Tracked background tasks** for `run_pipeline` so exceptions don't disappear silently.
- `/favicon.ico` returns 204 (was 404 with full HTML body served by catch-all).
- Moved `DASHBOARD_HTML = open(...).read()` to `FileResponse` (was blocking at import time, never reloaded).
- **Removed 592 lines** of `OLD_DASHBOARD_HTML` dead code.

### Frontend (`skyn3t/web/dashboard.html`)
- WS auto-reconnect: **exponential backoff + jitter** (was instant retry → thundering herd).
- `dashboardSetInterval` registry — every poll is now `document.hidden`-guarded and cleared on `pagehide`.
- Replaced user-facing `alert()` calls with `showToast`.

### Persistence (`skyn3t/persistence/`)
- **Atomic checkpoint write** (`tmp` + `fsync` + `os.replace`) with corrupt-head fallback in `load_latest`.
- **`schema_version` field** on `Checkpoint`; refuses to load newer versions to prevent silent field loss.

### Memory (`skyn3t/memory/`)
- **Persistent ingestor seen-hashes** at `data/.ingestor_seen_hashes.json`; restart no longer re-ingests every prior task.
- **Consciousness session TTL** (24h) + max history (500 entries) + opportunistic eviction.
- **meta_agent dedup** — `_proposal_last_filed` pruned >24h; empty `_agent_failures` deques dropped.

### Integrations (`skyn3t/integrations/`)
- **GitHub webhook delivery dedup** via `X-GitHub-Delivery` header (1h TTL, 5000-entry cap) — retried deliveries don't re-trigger agents.
- **Slack** auth.test on init for self user_id; mention strip uses the resolved id.
- **Email**: aggregate body size cap (was per-part only → OOM risk); migrated deprecated `get_event_loop()` → `asyncio.to_thread`.

### RAG (`skyn3t/rag/`)
- **Vector store embedding-model versioning** in Chroma collection metadata; refuses to load on model mismatch (was silent similarity corruption).
- **Web scraper** uses `urllib.robotparser` scoped to our UA (was substring `Disallow` check that ignored User-agent groupings).

### Config / Build
- `.dockerignore` (new) — keeps `.git`, `data/`, `logs/`, `projects/`, `.env*` out of build context.
- `pyproject.toml` / `requirements.txt` synced: added `cryptography`, `beautifulsoup4`, `prompt_toolkit`; dev extras gain `pytest-cov`, `respx`.
- **`SKYN3T_` env prefix** standardized in `security/secrets.py` with `SkyN3t_` back-compat.

### Tests (`tests/`)
- `test_integration_smoke.py` (7): orchestrator boot, idempotency dedup, queue backpressure, task failure event flow, EventBus thread safety, checkpoint corrupt-head fallback, studio path traversal.
- `test_web_hardening.py` (11): 413 enforcement, CSP/security headers, favicon 204, limit clamping, WS frame size + JSON errors, rate limiting, sanitized errors, webhook dedup.
- `test_agent_helpers.py` (8): `llm_complete` happy/stub/exception/timeout/retry paths, multi-line import refactor, CRLF preservation (this caught the bug), slack mention strip.
- `test_persistence_memory.py` (10): atomic write, schema_version rejection, corrupt-head fallback, custom_agents atomic write, ingestor persist, consciousness TTL, MemoryStore roundtrip, vector store mismatch.
- `test_security.py` (9): `redact_text` patterns, `SecretStore` env prefix + ephemeral key.
- `test_scheduler.py` (5): interval parsing, anchor drift math, missed-tick skip.
- `test_redaction.py` (3): `LLM_EXCHANGE` event scrubbing.

## Verification

```
Tests:  247/247 passing  (211 baseline + 36 new)
mypy:   clean (core/persistence/distributed — 16 source files, 0 issues)
ruff:   all checks passed
```

## Test plan

- [ ] CI runs `pytest tests/` and stays green.
- [ ] Smoke: `python -m uvicorn skyn3t.web.app:app` boots without import errors.
- [ ] Manual: load the dashboard, verify CSP header is present in DevTools, verify intervals stop when tab is hidden.
- [ ] Manual: send a webhook with duplicate `X-GitHub-Delivery` and verify second call returns `"duplicate": true`.
- [ ] Manual: SIGTERM the server while a task is in flight; verify a checkpoint is loadable on next start (no corrupt-head crash).
