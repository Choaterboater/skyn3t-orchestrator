# SkyN3t — Session Continuation Handoff

Use this file when a chat runs out of tokens or you start a fresh agent session.

## Moat-first baseline (2026-06-12)

| Item | Value |
|------|--------|
| **Homelab proof slug** | `aruba-central-field-triage` |
| **Reviewer score** | 92/100 (manual repair validation) |
| **Build verification** | `yes` (`npm run build`) |
| **Domain benchmark** | 92/100 (`domain_benchmark` in project.json) |
| **Studio smoke** | `bash scripts/studio_smoke.sh aruba-central-field-triage` (npm build path) |
| **Test suite** | `pytest tests/ -q --ignore=tests/test_observability.py` — target green after moat batch |

Phase 0–5 moat plan landed: score truth + feedback UI, generated smoke tests + pytest gate, token rollup persist, domain corpus prompts, graceful shutdown timeout, task reconciliation, deep-profile debate/A2A, examples decision doc, live-read approval API, weekly domain benchmark script.

## Current state (2026-06-11)

- **Branch:** `main` @ `7cb27d0` before this commit; push this handoff commit to `origin/main`.
- **Working tree:** tracked changes are the autonomy/domain-learning implementation; untracked only local editor noise (`.obsidian/`, `*.canvas`, `2026-05-27.md`, `FETCH_HEAD`) plus generated project artifacts outside the repo root.
- **Server:** SkyN3t web/API runs on `http://127.0.0.1:6660/` when started. It can become sluggish during large Studio builds; restart safely after projects are `done` or `failed`.
- **Autonomy budget:** lowered to safer settings in `.env`: `SKYN3T_AUTONOMOUS_BUILD_DAILY_BUDGET_USD=10.0`, `SKYN3T_AUTONOMOUS_BUILD_DAILY_CAP=10`, `SKYN3T_AGENT_FLEET_MAX_CONCURRENT_BUILDS=3`.
- **Model routing note:** Opus appeared because model evolution promoted `or_strong`; local data overrides removed that Opus override and routed some stages cheaper, but the committed default policy still has `or_strong` for architect/reviewer unless changed in code or via persisted routing.
- **Golden corpus:** 7 GitHub repos were ingested as read-only golden networking references.
- **Generated candidates:** three networking apps repaired to `done` 92/100; CentralMCP challenger v2 scored 100/100 vs centralmcp baseline 96; PyConsole 2030 scored 100/100 vs ConsolePi baseline 92.
- **Running app:** PyConsole 2030 is launched at frontend `http://127.0.0.1:5182/` and backend `http://127.0.0.1:3102/`.

### Git (verified 2026-06-11)

| Item | Value |
|------|--------|
| `HEAD` | `7cb27d0d8184e5166ea01f1486476dd1d4c89229` before commit |
| `origin/main` | same as pre-commit `HEAD` |
| Status | On `main`; commit and push pending |

Recent commits on `main`:

| SHA | Subject |
|-----|---------|
| `7cb27d0` | feat(cortex): fleet coordination, continuous improvement, and dashboard UX |
| `519091b` | wip: split-source snapshot for PR extraction |
| `30490ec` | Fix Studio clarification intent and improve REPL scout UX. |

### Open pull requests

| # | Title | Branch |
|---|-------|--------|
| 53 | feat(web): scout REST API for Cortex dashboard | `feat/cortex-scout-rest-api` |
| 52 | docs: CONTINUE handoff and mission updates | `docs/handoff-continue-mission` |
| 51 | feat(web): routing, autonomy, and OpenRouter dashboard APIs | `feat/web-dashboard-routing-autonomy` |
| 50 | feat(routing): OpenRouter sync, multi-LLM wizard, CLI skills | `feat/openrouter-sync-and-cli-wizard` |
| 49 | feat(autonomy): autonomous builds, proof runs, no-approval | `feat/autonomous-build-loop-no-approval` |
| 48 | feat(core): autonomy learning and self-healing | `feat/autonomy-learning-self-healing` |
| 47 | feat(studio): quality verification and critique stack | `feat/studio-quality-verification-stack` |
| 46 | feat(cortex): GitHub scout dashboard and competitive intel | `feat/cortex-github-scout-competitive-intel` |
| 45 | fix(deps): pin starlette for FastAPI compatibility | `fix/deps-starlette-pin` |

## Resume prompt

```
Read docs/CONTINUE.md. Continue from domain-autonomy implementation on main. Verify commit pushed.
Next: decide whether to commit generated candidate apps into an examples/projects repo, harden live connector credentials, and update default routing if Opus/strong-tier should be opt-in only.
```

## Next priorities (ordered)

> **RECONCILED 2026-06-11 (later session).** Most of the original list is DONE —
> see "## STATUS 2026-06-11 (Phases 0–5 + cost work)" just below. Remaining live
> items: (a) examples-repo decision, (b) approval-gated live-read credential
> flows, (c) keep feeding GOLD/WEAK repos (scout now auto-pulls general repos).

1. ~~Commit and push to `main`.~~ **DONE** — Phases 0–5 + cost work on `origin/main`.
2. Decide: copy generated candidate apps into a tracked examples repo vs. leave
   under `/Users/stephenchoate/Documents/Skyn3t/Projects`. *(open)*
3. ~~Make strong/Opus routing explicitly opt-in.~~ **DONE** — cost-blind model-tier
   evolution DISABLED (`SKYN3T_MODEL_EVOLUTION=0`, premium blocked); all tiers
   default to FREE OpenRouter models; cost-aware Phase-5 `SKYN3T_AUTO_ROUTE`
   handles "smarter over time".
4. Add approval-gated live-read credential flows for selected candidates. *(open)*
5. Keep feeding `GOLD:`/`WEAK:` repos. *(ongoing — scout auto-discovers + files
   general repos daily; no longer networking-only.)*
6. ~~Close/reconcile stale PRs #45–#53.~~ **DONE** — all 9 closed as superseded by
   main (#45's starlette pin was already present).

---

## STATUS 2026-06-11 (Phases 0–5 + cost work)

Full audit → fix → upgrade cycle landed on `origin/main`:

- **Phase 0/1** — 2 criticals + ~19 high bugs (worktree empty-scaffold, RCE-by-
  default, silent verifier no-ops, seatbelt regression) fixed + tested.
- **Phase 2** — self-learning loops closed (lessons→prompt, skills graded on
  verdict, unified stack buckets, threshold pattern-graduation, GitHub ingestion).
- **Phase 3** — generated-app quality + world-class UI gates + Capacitor/PWA/
  desktop packaging (stub hard-gate, real CRUD backend, functional + visual gates,
  design-token contract).
- **Phase 4** — living "cortex" command-center dashboard (shared WS, live build
  streaming, brain viz, install/settings wizard, charts).
- **Phase 5** — 10x: multi-LLM debate (cheap/free), A2A agent conversations,
  reflective self-correction, cost-aware auto-routing, asset-gen/Figma; + parity:
  remote/serverless backends (ssh/modal/daytona/e2b), 7 messaging channels, NL
  cron + gateway, browser automation. (A2A + asset-gen wired but OFF by default.)

**Runtime (cost-sensitive owner):** all OpenRouter tiers → FREE models; cost-blind
evolution OFF; debate/reflection/auto-route ON; Docker exec; caps 50 builds/day +
$20 ceiling → real spend ≈ $0 (the `daily_spend_usd` estimator was ~10x inflated
and is now fixed). The autonomous fleet runs UNATTENDED: agent_fleet dispatch
(30s) + never_stop replenish + curiosity/scout brief generation. Full forward
plan: `SKYN3T_10X_PLAN.md`. Start: `nohup skyn3t start --host 127.0.0.1 --port
6660` (framework Python 3.13, editable install); `load_dotenv` applies `.env`
flags on start.

---

## Session summary

Work across this thread focused on **fixing CI-quality issues** and **raising Studio build quality to beat Hermes**.

### Domain-autonomy expansion (this session)

| Area | Change |
|------|--------|
| **Golden corpus** | Added redacted domain corpus schema and ingestor for local/GitHub networking projects. Originals remain read-only. |
| **Networking rubric** | Added domain quality scoring for vendor API realism, dry-run safety, config validation, inventory, troubleshooting, sample mode, and operator docs. |
| **Benchmarking** | Added domain benchmark harness, model tournament store, and safe project-evolution candidate/proposal flow. |
| **CLI controls** | Added `skyn3t domain queries`, `skyn3t domain ingest-local`, and `skyn3t domain candidate`. |
| **GitHub seeds** | Ingested `secure-ssid/aos8-migration-tool`, `secure-ssid/New-Central-Portal`, `secure-ssid/centralmcp`, `Choaterboater/GreenCli`, `Choaterboater/securessid`, `aruba/pycentral`, and `Pack3tL0ss/ConsolePi` as read-only golden corpus skills/RAG docs. |
| **Baseline reports** | Saved comparison reports under session files: `baseline-comparison.json`, `centralmcp-challenger-v2-comparison.json`, `pyconsole-2030-consolepi-comparison.json`. |

### Generated candidate apps (outside repo root)

| Candidate | Path | Result |
|-----------|------|--------|
| Aruba Central field triage | `/Users/stephenchoate/Documents/Skyn3t/Projects/aruba-central-field-triage` | Repaired to `done`, score 92; build/test/domain benchmark pass. |
| AOS8 migration readiness | `/Users/stephenchoate/Documents/Skyn3t/Projects/aos8-migration-readiness-tool` | Repaired to `done`, score 92; build/test/domain benchmark pass. |
| Network config drift inventory | `/Users/stephenchoate/Documents/Skyn3t/Projects/network-config-drift-inventory` | Repaired to `done`, score 92; build/test/domain benchmark pass. |
| CentralMCP FieldOps challenger v2 | `/Users/stephenchoate/Documents/Skyn3t/Projects/centralmcp-fieldops-challenger-v2` | Local candidate scored 100 vs centralmcp baseline 96; proposal `b6054ff046a3`. |
| PyConsole 2030 | `/Users/stephenchoate/Documents/Skyn3t/Projects/pyconsole-2030-field-console-v1` | Local candidate scored 100 vs ConsolePi baseline 92; proposal `521b0da3a578`; launched on ports 5182/3102. |

### PyConsole 2030 current runtime

```bash
open http://127.0.0.1:5182/
curl http://127.0.0.1:3102/api/health
curl http://127.0.0.1:3102/api/config/status
curl http://127.0.0.1:3102/api/mcp/tools
```

The app now has env-driven credential status, live read-only Aruba Central request path, serial/SSH planning, Junos RPC planning, config validation, backup diff, diagnostic bundle preview, and MCP stdio server (`npm run mcp`). It remains dry-run by default.

### Fixes & tooling

| Area | Change |
|------|--------|
| **FastAPI/Starlette** | Pinned `starlette>=0.40.0,<1.2` in `pyproject.toml` + `requirements.txt` (fixes `Router.__init__() got an unexpected keyword argument 'on_startup'` on mismatched system Python) |
| **REPL test** | `skyn3t/cli/repl.py` — only run async approval path when line looks like an approval command (`_parse_studio_approval_plain`) |
| **Ruff** | Auto-fixed unused imports in cli, repl, tests |
| **Mypy** | `orchestrator.py` `setattr(agent, "rag", rag)`; `main.py` `cast()` + `approval_message` rename |

### Quality bar (shippable software, not demos)

| Area | Change |
|------|--------|
| **Execution profile** | Short briefs default to `balanced` (not `fast`) — `skyn3t/studio/runner.py` `_infer_execution_profile` |
| **Reviewer threshold** | `REVIEWER_SCORE_THRESHOLD = 80`; verdict `go` at ≥80, `go-with-fixes` at ≥60 (≥70 for UI builds) |
| **Code agent** | Stronger UI polish + end-to-end workflow requirements in per-file system prompt |
| **Missing reviewer** | If `build_verification` ran but no reviewer score → `needs_fixes` (not silent `done`) |

### Hermes-beating quality stack (implemented)

| Feature | File(s) | Notes |
|---------|---------|-------|
| **Cross-model critique** | `skyn3t/studio/runner.py` `_critique_and_revise` | Reviewer skips producer backend via `resolve_model` + `_skip_backends` |
| **Entrypoint wiring gate** | `skyn3t/agents/contract_engine.py` `_check_entrypoint_wiring` | Blocker when polished `components/*` exist but `App.jsx` never mounts them |
| **Fail-closed contract verifier** | `skyn3t/agents/contract_verifier.py` | Engine crash → `needs_fix` blocker, not silent pass |
| **Stricter npm verify** | `skyn3t/agents/build_verifier.py` | Network-only pass only in `fast` profile or `SKYN3T_VERIFY_OFFLINE=1` |
| **Post-fix re-score** | `skyn3t/studio/runner.py` `_rerun_reviewer_scoring` | After reviewer targeted fixes, re-run ReviewerAgent and update `quality_summary` |
| **Bigger fix budget** | `skyn3t/agents/reviewer_fixes.py` | 5 files / $0.50 balanced; 8 / $0.75 deep |
| **Auto-retry** | `skyn3t/studio/runner.py` | Default ON (`SKYN3T_AUTO_RETRY=1`); one bounded `-retry` with failure hints + cross-model hint |
| **Critique skills** | `skyn3t/agents/reviewer.py` `_llm_critique` | Injects skills tagged `reviewer`, `critique`, `quality`, `{stage_name}` |

### Tests added

- `tests/test_contract_entrypoint_wiring.py` — orphan component blocker
- `tests/test_openrouter_catalog.py` — OpenRouter catalog sync, cache TTL, tier fallback

### OpenRouter model catalog (2026-06-11)

| Item | Detail |
|------|--------|
| Module | `skyn3t/core/openrouter_catalog.py` |
| Cache | `data/openrouter_models.json` (24h TTL; **6h** when model evolution enabled) |
| Auto-sync | Orchestrator/web start + background loop when `OPENROUTER_API_KEY` set (`SKYN3T_OPENROUTER_SYNC=1`, default on) |
| API | `GET /api/models/openrouter` (`?refresh=1` to force; includes `evolution` block) |
| CLI | `skyn3t models sync` (`--force`, `--evolve`) |
| Routing | `model_router._tier_backend_model()` validates tier ids; missing → keyword fallback + warning |

### Model Evolution Engine (2026-06-11)

| Item | Detail |
|------|--------|
| Module | `skyn3t/core/model_evolution.py` |
| Overrides | `data/model_tier_overrides.json` — persisted tier→model upgrades (no downgrade by default) |
| Enable | `SKYN3T_MODEL_EVOLUTION=1` (default on with `OPENROUTER_API_KEY`) |
| Scoring | Context length, pricing, tool support, recency hints in model id, tier keywords |
| Alerts | `SYSTEM_ALERT` with `alert_type=MODEL_TIER_EVOLVED` on upgrades |
| Integration | `model_router` reads overrides before `_TIERS`; `pick_best_model_for_task` refines per file type |
| TTL | 6h catalog sync when evolution on (proactive discovery, not just missing-model fallback) |

Env: `SKYN3T_MODEL_EVOLUTION=1`, `SKYN3T_MODEL_EVOLUTION_DOWNGRADE=0`, `SKYN3T_MODEL_EVOLUTION_MIN_GAIN=0.25`

Tests: `tests/test_model_evolution.py`

---

## Modified files (git status snapshot)

**Current (2026-06-11, post–`7cb27d0`):** no tracked modifications; handoff work is on `main` @ `origin/main`. Untracked noise only — see **Current state** above.

<details>
<summary>Pre-merge snapshot (historical — merged into main)</summary>

Tracked modifications before direct push:

```
.env.example, README.md, docs/MISSION.md, pyproject.toml, requirements.txt,
skyn3t/agents/* (build_verifier, code_agent, contract_*, reviewer*),
skyn3t/cli/main.py, skyn3t/cli/repl.py, skyn3t/core/orchestrator.py,
skyn3t/cortex/build_pattern_bias.py, skyn3t/studio/runner.py, tests/*
```

Also landed: `docs/CONTINUE.md`, `docs/REBUILD_PLAN.md`, `tests/test_contract_entrypoint_wiring.py`.

</details>

---

## How to verify

Always use the project venv:

```bash
cd /Users/stephenchoate/Documents/Skyn3t/repo
source .venv/bin/activate

ruff check skyn3t tests
mypy skyn3t
pytest tests/ -q --ignore=tests/test_observability.py
```

Focused smoke after Studio changes:

```bash
pytest tests/test_contract_entrypoint_wiring.py tests/test_contract_engine.py \
  tests/test_approval_gate.py tests/test_generation_optimization.py -q
```

Proof-run a completed project scaffold (uses `PROJECTS_DIR` from `.env`):

```bash
./scripts/studio_smoke.sh <project-slug>
```

Last known good: **1862+ tests passed** (1 skipped) before doc/approval-gate edits in this handoff pass.

---

## Env vars (quality mode)

| Variable | Default | Effect |
|----------|---------|--------|
| `SKYN3T_NO_APPROVAL` / `SKYN3T_AUTO_APPROVE` | unset | `1` — skip Studio gates + auto-triage Cortex ingest/tuning (`SKYN3T_AUTO_APPROVE_STUDIO` is a synonym) |
| `SKYN3T_AUTONOMOUS_LEARNING` | `1` | Scout schedule + ingest on orchestrator start |
| `SKYN3T_AUTONOMOUS_BUILDS` | `0` | `1` — queue practice builds from scout/lessons; implies no-approval unless `SKYN3T_AUTO_APPROVE=0` |
| `SKYN3T_AUTO_RETRY` | `1` | One bounded auto-retry on failed builds (`0` to disable) |
| `SKYN3T_DISABLE_CRITIQUE` | unset | Set `1` to skip inter-agent critique loops |
| `SKYN3T_VERIFY_OFFLINE` | unset | Set `1` to accept syntax-only pass when `npm install` has no network |
| `SKYN3T_VERIFY_NPM_INSTALL` | `1` | Set `0` to skip npm install/build in BuildVerifier |
| `SKYN3T_AUTONOMOUS_PROOF_RUN` | `1` | Post-build npm/py_compile proof for autonomous builds |
| `SKYN3T_AGENT_FLEET_SIZE` | `0` | `20` — parallel autonomous learn + build worker slots |
| `SKYN3T_AGENT_FLEET_MAX_CONCURRENT_BUILDS` | `5` | Studio semaphore cap when fleet builds on (keeps API responsive) |
| `SKYN3T_AGENT_FLEET_LEARNING` | `1` | Parallel learning ticks per idle slot (scout/RAG/routing/evolution) |
| `SKYN3T_CORTEX_SCOUT_DEFER_BOOT_SECONDS` | `120` | Defer fleet scout GitHub searches after orchestrator boot |
| `SKYN3T_STUDIO_WORKTREE` | `1` | Git worktree per fleet slot for CodeAgent isolation |
| `SKYN3T_CONTINUOUS_IMPROVEMENT` | `1` | Never-stop flywheel; boots 3-slot fleet when `FLEET_SIZE` unset |
| `SKYN3T_SKILLS_HUB_AUTO_INSTALL` | `1` | Seed hub skills on orchestrator start when no-approval |
| `SKYN3T_SKILLS_HUB_PATHS` | `examples/skills_seed,skills` | Comma-separated hub roots |
| `SKYN3T_CODE_TIER` | unset | Override code stage tier (`or_backend`, `or_cheap`, …); beats default `or_strong` |
| `SKYN3T_CHEAP_SMART` | `1` | Cheap-first code gen + context boost + escalation; set `0` for always-strong code |
| `execution_profile` | `balanced` | Pass `deep` on CLI/API for max critique rounds + fix budget |

Example max-quality build:

```bash
skyn3t project --execution-profile deep "Build a habit tracker with streaks and dark theme"
```

Documented in `.env.example`.

---

## Next priorities (historical — superseded)

> **Canonical queue:** see **Next priorities (ordered)** at the top of this document.

1. ~~**Install wizard for multi-LLM backends**~~ — `skyn3t wizard` / `skyn3t init --wizard` (Studio Quality OpenRouter preset + local CLI)
2. **Messaging channel parity** — Hermes ~18 channels; SkyN3t has ~13
3. ~~**ACP / Docker subagent runners as default**~~ — wizard sets `SKYN3T_EXECUTION_BACKEND=auto`; Agents page sandbox card + `GET/PATCH /api/execution/backend`
4. ~~**Model routing wizard**~~ — Agents page: Studio Quality preset button, per-stage tier table, `POST /api/routing/presets/studio-quality`

### Done in handoff pass (2026-06-11)

- `docs/CONTINUE.md` created (this file)
- `docs/REBUILD_PLAN.md` matrix — debate rows → ⚠️ Partial
- `docs/how-to-raise-studio-score.md` — thresholds 80/60, orphan_components note
- `skyn3t/studio/approval_gate.py` — default gates: Architect + Designer
- `tests/test_approval_gate.py` — default gate tests updated

### Done in autonomy wiring pass (2026-06-11)

| Feature | File(s) | Notes |
|---------|---------|-------|
| **Autonomy stack auto-boot** | `skyn3t/core/orchestrator.py` `_ensure_autonomy_prerequisites()` | `start()` now enables memory, consciousness, ingestor, reflection, tuner, meta-agent, and wires self-healing even when callers skip explicit `enable_*()` |
| **Real self-healing actions** | `skyn3t/core/self_healing.py` | Restart/throttle/timeout/queue-reset/isolate/cache-clear operate on live agents via orchestrator ref |
| **Live tuning apply** | `orchestrator._on_system_alert` | `tuning_applied` events push safe config nudges onto registered agents immediately |
| **Auto-triage: safe tuning** | `skyn3t/cortex/proposals.py` | `request_interval` / `timeout` / `max_tokens` / `prompt_suffix` / `auth_retry` proposals auto-apply when `SKYN3T_CORTEX_AUTO_APPROVE_SAFE_TUNING=1` (default) |
| **Auto-triage: build patterns** | `proposals.py`, `meta_agent.py`, `build_pattern_bias.py` | Bias proposals auto-apply; MetaAgent also writes skills + `data/build_pattern_preferences.json` on detection |
| **Studio outcome ingestion** | `skyn3t/memory/ingestor.py` | `PROJECT_COMPLETED` success/failure + `PROJECT_FAILED` events land in RAG/experience index without operator action |
| **Learning loop fix** | `skyn3t/intelligence/learning_loop.py` | Task lessons persist via correct `ingest_lesson()` signature |
| **Tests** | `tests/test_autonomy_wiring.py` | Stack boot, healing, auto-triage, ingestion |

**Runs autonomously on orchestrator start (no extra CLI):**

- ExperienceIngestor → RAG + experience index (task + Studio events)
- LearningLoop → lesson capture + RAG injection on route
- ReflectionEngine → failure pattern detection → SelfTuningEngine suggestions
- MetaAgent → threshold proposals + build-pattern skill/prefs
- CortexBootstrap → gated tuner, repo scout, curiosity, review watcher, auto cleanup
- SelfHealingManager → agent restart/throttle on repeated errors
- ProjectMemoryAgent (default roster) → full project artifact ingest on `PROJECT_COMPLETED`
- BuildPatternScoreboard (Studio runner) → shape/backend stats on every verifier verdict
- Studio auto-retry (`SKYN3T_AUTO_RETRY=1`)

**Still requires human approval (safety floor — even in no-approval mode):**

- SkyN3t repo self-edits: `code_patch`, `studio_debug`, scout-adaptation `feature` ideas
- Disabling autonomy: `SKYN3T_CORTEX_DISABLE=*` or per-component name

**Bypass approval gates (no-approval mode):**

Set `SKYN3T_NO_APPROVAL=1` (synonyms: `SKYN3T_AUTO_APPROVE=1`, `SKYN3T_AUTO_APPROVE_STUDIO=1`), or enable `SKYN3T_AUTONOMOUS_BUILDS=1` (implicit unless `SKYN3T_AUTO_APPROVE=0`). This skips Architect + Designer Studio gates and auto-approves Cortex ingest/tuning. Autonomous loop builds use the implicit path when `SKYN3T_AUTONOMOUS_BUILDS=1`.

---

### Done in autonomous loop pass (2026-06-11)

| Feature | File(s) | Notes |
|---------|---------|-------|
| **Autonomous learning boot** | `skyn3t/cortex/autonomous_loop.py`, `cortex/bootstrap.py` | `SKYN3T_AUTONOMOUS_LEARNING=1` schedules `autonomous-repo-scout` (12h default) |
| **Autonomous Studio builds** | `autonomous_loop.py`, `orchestrator.py`, `studio/runner.py` | `SKYN3T_AUTONOMOUS_BUILDS=1` queues micro-briefs from patterns/scout/competitive intel |
| **Operator API** | `GET /api/autonomous/status`, `SYSTEM_ALERT` kinds `AUTONOMOUS_BUILD_*` | Dashboard + logs distinguish autonomous vs manual (`manifest.autonomous`) |
| **Safety caps** | `settings.py` | Daily build cap + USD budget; respects Studio concurrency + scout busy gate |

Env: see `.env.example` autonomous section. SkyN3t repo changes still approval-gated; builds land in `PROJECTS_DIR` only.

### Done in agent fleet pass (2026-06-11)

| Feature | File(s) | Notes |
|---------|---------|-------|
| **AgentFleetCoordinator** | `skyn3t/cortex/agent_fleet.py` | N slots (`idle` / `learning` / `building`); fleet semaphore; composes with `AutonomousCoordinator` queue |
| **Fleet learning ticks** | `skyn3t/cortex/continuous_improvement.py` | `run_fleet_learning_tick` — scout, RAG, routing apply, model evolution |
| **Studio concurrency** | `skyn3t/studio/runner.py` | `configure_max_concurrent(N)` when fleet + autonomous builds on |
| **Operator API** | `GET /api/fleet/status` | 20 slots with state, brief/slug, tokens today, backpressure |
| **Events** | `SYSTEM_ALERT` | `FLEET_SLOT_STARTED`, `FLEET_SLOT_COMPLETED` |
| **Dashboard** | `OverviewPage.tsx` | Agent fleet tile (busy/building/learning counts) |
| **Safety** | `agent_fleet.py`, `autonomous_loop.py` | Daily cap scales with fleet size; orchestrator backpressure at 75% task load |

**Enable 20-agent fleet:**

```bash
SKYN3T_AGENT_FLEET_SIZE=20
SKYN3T_AGENT_FLEET_LEARNING=1
SKYN3T_AUTONOMOUS_BUILDS=1
SKYN3T_AUTONOMOUS_LEARNING=1
SKYN3T_AUTONOMOUS_BUILD_DAILY_CAP=20
SKYN3T_AUTONOMOUS_BUILD_INTERVAL_SECONDS=60
SKYN3T_NO_APPROVAL=1   # or implicit via AUTONOMOUS_BUILDS
```

Tests: `tests/test_agent_fleet.py`

### Done in quality-fix pass (2026-06-11)

| Feature | File(s) | Notes |
|---------|---------|-------|
| **Post-critique re-review** | `skyn3t/studio/runner.py` `_critique_and_revise` | After code-stage targeted fix with `files_changed`, calls `_rerun_reviewer_scoring` |
| **Pre-code design gate** | `skyn3t/studio/runner.py` `_run_pre_code_design_gate` | Before CodeAgent when designer ran + visual UI brief; checks palette/brand/components |
| **Critique fail-closed (code)** | `runner._critique_timeout_for`, critique handlers | +60s code floor for balanced/deep; `CRITIQUE_INCOMPLETE` history on code timeout |
| **TODO stub fix loop** | `runner._run_post_code_checks` | Stubs trigger targeted fix first; fail stage if stubs remain after fix |
| **Stronger code routing** | `skyn3t/core/model_router.py` | Default `code` / `code_agent` tier → `or_strong` (quality > cost) |
| **SKYN3T_CODE_TIER override** | `skyn3t/core/model_router.py` | Operators can downgrade code stage + per-file routing via env |
| **Proof-run helper** | `scripts/studio_smoke.sh` | `npm install` + `build` (or `py_compile`) on `scaffold/` for a slug |
| **Multi-LLM wizard** | `skyn3t wizard`, `cli/main.py` | Studio Quality preset: OpenRouter + per-stage `or_strong`/`or_ui` routing + `SKYN3T_AUTO_RETRY=1` |
| **Docker sandbox default** | wizard, `GET/PATCH /api/execution/backend`, Agents UI | `SKYN3T_EXECUTION_BACKEND=auto` (Docker pool when available) |
| **Routing wizard UI** | `AgentsPage.tsx`, routing preset API | One-click Studio Quality + per-stage tier editor |

### Done in cheap-smart pass (2026-06-11)

| Feature | File(s) | Notes |
|---------|---------|-------|
| **SKYN3T_CHEAP_SMART** | `skyn3t/intelligence/cheap_smart.py`, `model_router.py` | Default ON — code stages start `or_cheap`; runtime escalation → `or_strong` on failure |
| **Context boost** | `cheap_smart.py`, `code_agent.py` | Checklist + winning scaffold shape + competitive/UI/backend hints injected for cheap tiers |
| **Capability-aware picker** | `openrouter_catalog.py`, `model_router.py` | `pick_best_model_for_task()` selects catalog specialist per file type |
| **Model evolution** | `model_evolution.py`, `openrouter_catalog.py` | Proactive tier upgrades after sync; overrides in `data/model_tier_overrides.json` |
| **Cross-model lift** | existing `runner._critique_and_revise` | Cheap producer + strong reviewer unchanged; escalation bumps code tier when critique/verifier fails |
| **Routing auto-apply** | `cheap_smart.auto_apply_cheaper_routing`, `runner.py` | High-confidence cheaper recommendations applied at pipeline start |
| **Per-file escalation retry** | `code_agent.py` | Failed cheap file gen retries on `resolve_model_for_file(..., escalate=True)` before CLI failover |

Env: `SKYN3T_CHEAP_SMART=1` (default), `SKYN3T_CHEAP_SMART=0` to restore quality-first `or_strong` code routing.

Tests: `tests/test_cheap_smart.py`

---

### Done in proof-run + skills hub pass (2026-06-11)

| Feature | File(s) | Notes |
|---------|---------|-------|
| **Autonomous proof runs** | `skyn3t/studio/proof_run.py`, `cortex/autonomous_loop.py` | After `PROJECT_COMPLETED` on autonomous builds, runs BuildVerifier-equivalent check; fail-closed queues failure brief |
| **Skills Hub** | `skyn3t/intelligence/skills_hub.py` | Install from `examples/skills_seed/` + `skills/`; auto-install + draft auto-approve in no-approval mode |
| **Hub API + CLI + REPL** | `web/app.py`, `cli/main.py`, `cli/repl.py` | `GET/POST /api/skills/hub`, `skyn3t skills hub --install`, `/skills install hub` |
| **Dashboard tiles** | `OverviewPage.tsx`, `api/client.ts` | Autonomous loop status + OpenRouter catalog on Overview |

Tests: `tests/test_proof_run_and_skills_hub.py`

### Never-stop improvement flywheel (2026-06-11)

| Feature | File(s) | Notes |
|---------|---------|-------|
| **ContinuousImprovementEngine** | `skyn3t/cortex/continuous_improvement.py` | Single asyncio coordinator; `SKYN3T_CONTINUOUS_IMPROVEMENT=1` (default on) |
| **Loop A — outcome learning** | `continuous_improvement.py`, `runner.py` | Every `PROJECT_COMPLETED`/`PROJECT_FAILED`: scoreboard flush, routing check, `reviewer_score` in payload |
| **Loop B — model freshness** | `model_evolution.py`, `openrouter_catalog.py` | 6h catalog sync + tier evolution; per-tick `cheap_smart.auto_apply_cheaper_routing` |
| **Loop C — competitive drills** | `continuous_improvement.py`, `autonomous_loop.py` | Scout competitor ingest → micro practice brief via `enqueue_brief` (1/day cap) |
| **Loop D — regression guard** | `continuous_improvement.py` | Rolling reviewer avg per stack in `data/improvement_metrics.json`; auto tier bump + cortex proposal |
| **Loop E — visibility** | `GET /api/improvement/status`, `IMPROVEMENT_TICK` events | Flywheel health: last tick, builds today, score trend, model evolutions |
| **NeverStopWatchdog** | `skyn3t/cortex/never_stop.py` | Every 30s: restart dead improvement/autonomous/fleet tasks; `NEVER_STOP_RECOVERED` alerts |
| **Queue replenishment** | `autonomous_loop.replenish_queue_if_stale`, watchdog | After 5m empty queue → synthetic briefs from build-pattern gaps + competitive intel |
| **Process wrapper** | `scripts/never_stop.sh` | Optional shell loop restarts web server if port 6660 dies |

**Flywheel prose (how it runs forever):**

On orchestrator boot, `CortexBootstrap` starts `continuous_improvement` alongside `autonomous_loop` and the `never_stop` watchdog last. Loops tick immediately on boot (no warmup sleep) with a 30s minimum interval when `SKYN3T_NEVER_STOP=1`. The improvement engine publishes `IMPROVEMENT_TICK` on the event bus each tick (`SKYN3T_IMPROVEMENT_TICK_SECONDS`, default 600). Each Studio build completion feeds Loop A: the build scoreboard flushes, reviewer scores roll into per-stack windows, and adaptive routing health is checked. Every six hours Loop B syncs OpenRouter and runs `model_evolution` tier upgrades; on every tick high-confidence “use cheaper” routing recommendations apply when trajectory proves strong-tier overkill. Loop C watches applied scout ingest proposals for known competitors and queues one internalizing practice build per day into the autonomous queue (requires `SKYN3T_AUTONOMOUS_BUILDS=1`). Loop D compares rolling reviewer averages against `SKYN3T_IMPROVEMENT_SCORE_REGRESSION` and auto-bumps code tier plus files a cortex proposal when quality regresses. Spend stays bounded by reusing autonomous daily caps for competitive drills and proof retries.

The watchdog monitors background asyncio tasks; if any die, it stops/restarts the component within 30s and logs `NEVER_STOP_RECOVERED`. When the brief queue stays empty for `SKYN3T_NEVER_STOP_QUEUE_EMPTY_SECONDS` (default 300), synthetic practice briefs refill slots without bypassing daily build caps. `GET /api/improvement/status` includes `never_stop`, `last_recovery_at`, and `uptime_seconds`.

Env: see `.env.example` improvement section. Tests: `tests/test_continuous_improvement.py`, `tests/test_never_stop.py`.

### Competitive intel pass (2026-06-11)

Repos analyzed: Hermes, MetaSwarm, Forge, Railyard, Ark, ATC, Ruah, Karajan, OpenClaw, Paperclip, gbrain, BEADS.

| Adopted | File(s) | Source inspiration |
|---------|---------|---------------------|
| **Competitor catalog + scout queries** | `skyn3t/cortex/competitive_intel.py`, `repo_scout.py` | Hermes, Forge, OpenClaw, MetaSwarm, etc. |
| **Structured adaptation briefs** | `scout_adaptation.py` | Forge/MetaSwarm spec-driven proposals |
| **Studio token budget cap** | `runner.py`, `SKYN3T_STUDIO_TOKEN_BUDGET` | Forge per-run cost caps |
| **Pipeline checkpoint resume** | `runner.py` `pipeline_checkpoint` | Forge/Ark/OpenClaw pause-resume |

**Top 3 remaining gaps vs Hermes + field:**

1. **Messaging channel parity** (~13 vs ~22) + unified cron→gateway delivery
2. **Gateway + serverless backends** — Modal/Daytona + cron→gateway delivery for VPS ops story
3. **Homelab proof brief** — run autonomous build + `./scripts/studio_smoke.sh` on a real dashboard brief end-to-end

**Next priorities (historical — see top of doc):**

1. **Messaging channel parity** — add 1–2 high-value channels or unified cron→gateway stub
2. **Homelab proof brief** — run autonomous build + `./scripts/studio_smoke.sh` on a real dashboard brief end-to-end
3. **Studio/Cortex route UI polish** — match new Overview design on Agents/Cortex pages

### Done in fleet-backpressure + worktree pass (2026-06-11)

| Feature | File(s) | Notes |
|---------|---------|-------|
| **Studio concurrency cap** | `agent_fleet.py`, `settings.py` | `SKYN3T_AGENT_FLEET_MAX_CONCURRENT_BUILDS=5` (default) decouples fleet slots from Studio semaphore |
| **Non-blocking fleet status** | `agent_fleet.py`, `web/app.py` | 2s cached snapshot + `asyncio.to_thread` on `/api/fleet/status` |
| **Scout off boot path** | `continuous_improvement.py` | Fleet scout uses `start_background`; `SKYN3T_CORTEX_SCOUT_DEFER_BOOT_SECONDS=120` |
| **Git worktree helper** | `skyn3t/worktree.py`, `scripts/worktree.sh` | Per-slot worktree under `.worktrees/`; autonomous builds pass `worktree=True` |
| **CodeAgent worktree dir** | `runner.py`, `code_agent.py` | `code_scaffold_dir` override when worktree enabled |

Env: `SKYN3T_AGENT_FLEET_MAX_CONCURRENT_BUILDS=5`, `SKYN3T_CORTEX_SCOUT_DEFER_BOOT_SECONDS=120`, `SKYN3T_STUDIO_WORKTREE=1`

Tests: `tests/test_agent_fleet.py`, `tests/test_continuous_improvement.py`, `tests/test_worktree.py`

---

## Hermes competitive check (live, 2026-06-11)

**Repo:** [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) — latest **v2026.6.5** (Hermes Agent v0.16.0).

Recent SkyN3t quality work was **not** validated against live Hermes at build time; this section records the first explicit comparison.

| SkyN3t leads | Hermes leads |
|--------------|--------------|
| Project Studio → runnable `scaffold/` | Desktop app, TUI, one-line install wizard |
| Verification sandwich (contract → build/boot/integration) | ~22 messaging channels vs SkyN3t ~13 |
| Cross-model pipeline critique | 6 execution backends (Modal, Daytona, SSH, …) |
| Cortex + GitHub scout + build scoreboard | Skills Hub, slash commands, autonomous skill writes |
| PackagingAgent + ACP server | Cron → gateway delivery, external memory providers |

**Top 3 gaps to close next (updated from live Hermes):**

1. **Gateway + cron + serverless** — unified messaging delivery + Modal/Daytona backends
2. **Homelab proof brief** — validate autonomous loop + proof runs on a real homelab dashboard build
3. **Messaging channel parity** — ~13 vs Hermes ~22 channels

---

## Cursor + Fleet dual loop (2026-06-11)

Two never-stop improvement paths run in parallel:

| Loop | Improves | Mechanism |
|------|----------|-----------|
| **Fleet** | `PROJECTS_DIR` scaffolds | `AgentFleetCoordinator` + `AutonomousCoordinator` + Studio pipeline |
| **Cursor** | This repo (`skyn3t/`) | Cursor IDE agents + optional Cursor Automation |

### Fleet (autonomous Studio)

- Env: `SKYN3T_AGENT_FLEET_SIZE=20`, `SKYN3T_AUTONOMOUS_BUILDS=1`, `SKYN3T_CONTINUOUS_IMPROVEMENT=1`
- APIs: `GET /api/fleet/status`, `GET /api/improvement/status`
- Boot seeds competitive practice briefs via `seed_startup_briefs()`; dispatcher uses `SKYN3T_AGENT_FLEET_TICK_SECONDS` (default 30s)

### Cursor (orchestrator repo)

| Artifact | Purpose |
|----------|---------|
| `.cursor/rules/continuous-improvement.mdc` | Agent rule: read CONTINUE.md, small diffs, run tests |
| `.cursor/automations/skyn3t-continuous-improvement.json` | Automation prefill (weekday 09:00 cron) |
| `.cursor/automations/README.md` | Enable steps in Cursor Automations UI |
| `data/cursor_tasks.json` | Queue written by improvement flywheel |
| `skyn3t/cortex/cursor_improvement.py` | Enqueue on regression / competitive scout |
| `scripts/cursor_improve.sh` | Print next task + fleet/improvement smoke + pytest subset |

**Enable Cursor Automation:** Cursor → Automations → New → prefill from `.cursor/automations/skyn3t-continuous-improvement.json` → set repo `Choaterboater/skyn3t-orchestrator` → save.

**Manual chat:** `Process cursor_tasks.json` or `./scripts/cursor_improve.sh`

**Regression / scout → Cursor:** `ContinuousImprovementEngine` calls `enqueue_regression_task()` and `maybe_enqueue_from_competitive_adaptation()` so IDE agents can fix SkyN3t itself while fleet drills stay in `PROJECTS_DIR`.

Tests: `tests/test_cursor_improvement.py`

---

## Resume prompt (copy-paste to next agent)

Use the **Resume prompt** block at the top of this document (canonical). Also process `data/cursor_tasks.json` if the queue has items.

---

## Key code locations

| Concern | Path |
|---------|------|
| Studio pipeline | `skyn3t/studio/runner.py` |
| Critique + cross-model | `runner._critique_and_revise` (~3579) |
| Final outcome gate | `runner._finalize_project_outcome` (~4842), `REVIEWER_SCORE_THRESHOLD = 80` |
| Contract checks | `skyn3t/agents/contract_engine.py` → `check_contract()` |
| Reviewer scoring | `skyn3t/agents/reviewer.py` |
| Approval gates | `skyn3t/studio/approval_gate.py`, `data/approval_gates.json` |
| Mission / product bar | `docs/MISSION.md` |
| Score troubleshooting | `docs/how-to-raise-studio-score.md` |

---

## Architecture reminder

SkyN3t Studio pipeline (code builds):

```
brainstorm → research → architect → designer → code
  → contract_verifier → packaging → consistency_reviewer → reviewer
  → [build_verifier → boot_verifier → integration_verifier]
```

Inter-agent critique runs after most stages (not brainstorm/reviewer/verifiers).
Product bar: **runnable software**, not markdown theater (`docs/MISSION.md`).
