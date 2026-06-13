# SkyN3t — Session Handoff (2026-06-13)

> Read this first next session. It captures the state, what was fixed, the honest
> remaining problem, and exactly where to pick up. Companion to `STATUS.md`.

## TL;DR

The build loop went from **dead (~0% ship, every build died at the *research*
stage on a `403`)** to **completing all 9 stages and generating a React app that
`npm run build` compiles**. Every *systemic* blocker is fixed and on `main`. But
**reliable *passing* (reviewer "go", score ≥85) is NOT yet demonstrated** —
recent builds still score ~53–62. That last gap is genuine multi-stage codegen
*quality/fragility*, not a single bug.

**Last passing build before this session: ~49h ago (a React dashboard, score
100). Done-builds total is still 17** — no *new* pass landed during the session
(post-fix builds were still in-flight at close). Verify the trend next session.

## Live state at handoff

- Server: `skyn3t start --host 127.0.0.1 --port 6660`, running **detached** (survives sessions). Restart pattern in "Operational reference" below.
- `main` == `origin/main` (GitHub `Choaterboater/skyn3t-orchestrator`). All work pushed.
- OpenRouter key cap **raised $20 → $100** (owner); `.env` `SKYN3T_FREE_ONLY=0` → **paid ladder active, 0×403**. ~$80 credit remaining.
- Tiers pinned via the dashboard per-tier picker: `deepseek/deepseek-v4-flash` (cheap) + `deepseek/deepseek-v4-pro` (strong). Pins are LOCKED (evolution won't override).
- `.env`: `SKYN3T_NO_CLAUDE=1`, `AGENT_FLEET_SIZE=5`, `AUTONOMOUS_BUILD_DAILY_BUDGET_USD=26`.
- Skills: ~30–41 (fluctuates). The 24 imported `addyosmani/agent-skills` persist.

## Root cause of the recent 0% ship-rate (debug-swarm verified)

1. **PAID→FREE regression** (commit `c5e4e90`, ~2 days ago): when the $20 key
   exhausted, build tiers were flipped to `:free` models → broken scaffolds.
   *Already reverted* (FREE_ONLY=0 + funded ladder).
2. **Dead self-heal loop** (the big one): the build-fix-and-reverify loop in
   `runner.py` (~2822) was gated on `skip_fix_loops = reviewer_failed`. The
   ReviewerAgent runs *before* the build verifier and always no-go's a broken
   scaffold, so `reviewer_failed` was always True for exactly the builds with a
   fixable break → the self-healing loop was **permanent dead code**. Builds
   shipped broken (e.g. `import { X }` vs `export default X` → vite/rollup fails),
   scored ~53, never repaired. Fixed in `0413e53`.
3. **Self-poisoning autonomous briefs**: `_maybe_quality_retry` re-wrapped a
   FAILED drill's already-wrapped brief → "rebuild rebuild rebuild…" compounding,
   `[:180]` truncation buried the real spec → garbage brief → guaranteed fail.
   Fixed in `03b6e93`.

## This session's commits (all on `main`, in order)

```
ba9ed70  Free-only routing + strip stale claude/paid config + anti-theater gate
011571e  Model Routing free-only TOGGLE (dashboard)
af51b48  Per-tier model PICKER + reset (dashboard)
88ba6ee  Gitignore .coverage / mypy / ruff caches
f543d4b  Skill install from git: import the WHOLE repo (all SKILL.md), not one
f285d9a  (Skill curator grace-period — REVERTED)
98a27af  Revert of f285d9a
00d6a06  Skills reach builds: relevance retrieval + multi-format importer
63d03a6  Skills stop vanishing: SkillLibrary honors DATA_DIR (was cwd-relative) — test-isolation bug that deleted real skills
c19cd89  Model selection: prefer newer + cheaper (version recency, de-bias name keywords, refresh stale ladders)
aba6a60  Autonomous uses cheap PAID when funded (was forced-free → 429 wall)
c410fde  OpenRouter concurrency throttle (SKYN3T_OPENROUTER_MAX_CONCURRENCY=4) — 429 wall → survivable
28ac715  (Codegen ladder via cheapest_paid — REVERTED, surfaced junk 1-2B models → more stubs)
d306e21  Revert of 28ac715
a4dc51b  Codegen ladder: lead with reliable QUALITY models when funded (correct fix)
0413e53  Self-heal broken builds: decouple build-fix loop from reviewer + re-score after repair
1975e03  Fix 2 crash bugs: prune date math (×4 .replace(day=) → timedelta) + token-totals KeyError
03b6e93  Fix self-poisoning autonomous recovery-drill brief
```

## The honest remaining problem

The loop is **multi-stage fragile** (~9 sequential stages; product of per-stage
success rates caps ship-rate). Reviewer scores are *correct* — failing builds
genuinely don't compile or are incomplete; it is NOT a harsh rubric. The path to
reliable passing is hardening the stages, not one more fix.

Two reviewer bugs (secondary, make it too LENIENT not too harsh — from the swarm):
- When `_llm_review` returns None, score floors to ~53 even for a non-building scaffold (`reviewer.py` ~230/877).
- The H26 build-verification cap can't fire because the reviewer runs BEFORE build verification (`runner.py:1374` reads `build_verification` in-loop; verifier runs post-loop) → reviewer is structurally blind to whether the scaffold compiles.

## Next steps (priority order)

1. **Verify the trend** — autonomous loop runs continuously. Check `Projects/*/project.json` for builds whose `completed_at` is AFTER 2026-06-13 ~20:00, or `GET /api/improvement/status`. If post-fix drills hit ≥85 → loop restored.
2. **Confirm the self-heal loop actually fires** — one drill showed `build_verification:no` but `build_fix_attempts:None`. Check whether `_apply_build_fix_round` engages + can repair the import/export break, on a build that *reaches codegen*.
3. **Reviewer ordering** — run build/boot verification BEFORE the reviewer (or re-apply the H26 cap after), so a non-compiling scaffold can't score 53.
4. **Generation gate** — add an import-resolution check for entrypoint `.jsx/.tsx` in `code_agent.py` (verify `import {X}` matches the sibling's export) so the break never ships.
5. **#11 Bulk skill import** (still open) — fan out over `officialskills.sh` / the awesome-list (140 repos) using the existing `import_skill_repo`. The single-repo path works (24 addyosmani imported).

## Operational reference

- **Restart server** (loads code changes): `pkill -TERM -f "skyn3t start"`, wait for port 6660 free, then `nohup .venv/bin/skyn3t start --host 127.0.0.1 --port 6660 > logs/server.log 2>&1 < /dev/null &`. **Run from the repo dir** (cwd matters: `.env` + `./data` are cwd-relative). `logs/server.log` is truncated on each restart.
- **Trigger one build (API)**: `POST http://127.0.0.1:6660/api/studio/start` with `{"template":"auto","slug":"…","mission_setup":{"autonomy":"move_fast"},"brief":"…"}`. Poll `GET /api/studio/projects/<slug>`.
- **Smoke-test a build output**: `bash scripts/studio_smoke.sh <slug>` (real `npm install && npm run build`, or `py_compile`).
- **Drive a build in-process** (no server): `StudioRunner(event_bus, projects_root).start(template_key="auto", brief=…, mission_setup={"autonomy":"move_fast"})` — but set `projects_root` to the **configured** `PROJECTS_DIR` (`…/Skyn3t/Projects`), NOT a repo-internal path (the `resolve_artifact_dir` guard rejects repo-internal dirs → files go to scratch).
- **Check OpenRouter**: `GET /api/v1/key` (per-key cap) vs `GET /api/v1/credits` (account). These are DIFFERENT — a $0 key cap 403s even with account credit (that was the 2-day blocker).

## Gotchas (do not relearn the hard way)

- **Restarting the server kills in-flight builds** (they get reaped as interrupted). Don't restart mid-build when measuring; let the autonomous loop run uninterrupted.
- **`cheapest_paid_models` ≠ reliable** — it surfaces tiny 1–2B junk models (qwen-2.5-7b, ling-2.6-flash) that produce stubs. Use the curated quality ladder / pinned models instead.
- **Don't hardcode "model X is throttled"** — provider rate limits are dynamic; that's the stale-assumption anti-pattern. Free selection is already free-first with ladder fallthrough.
- **Skills are gitignored runtime data** (`data/skills/`). Tests used to delete the REAL dir (SkillLibrary ignored DATA_DIR) — fixed in `63d03a6`; don't reintroduce a cwd-relative default.
- **`runner.py` (~8.5k LOC) and `code_agent.py` (~4.8k LOC) are god-classes** — verification/scoring flow is tightly coupled; change surgically + run `pytest -k "studio or runner or reviewer"` (≈298 tests).
- **Build variance is high** — don't conclude from one build. Measure cohorts.
