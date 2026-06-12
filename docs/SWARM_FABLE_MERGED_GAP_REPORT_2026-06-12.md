# SkyN3t — Merged Swarm + Fable Gap Report & Action Plan

**Date:** 2026-06-12  
**Sources:**
- Swarm audit: 6 parallel read-only explore agents + automated checks (`pytest`, `ruff`, `mypy`) across `/Users/stephenchoate/Documents/Skyn3t/repo`.
- Fable audit: Cursor canvas report at `/Users/stephenchoate/.cursor/projects/Users-stephenchoate-Documents-Skyn3t-repo/canvases/skyn3t-gap-report.canvas.tsx`.
- Validation: Fable's findings were re-checked against current HEAD.

**Scope:** Full Skyn3t orchestrator (core runtime, Studio pipeline, web control plane, security, engineering health, product parity).

---

## Executive Summary

Both audits independently conclude the same thing: **SkyN3t is a real, running multi-agent orchestrator with genuine differentiators, but it is currently unsafe to expose beyond a single local operator and its quality signals are weaker than they appear.**

**Automated baseline (current HEAD):**

| Check | Result |
|---|---|
| `ruff check skyn3t tests` | ✅ Passed |
| `mypy skyn3t` | ❌ 25 errors in 8 files |
| `pytest tests/` | ❌ 34 failed, 2670 passed, 1 skipped |

The test failures are stale routing/model-name assertions, not new logic bugs, but they keep main red.

**Merged finding count:**

| Severity | Count | Notes |
|---|---|---|
| Critical | 14 | Security, fail-open verification, unwired critical features |
| High | 32 | Correctness, learning-loop gaps, operator UX, engineering integrity |
| Medium | 38+ | Cleanup, observability, UI polish, docs drift |

---

## Validation of Fable's Findings

Fable's canvas report contained **75 findings**. I re-validated all Critical/High findings and a representative sample of Medium findings against current HEAD:

- **11/11 Critical findings confirmed** still present.
- **24/25 High findings confirmed** still present; 1 was partial (the "no real e2e test" claim is partly true — `test_integration_smoke.py` exercises the real orchestrator, but no test runs a real brief through the full Studio pipeline).
- **14/15 Medium sample findings confirmed**; 1 had a stale line reference (`core/agent.py:322` → `core/agent.py:327`) but the gap remains.

**No Fable findings were fully fixed in the current tree.** The swarm audit surfaced additional issues not in Fable's report (see sections below).

---

## Critical Findings (Must Fix Before Any Wider Exposure)

### C1 — Authenticated RCE via `/api/exec` downgrade to `InlineBackend`
- **Sources:** Swarm
- **Files:** `skyn3t/web/app.py:1520-1568`, `skyn3t/security/sandbox.py:1074-1108`
- **Issue:** `PATCH /api/execution/backend` lets a token holder persist `backend=inline` without requiring `SKYN3T_ALLOW_INLINE_EXEC=1`. Once persisted, `POST /api/exec` runs submitted code through `InlineBackend.execute()` → Python `exec()` in the main process. The default `"auto"` path was fixed in a prior audit, but the explicit `"inline"` path bypasses the guard.
- **Fix:** Refuse to persist `inline` unless `SKYN3T_ALLOW_INLINE_EXEC` is set; gate `get_backend("inline")` itself, or remove `InlineBackend`.

### C2 — Path traversal in Studio project slug
- **Sources:** Swarm
- **Files:** `skyn3t/studio/runner.py:119-137`, `3522-3547`; `skyn3t/web/app.py:3817-3868`
- **Issue:** Caller-supplied `slug` is used directly as a path component under `projects_root`. No validation rejects `..`, absolute paths, or traversal sequences.
- **Exploit:** `POST /api/studio/start {"slug":"../evil",...}` creates projects outside the intended tree.
- **Fix:** Strict slug regex (`^[a-z0-9_-]{1,64}$`), reject `..`, canonicalize with `resolve()` + `relative_to(projects_root)`.

### C3 — World-readable `.env` contains live API tokens
- **Sources:** Swarm, Fable
- **File:** `.env` (repo root)
- **Issue:** `.env` is `644` and contains real `SKYN3T_TELEGRAM_TOKEN`, `OPENROUTER_API_KEY`, and `GITHUB_TOKEN`.
- **Fix:** `chmod 600 .env`, move credentials to encrypted store/keychain, rotate exposed tokens.

### C4 — Control plane unauthenticated by default; server binds `0.0.0.0`
- **Sources:** Fable, Swarm
- **Files:** `skyn3t/web/app.py:646-662`, `skyn3t/config/settings.py:253`
- **Issue:** With no `SKYN3T_WEB_TOKEN`, the only protection is a loopback-host check. The server binds `0.0.0.0` by default. `/api/exec` and other dangerous endpoints share the same gate.
- **Fix:** Require auth by default; make insecure-localhost an explicit opt-in; disable `/api/exec` outside dev mode.

### C5 — Skipped verifiers count as shipped
- **Sources:** Fable
- **File:** `skyn3t/studio/runner.py:6663-6667`
- **Issue:** Final `done` check treats `"skipped"` build/boot/integration verdicts as passing. Unknown stacks reach `done` with zero runtime proof.
- **Fix:** Treat `"skipped"` as blocking for code-bearing briefs unless explicitly waived.

### C6 — Verifier crash or `None` return is fail-open
- **Sources:** Fable
- **File:** `skyn3t/studio/runner.py:2612-2615`
- **Issue:** If a verifier raises, it is logged and `build_result = None`. The subsequent check is `if build_result is not None:`, so no `no` verdict is recorded and the reviewer can still finalize.
- **Fix:** Map verifier `None`/exception to `verdict="no"` with a failure hint.

### C7 — No crash recovery: checkpoints exist but are unwired
- **Sources:** Fable
- **File:** `skyn3t/persistence/recovery.py:70`
- **Issue:** `RecoveryManager` is exported but never invoked by `orchestrator.py` or `web/app.py`. In-flight tasks are lost on restart.
- **Fix:** Wire periodic checkpoints; on boot reconcile pending/running DB rows against live agents.

### C8 — Never-stop watchdog runs inside the process it guards
- **Sources:** Fable
- **File:** `skyn3t/cortex/never_stop.py:54`
- **Issue:** A segfault/OOM-kill takes both the process and the watchdog down.
- **Fix:** Add an external supervisor (launchd/systemd/Docker restart policy) that probes `/api/fleet/status` and restarts the server.

### C9 — Single-process architecture: event bus/consciousness/fleet are in-memory singletons
- **Sources:** Fable
- **File:** `skyn3t/core/events.py:99`
- **Issue:** No horizontal scaling path; state is lost on restart.
- **Fix:** Declare single-leader deployment explicitly, or move to Redis/NATS bus + external queue first.

### C10 — Cron-to-gateway delivery bridge built but never registered at boot
- **Sources:** Fable
- **File:** `skyn3t/integrations/gateway.py:410`
- **Issue:** `register_scheduled_delivery_bridge()` exists but is never called. Scheduled deliveries only work in tests.
- **Fix:** Call it in orchestrator/web startup.

### C11 — Remote execution backends unreachable
- **Sources:** Fable, Swarm
- **Files:** `skyn3t/security/sandbox.py:1060-1109`, `skyn3t/intelligence/backends/*`
- **Issue:** Modal/Daytona/E2B/SSH backends have code and tests but `get_backend()` only resolves `inline`/`docker`/`docker-pool`/`auto`.
- **Fix:** Route remote backend names through `get_remote_backend()` in `get_backend()`, or delete the dead backends.

### C12 — Unified messaging ingest not wired end-to-end
- **Sources:** Fable
- **Files:** `skyn3t/integrations/messaging.py:1230`, `skyn3t/core/orchestrator.py`
- **Issue:** `MessagingRouter` exists, but the orchestrator does not route channel tasks in or replies out. Only parallel Discord/Telegram bots work.
- **Fix:** Add orchestrator handler: channel `TASK_CREATED` in, `MessagingRouter.reply()` out.

### C13 — Stale tests fail on clean `main`
- **Sources:** Fable, Swarm
- **Files:** `tests/test_router_cost_weighted.py`, `tests/test_phase5a_routing.py`, `tests/test_model_router_adaptive.py`, etc.
- **Issue:** 34 tests assert old cost tables/model names. The CI baseline is red.
- **Fix:** Derive expectations from module constants; update stale assertions.

### C14 — Research stage ships placeholder findings on LLM failure
- **Sources:** Fable
- **File:** `skyn3t/agents/research_agent.py:373-377`
- **Issue:** On LLM failure, synthetic "Placeholder finding N" results are appended and consumed as real research.
- **Fix:** Fail the stage or mark `research_quality=synthetic` and block code generation.

---

## High Findings (Selected, Deduplicated)

### Security
| # | Issue | Files | Fix |
|---|---|---|---|
| H1 | Linux sandbox has only rlimits — no namespaces/seccomp/network enforcement | `security/sandbox.py:278-444` | Add namespaces/seccomp or require Docker |
| H2 | Docker containers run as root with full capabilities | `security/sandbox.py:739-752`, `851-868` | Add `--user`, `--cap-drop ALL`, `--security-opt no-new-privileges` |
| H3 | Secret scrubbing misses provider keys and over-strips `TOKEN*` | `security/sandbox.py:53-58` | Maintain explicit provider-specific block-list |
| H4 | Permission engine exists but is never enforced | `security/permissions.py:153-401` | Wire `can_execute_task()` into task dispatch |
| H5 | MCP path checks use `startswith` / no check → traversal | `adapters/mcp_tools.py:22-27`, `mcp_client.py:34-41` | Use `relative_to()` containment |
| H6 | CORS defaults to `["*"]` | `config/settings.py:255` | Default to explicit local origins |
| H7 | Web token compared with non-constant-time `!=` | `web/app.py:651`, `682` | Use `hmac.compare_digest()` |
| H8 | Raw `str(e)` leaks in 500 responses | `web/app.py:1258`, `1659`, `1872` | Route all 500s through `_safe_error_response()` |
| H9 | `/health` and `/metrics` not exempt from web-token auth | `web/app.py:781-801`, `646-662` | Exempt health/metrics or document token injection |
| H10 | Telegram webhook unauthenticated when secret unset | `integrations/telegram_webhook.py:45-51` | Require secret; fail closed |

### Correctness / Silent Failures
| # | Issue | Files | Fix |
|---|---|---|---|
| H11 | Python verifier only `py_compile`s; missing imports pass | `agents/build_verifier.py:355-364` | Install deps / run imports or downgrade verdict |
| H12 | npm network failures return `yes` in fast/offline mode | `agents/build_verifier.py:456-464` | Return `"skipped"`, not `"yes"` |
| H13 | Forced `CI=1` turns warnings into errors | `agents/build_verifier.py:1188` | Make opt-in or remove |
| H14 | `task --pipe-to` forwards failed upstream output | `cli/main.py:2215-2258` | Inspect upstream success before forwarding |
| H15 | Per-file routed `LLMClient` leaked | `agents/code_agent.py:2479-2525` | `aclose()` in `finally` |
| H16 | `_probe_version` leaks zombie children | `adapters/llm_client.py:579-598` | `await proc.wait()` after kill |
| H17 | `DockerPoolBackend._ensure_pool` unsynchronized | `security/sandbox.py:842-878` | Guard with `asyncio.Lock` |

### Self-Learning / Autonomy
| # | Issue | Files | Fix |
|---|---|---|---|
| H18 | Collective consciousness is RAM-only | `memory/consciousness.py:31-43` | Snapshot to SQLite/Redis and hydrate |
| H19 | Reflection lessons stored without embeddings → RAG never surfaces them | `memory/ingestor.py:285-317` | Embed lessons into Chroma on approval |
| H20 | Lesson attribution broken for Studio builds | `intelligence/learning_loop.py`, `studio/runner.py:1297-1305` | Publish Studio task events or call `LessonScoreboard` directly |
| H21 | Skill grading only fires for hard-coded tag list | `agents/code_agent.py:1869-1909` | Grade all injected skills |
| H22 | `experience_index` 80% `NULL` stack | `memory/store.py`, `studio/runner.py:1428-1435` | Include `stack` in all stage-failure events |
| H23 | Meta-agent/self-tuner emit alerts rather than applying changes | `memory/meta_agent.py:639-677` | Define auto-apply tiers |
| H24 | No retention policy: tasks/logs/docs grow forever | `memory/store.py:981-1011` | Scheduled pruning |
| H25 | Global asyncio lock serializes all SQLite writes | `memory/store.py:43` | Narrow lock scope / batch / Postgres |

### Studio / Product
| # | Issue | Files | Fix |
|---|---|---|---|
| H26 | Reviewer score is mostly LLM self-grade + heuristics | `agents/reviewer.py:218-222` | Cap score when objective verification fails |
| H27 | No end-user feedback loop post-ship | `studio/approval_gate.py` | Add post-completion rating wired to scoreboard |
| H28 | Stack support only web/server/fullstack | `agents/stack_detector.py:34` | Add CLI/mobile/desktop families or fail loudly |
| H29 | docker compose build verification documented but missing | `agents/packaging_agent.py:906`, `build_verifier.py` | Implement compose verify or correct docs |
| H30 | No UI/API to cancel a running Studio build | `web/ui/src/routes/StudioPage.tsx` | Add cancel endpoint + button |
| H31 | Audit log has no REST endpoint or UI | `security/audit.py:93` | Expose `GET /api/audit` |
| H32 | Encrypted `SecretStore` has no HTTP surface; UI cannot manage keys | `security/secrets.py:103` | Add masked key API + optional encrypted writes |

### Engineering Health
| # | Issue | Files | Fix |
|---|---|---|---|
| H33 | 10,700-line legacy `dashboard.html` duplicates React SPA | `web/dashboard.html`, `web/app.py:824-833` | Finish React parity, remove monolith |
| H34 | SPA build not in setup or CI | `scripts/setup.sh`, `.github/workflows/ci.yml` | Add `npm ci && npm run build` |
| H35 | Dual dependency manifests drift; no lockfile | `pyproject.toml`, `requirements.txt` | Single source of truth + committed lock |
| H36 | CI skips observability tests instead of fixing singleton pollution | `.github/workflows/ci.yml:34` | Autouse reset fixtures; re-enable |
| H37 | No real end-to-end test of full Studio pipeline | `tests/test_integration_smoke.py` | Add marked-slow e2e behind env vars |
| H38 | CI runs ruff/mypy/pytest but not black/coverage/UI tests; mypy permissive | `.github/workflows/ci.yml`, `pyproject.toml` | Add format check, coverage floor, vitest |

---

## Additional Findings from Swarm Not in Fable's Report

These were surfaced by the swarm but absent from the canvas report:

1. **Inline downgrade RCE** (C1) — Fable noted `/api/exec` is dangerous but missed the `PATCH /api/execution/backend` downgrade vector.
2. **Slug path traversal** (C2) — not covered.
3. **`.env` permissions and live tokens** — Fable mentioned plaintext `.env` but not world-readable perms or live values.
4. **Linux sandbox weakness** (H1), **Docker root/caps** (H2), **secret scrubbing gaps** (H3), **MCP traversal** (H5).
5. **Per-file LLMClient leak** (H15), **zombie probe** (H16), **DockerPool race** (H17).
6. **`lesson_scores.json` absent** (H20), **80% null stack** (H22) — Fable covered lessons-not-embedded but not these data-quality gaps.
7. **Token tracker in-memory singleton**, **trajectory logger sync write stalls event bus**, **trajectory token key mismatch**.
8. **Missing `Dockerfile`** (compose references it but file absent).
9. **WebSocket token in query string + localStorage** (Fable covered localStorage but not query string).
10. **130 backend routes, UI uses ~36** — large orphaned API surface.
11. **Production-dead subsystems:** `skyn3t/distributed/`, `integrations/acp_server.py`, `agents/browser_agent.py`.
12. **34 stale routing tests** on main — Fable covered stale tests generally; swarm has the exact count and failure areas.
13. **Low coverage modules:** `research_agent.py` (10%), `github_ingestor.py` (15%), `email_agent.py` (21%), `boot_verifier.py` execute path (45%).

---

## Single Action Plan

### Phase 0 — Safety & trust (do first; small, contained changes)

| # | Action | Owner files | Effort | Why |
|---|---|---|---|---|
| 0.1 | Disable `/api/exec` outside dev mode; refuse `inline` backend persistence without opt-in | `web/app.py:1520`, `security/sandbox.py:1074` | Small | Closes authenticated RCE |
| 0.2 | Require `SKYN3T_WEB_TOKEN` by default; make loopback-only an explicit opt-in | `web/app.py:646`, `config/settings.py:253` | Small | Closes unauthenticated control plane |
| 0.3 | Sanitize Studio project slugs | `studio/runner.py:119`, `web/app.py:3817` | Small | Closes path traversal |
| 0.4 | Lock `.env` to 600, rotate tokens, document secret-store migration | `.env`, `security/secrets.py` | Small | Closes credential leak |
| 0.5 | Treat skipped/crashed verifier as `verdict=no` | `studio/runner.py:2612`, `6663` | Small | Closes fail-open verification |
| 0.6 | Constant-time token compare; exempt `/health`/`/metrics` from auth | `web/app.py:651`, `781` | Small | Security hygiene |
| 0.7 | Fix the 34 stale tests on main | `tests/test_router_cost_weighted.py`, etc. | Small | Restores green CI |

### Phase 1 — Integrity & operability

| # | Action | Owner files | Effort |
|---|---|---|---|
| 1.1 | Add external supervisor / crash recovery wiring | `persistence/recovery.py`, `scripts/run.sh`, systemd/launchd | Medium |
| 1.2 | Snapshot collective consciousness to SQLite/Redis and hydrate | `memory/consciousness.py` | Medium |
| 1.3 | Embed reflection lessons into RAG | `memory/ingestor.py:285` | Small |
| 1.4 | Wire Studio builds into lesson attribution | `studio/runner.py`, `intelligence/learning_loop.py` | Medium |
| 1.5 | Record `stack` on all stage-failure events | `studio/runner.py:1428` | Small |
| 1.6 | Add retention/pruning for tasks/logs/knowledge | `memory/store.py` | Medium |
| 1.7 | Add cancel-build API + UI button | `web/app.py`, `web/ui/src/routes/StudioPage.tsx` | Small |
| 1.8 | Expose audit log API + page | `security/audit.py`, `web/app.py`, UI | Medium |

### Phase 2 — Built-but-not-booted features

| # | Action | Owner files | Effort |
|---|---|---|---|
| 2.1 | Register cron-to-gateway bridge at boot | `integrations/gateway.py:410`, `web/app.py` | Small |
| 2.2 | Wire remote backends or delete dead code | `intelligence/backends/`, `security/sandbox.py` | Medium |
| 2.3 | Wire unified messaging router | `integrations/messaging.py:1230`, `core/orchestrator.py` | Medium |
| 2.4 | Start Slack bot when token configured | `integrations/slack_bot.py`, `web/app.py:4321` | Small |
| 2.5 | Register channels at boot, not lazily | `web/app.py:4321-4353` | Small |

### Phase 3 — Verification hardening

| # | Action | Owner files | Effort |
|---|---|---|---|
| 3.1 | Python verifier installs deps / checks imports | `agents/build_verifier.py:355` | Medium |
| 3.2 | Return `"skipped"` on npm network fallback | `agents/build_verifier.py:456` | Small |
| 3.3 | Remove unconditional `CI=1` | `agents/build_verifier.py:1188` | Small |
| 3.4 | Cap reviewer score when objective verification fails | `agents/reviewer.py:218` | Small |
| 3.5 | Fail on placeholder research findings | `agents/research_agent.py:373` | Small |
| 3.6 | Add Docker-backed verification backend | `agents/build_verifier.py:1173` | Medium |

### Phase 4 — Architecture cleanup

| # | Action | Owner files | Effort |
|---|---|---|---|
| 4.1 | Delete or fully wire `skyn3t/distributed/` | `skyn3t/distributed/` | Small |
| 4.2 | Remove status-only `browser_agent.py` and `acp_server.py` unless UI/commands built | `agents/browser_agent.py`, `integrations/acp_server.py` | Small |
| 4.3 | Audit and remove/document ~94 unused API endpoints | `web/app.py` | Medium |
| 4.4 | Consolidate execution-backend abstractions | `security/sandbox.py`, `intelligence/subagent_runner.py`, `intelligence/docker_backend.py`, `intelligence/backends/base.py` | Medium |
| 4.5 | Break model-router circular dependency | `core/model_router.py`, `core/openrouter_catalog.py`, `core/model_evolution.py` | Small |

### Phase 5 — Engineering health

| # | Action | Owner files | Effort |
|---|---|---|---|
| 5.1 | Fix 25 mypy errors | 8 files | Small |
| 5.2 | Add `black --check` and `pytest-cov` floor to CI | `.github/workflows/ci.yml` | Small |
| 5.3 | Add SPA build job to CI and setup | `scripts/setup.sh`, `.github/workflows/ci.yml` | Small |
| 5.4 | Single source of truth for deps + committed lockfile | `pyproject.toml`, `requirements.txt` | Small |
| 5.5 | Remove legacy `dashboard.html` after React parity | `web/dashboard.html` | Small |
| 5.6 | Add one marked-slow Studio e2e test | `tests/` | Medium |
| 5.7 | Backfill tests for `boot_verifier.execute()`, `research_agent.py`, `github_ingestor.py`, `email_agent.py` | `tests/` | Medium |

---

## Bottom Line

SkyN3t has a **working engine and real competitive moats**: the Studio verification sandwich, the Cortex flywheel, domain learning, and autonomous scheduling are not cosmetic. But the system is currently a **dangerous half-product**:

- An authenticated user (or any loopback process without a token) can get host RCE.
- Generated apps can pass verification while being unrunnable.
- Half the documented competitive features exist in code but are never wired at boot.
- The system practices constantly but throws away most of what it learns.
- The repo's self-description and test baseline both drift from reality.

**The highest-leverage next step is Phase 0.** Items 0.1–0.7 are each a focused session and close the most severe security and integrity holes. After that, Phase 1 closes the learning-loop gaps that justify the project's "self-improving" positioning, and Phase 2 flips the cheapest wins: the built-but-unwired features.
