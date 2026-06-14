# SkyN3t Build-Loop Scorecard & Fix Plan

**Resume point for a fresh session.** Walk the pipeline in order, score each step, fix the lowest-scoring high-leverage ones, re-score. The core loop is reliable as of 2026-06-14 (builds compile, median **73–80**); the goal now is raising quality toward the **85 ship bar** and closing the remaining capability gaps.

---

## How to score (the method — do this each round)

1. **Set up clean:** server on `main`, isolated env — `SKYN3T_AUTONOMOUS_BUILDS=0`, cortex loops off (`SKYN3T_AUTONOMOUS_LEARNING/CONTINUOUS_IMPROVEMENT/MODEL_EVOLUTION=0`), `SKYN3T_OPENROUTER_MAX_CONCURRENCY=2`. Restart the server so it loads current code.
2. **Run ONE paced build** (never concurrent — it starves itself through the cap):
   `skyn3t project "<a react_vite dashboard brief>" --autonomy move_fast --no-watch`, then watch `Projects/<slug>/project.json` to a terminal status.
3. **Walk each stage's artifact** (`Projects/<slug>/*.md`, `scaffold/`, `review.md`) and score 1–10 against "what good looks like" below. The final `quality_summary.score` is the aggregate.
4. **Fix the lowest-scoring × highest-leverage step. Re-score.** One change at a time; gate on the build actually moving.

Baseline scores below are my 2026-06-14 estimate. **Overwrite them as you measure.**

---

## The pipeline — score in order

| # | Step | Inspect | Base | What good looks like / the gap | Fix |
|---|------|---------|:---:|--------------------------------|-----|
| 1 | Brief intake / clarify | `project.json` brief_expanded | 6 | every implied + specified feature captured | catches features; **drops some specified behaviors** (e.g. "live updates") → add a feature-completeness check |
| 2 | Brainstorm | `brainstorm.md` | 6 | real alternatives, not filler | OK |
| 3 | Research | `research.md` | 6 | concrete API/domain facts | OK (was a 403 killer; fixed) |
| 4 | Architect | `architecture.md` | 6 | coherent file/API plan | OK |
| 5 | **Design system** | `brand.md` / `components.md` / `tokens.css` | **8 ✅** | distinctive, specific, reasoned | **strong — not the weak link** |
| 6 | Codegen | `scaffold/src/` | 6 | applies the spec, no stubs, real features, TS when asked | spec-adherence shipped (`e4fc5af`) — **validate it**; stubs under 429 now rescued (`7945dc4`) |
| 7 | Build verify (compiles) | `build_verification` | **7 ✅** | honest `npm run build` gate | OK (`8034833`, gates don't false-fail) |
| 8 | **Functional verify (works?)** | — | **4 ❌** | proves the app actually FUNCTIONS — data flows, clicks work | **biggest gap: build a real functional smoke (Playwright drives the app + checks behavior)** |
| 9 | Boot verify | `boot_verification` | 6 | rejects hollow apps | OK (`979c319` SPA-catch-all) |
| 10 | Consistency review | `consistency_check` | 6 | flags real first-party defects only | OK (`adf71bc` stops node_modules) |
| 11 | Self-repair loop | `build_fix_attempts` | 6 | diagnoses → fixes → re-verifies, doesn't blind-retry | grounds/learns/stuck-stops/429-resilient (`f0a3ffc`,`abf2613`,`50cf058`); **reach limited** — only template paths rescued when LLM can't |
| 12 | Vision design grade | `visual_verification` | 6 | a vision LLM critiques the real render | shipped (`9c6fa9d`, gpt-4o-mini ~1.4s); **calibrate `SKYN3T_VISUAL_MIN_SCORE` before trusting hard-fails** |
| 13 | Reviewer scoring | `review.md` / `quality_summary` | 6 | honest, build-aware score + actionable deductions | honest re-score (`f931b6c`); still a subjective LLM judge |
| 14 | Package / ship | packaging fields | 5 | runnable handoff (README, deps, run cmd) | **assess — under-measured** |
| 15 | **Learning loop (cross-build)** | `/Volumes/Projects/skynetllm/playbook.json` | **3 ❌** | reviewer deductions → next build avoids them, scores rise | **mechanism exists, currently OFF, never shown to move a score** — re-enable + measure |

## System / capability areas

| Area | Base | Gap | Fix |
|------|:---:|-----|-----|
| Throughput / rate limits | 4 | capped to 2 to dodge 429s → slow; can't scale | fund/raise the OpenRouter key limit (owner); then raise concurrency |
| Multi-build reliability | 3 | single-build-reliable; concurrent builds starve each other | admission control / per-build quota / pause-cortex-during-build |
| Breadth | 3 | proven only on React/Vite dashboards in one domain | test FastAPI/backend, other app types, other design languages |
| Operational robustness | 5 | footguns (paths, uncapped spend, restart-to-load) mostly fixed today | daily-spend cap, restart discipline, config validation, observability |

---

## Priority fix order (lowest score × highest leverage)

1. **Functional verification (#8, score 4)** — make verification prove the app *works*, not just compiles + looks good. This makes the 73–80 score *honest* and gives the learning loop a real target. **Highest leverage.**
2. **Prove the learning loop (#15, score 3)** — re-enable cortex learning (with a daily spend cap), run a batch, show the median rises. Without this, "self-improving" is unproven.
3. **Validate today's codegen+design fixes (#6, #12)** — one paced build; confirm spec-adherence + vision deductions move a score toward 85.
4. **Multi-build reliability + throughput** — admission control; fund the key.
5. **Breadth** — prove a non-dashboard / backend build.

---

## Start here (next session)

**Step 0 — restart + one paced validation build** (loads `e4fc5af` spec-adherence + `9c6fa9d` vision grading, neither yet proven on a live build). Score steps 6, 8, 12 against it.
**Step 1 — build Functional Verification (#8)** — the single highest-leverage fix.

Context: all work is on GitHub `main` (`github.com/Choaterboater/skyn3t-orchestrator`). The owner's pre-session `temp` work is in `git stash@{0}`+`{1}`. Local `.env` toggles (autonomous/cortex off, concurrency 2) are gitignored — re-enable deliberately, with caps. See the auto-memory `skyn3t-learning-loop-state` for the full day's arc.
