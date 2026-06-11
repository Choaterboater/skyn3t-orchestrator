# SkyN3t — Session Continuation Handoff

Use this file when a chat runs out of tokens or you start a fresh agent session.
Branch: `cursor/github-scout-dashboard-ui` (dirty working tree as of 2026-06-11).

---

## Session summary

Work across this thread focused on **fixing CI-quality issues** and **raising Studio build quality to beat Hermes**.

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
| Cache | `data/openrouter_models.json` (24h TTL) |
| Auto-sync | Orchestrator/web start + daily background loop when `OPENROUTER_API_KEY` set (`SKYN3T_OPENROUTER_SYNC=1`, default on) |
| API | `GET /api/models/openrouter` (`?refresh=1` to force) |
| CLI | `skyn3t models sync` (`--force`) |
| Routing | `model_router._tier_backend_model()` validates tier ids; missing → keyword fallback + warning |

---

## Modified files (git status snapshot)

**Tracked modifications:**

```
.env.example
README.md
docs/MISSION.md
pyproject.toml
requirements.txt
skyn3t/agents/build_verifier.py
skyn3t/agents/code_agent.py
skyn3t/agents/contract_engine.py
skyn3t/agents/contract_verifier.py
skyn3t/agents/reviewer.py
skyn3t/agents/reviewer_fixes.py
skyn3t/cli/main.py
skyn3t/cli/repl.py
skyn3t/core/orchestrator.py
skyn3t/cortex/build_pattern_bias.py
skyn3t/studio/runner.py
tests/test_build_pattern_bias_apply.py
tests/test_generation_optimization.py
tests/test_scout_adaptation.py
tests/test_studio_approval_cli.py
```

**New untracked (should be committed with this work):**

```
docs/CONTINUE.md          ← this file
docs/REBUILD_PLAN.md      ← updated matrix (in progress)
tests/test_contract_entrypoint_wiring.py
```

**Untracked noise (do not commit):** `.obsidian/`, `*.canvas`, `2026-05-27.md`, `FETCH_HEAD`

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
| `SKYN3T_SKILLS_HUB_AUTO_INSTALL` | `1` | Seed hub skills on orchestrator start when no-approval |
| `SKYN3T_SKILLS_HUB_PATHS` | `examples/skills_seed,skills` | Comma-separated hub roots |
| `SKYN3T_CODE_TIER` | unset | Override code stage tier (`or_backend`, `or_cheap`, …); beats default `or_strong` |
| `execution_profile` | `balanced` | Pass `deep` on CLI/API for max critique rounds + fix budget |

Example max-quality build:

```bash
skyn3t project --execution-profile deep "Build a habit tracker with streaks and dark theme"
```

Documented in `.env.example`.

---

## Next priorities (ordered)

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

### Done in proof-run + skills hub pass (2026-06-11)

| Feature | File(s) | Notes |
|---------|---------|-------|
| **Autonomous proof runs** | `skyn3t/studio/proof_run.py`, `cortex/autonomous_loop.py` | After `PROJECT_COMPLETED` on autonomous builds, runs BuildVerifier-equivalent check; fail-closed queues failure brief |
| **Skills Hub** | `skyn3t/intelligence/skills_hub.py` | Install from `examples/skills_seed/` + `skills/`; auto-install + draft auto-approve in no-approval mode |
| **Hub API + CLI + REPL** | `web/app.py`, `cli/main.py`, `cli/repl.py` | `GET/POST /api/skills/hub`, `skyn3t skills hub --install`, `/skills install hub` |
| **Dashboard tiles** | `OverviewPage.tsx`, `api/client.ts` | Autonomous loop status + OpenRouter catalog on Overview |

Tests: `tests/test_proof_run_and_skills_hub.py`

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
2. **Git worktree parallel isolation** (Railyard/ATC/Maestro) for multi-agent same-repo edits
3. **Gateway + serverless backends** — Modal/Daytona + cron→gateway delivery for VPS ops story

**Next priorities (ordered):**

1. **Git worktree helper** — `skyn3t/worktree` util + Studio option for parallel CodeAgent edits
2. **Messaging channel parity** — add 1–2 high-value channels or unified cron→gateway stub
3. **Homelab proof brief** — run autonomous build + `./scripts/studio_smoke.sh` on a real dashboard brief end-to-end

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

1. **Git worktree parallel isolation** — Railyard/ATC pattern for multi-agent same-repo edits
2. **Gateway + cron + serverless** — unified messaging delivery + Modal/Daytona backends
3. **Homelab proof brief** — validate autonomous loop + proof runs on a real homelab dashboard build

---

## Resume prompt (copy-paste to next agent)

```
Read docs/CONTINUE.md and continue the Hermes-beating quality work from the next priorities list.
```

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
