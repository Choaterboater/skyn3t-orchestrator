# Issue: Code-generation reliability on integration-heavy briefs

**Date:** 2026-05-14
**Last updated:** 2026-05-15
**Status:** Partially mitigated — see "Resolution status" at the bottom.
**Severity:** High — current bottleneck on success rate
**Affects:** v37, v38, v45, v45-retry, v46 (all homelab dashboard runs since infra fixes landed)

## Summary

The studio pipeline's infrastructure (planner, critique loop, consistency engine,
build/boot/integration verifiers, retry cap, leak guards, CLI subprocess sandbox)
is healthy. Three back-to-back runs (v45, v45-retry, v46) all reached the verifier
phase without crashes, leaks, or retry storms.

But all three **failed verification** — for different small wiring reasons each
time. The remaining bottleneck is not the infrastructure. It is the code-generation
agents writing scaffolds that *almost* work but consistently miss one or two
load-bearing wires.

## Failure modes observed

### Class 1 — Missing router mount (v45, v45-retry)

`server/routes/config.js` exists with real `router.get("/")`, `router.put("/:slug")`,
`router.post("/:slug/test")` handlers — but `server/index.js` never calls
`app.use("/api/config", configRouter)`. So the frontend hits 404 on every
`/api/config*` call. IntegrationContractVerifierAgent caught this correctly:

```
Integration contract FAILED: 3 frontend route(s) have no backend handler.
Missing: /api/config, /api/config/:*, /api/config/:*/test
```

The router file itself is well-written. It's the import + mount line that gets
dropped between code stage rounds.

### Class 2 — Frontend build failure (v46)

Reviewer scored 62/100 (`go-with-fixes`), consistency reviewer found 2 blockers
and fixed them, then `npm run build` failed during `vite build`. The pipeline
ran cleanly through the entire critique → fix loop, then died at the final
`vite build` gate.

```
Build FAILED (node): /opt/homebrew/bin/npm run build --silent
```

Stack-aware deterministic templates already lock in `package.json`, `vite.config.js`,
`index.html`, and `src/main.jsx`. The build error must be in an LLM-written file
(an import that doesn't resolve, a JSX/TS error, or a missing devDep).

### Class 3 — Bonus output lost to sandbox (all runs since cwd patch)

The cwd patch (`llm_client.py:_run_capture` now passes `cwd=mkdtemp()`) successfully
stops claude CLI from writing files to the SkyN3t repo root. But it ALSO means the
files claude CLI writes on its own initiative — observed today in
`/var/folders/.../skyn3t-llm-cwd-567hfihu/` — never reach the scaffold:

```
sandbox/server/...
sandbox/src/components/SettingsModal.jsx
sandbox/src/components/ServiceEditor.jsx
sandbox/index.html
sandbox/package.json
sandbox/vite.config.js
```

These contain real component code. The pipeline only reads stdout and ignores
sandbox files. So content gets generated then discarded. **This is likely
*contributing* to Class 1** — claude CLI may write the corrected `index.js` (with
the missing mount line) into the sandbox, but the pipeline only sees stdout
and writes a different version (without the mount) to the actual scaffold.

## Why the existing safety nets miss it

| Layer | Catches | Misses |
|---|---|---|
| Critique loop (3 rounds) | "is this file internally consistent" | "is this file wired into the rest of the project" |
| Consistency engine | broken imports, missing files, hallucinated services | "router exported but never mounted" |
| Consistency reviewer (LLM) | semantic cross-file truth | depends on what the reviewing LLM happens to notice |
| Reviewer | overall code quality, brief coverage | scores `go-with-fixes` but doesn't BLOCK |
| Build verifier | `npm install + vite build` | **can't run if scaffold doesn't compile** — and `node --check` passes orphan routers fine |
| Boot verifier | server starts and serves /api/health | doesn't curl arbitrary `/api/*` routes |
| Integration verifier | composed `app.use(prefix) + router.method(subpath)` route map vs. frontend fetches | only catches missing mounts AFTER scaffold is otherwise OK |

The integration verifier is the only layer that catches Class 1 — and it runs
LAST. By the time it fails, the entire pipeline has run, costing wall time and
tokens.

## Proposed fixes (ranked by ROI)

### 1. Mount checker in consistency engine (cheap, big win)

Add a static check: for each file under `server/routes/*.js` that defines an
Express `Router()` and exports it, search `server/*.js` for a matching
`app.use(<some-prefix>, <imported-name>)` line. If missing, flag as a blocker
with a suggested fix:

```
ISSUE: server/routes/config.js exports a Router but no `app.use(...)` mount
       was found in server/index.js or any server/*.js entry.
SUGGEST: add `import configRouter from './routes/config.js'` and
         `app.use('/api/config', configRouter)` to server/index.js.
```

This catches Class 1 BEFORE the integration verifier — saves the boot+integration
phase for runs that would have failed there.

Pure-Python regex check, no LLM calls. ~50 lines in
`skyn3t/agents/consistency_engine.py`. Mirrors how the existing import-graph
check works.

### 2. Sandbox-content harvesting (medium, partial win on Class 1)

After every `_run_capture` call, scan the sandbox dir for files newer than the
call started. For each new file:

- If the path matches a file the pipeline expected (e.g. `server/index.js`),
  prefer the sandbox version over what stdout returned.
- Log the harvested content as a `sandbox_artifact` event so we can see what
  claude CLI was *actually* writing.

This means claude CLI's "I'll just write this whole file" instinct stops being
wasted. ~80 lines in `llm_client.py`.

Risk: the sandbox is a tmpdir, but content from one call could be misattributed
to a later call if not cleared. Cheap fix: add a `sandbox_seq` counter and only
harvest files mtime-newer-than the call's start.

### 3. Frontend build dry-run inside critique loop (medium, win on Class 2)

When CodeAgent finishes a code stage, run `vite build --mode=development` against
the scaffold's `web/` (or root) dir BEFORE handing off to consistency reviewer.
On failure, feed the build error to the critique loop as a 4th round so the
LLM can fix it in-loop instead of waiting for the final BuildVerifier.

This shifts a 30-min "fail at the end" into a 2-min "fix during critique."

Risk: `vite build` is slow (~20s) and adds wall time to every run, including
ones that would have built fine. Mitigation: only run when CodeAgent flagged
that any frontend file was touched in the latest round.

### 4. Per-class scoreboard rule (small, learning-based)

Once the mount checker (#1) lands, record `(stack, shape, "missing_mount")` to
the build patterns scoreboard. After N occurrences, the planner can pre-warn
CodeAgent: "this scaffold shape historically loses the config router mount —
double-check `server/index.js` includes all `routes/*.js` imports."

## Expected impact

- v45/v45-retry would have been caught by #1 before integration verifier ran
  (~7 min saved per failed run, ~$X tokens saved).
- v46 would have been caught by #3 before the final BuildVerifier ran (~3 min
  saved).
- All three would have had a chance to self-fix in the critique loop instead
  of failing the run, raising the success rate from 0/3 toward 2-3/3.

## What's healthy and should NOT be touched

- The retry-storm cap (slug-based) is doing its job; v45 spawned 1 retry, v46
  spawned 1 retry, neither cascaded.
- The leak guards (path-escape rejection in `targeted_fix` + `_write_scaffold_files`,
  cwd sandbox in `llm_client._run_capture`) are doing their job; repo root has
  stayed clean across 3 consecutive runs.
- The critique loop is firing meaningfully — research, architect, and code
  stages each go through 1-3 rounds and `CODE_CRITIQUE_FIX_APPLIED` is producing
  real diffs, not placebo events.
- The integration verifier route composition fix (`app.use(prefix) + router.method`
  composition) is correctly capturing the mounted route shape; it's the absence
  of the mount, not the verifier's logic, that's the issue.

## References

- Code-stage timing: v45 6m, v45-retry 18m, v46 17m (consistency reviewer
  is the variable — it ran 8m for v45-retry's 4 blockers, 0:14 for v46's 2)
- Reviewer scores: v45 72, v45-retry 65, v46 62 (all `go-with-fixes`, all
  below the 75 threshold for `done`)
- Sandbox dir for current backend (PID 75020): `/var/folders/yj/.../skyn3t-llm-cwd-567hfihu/`
- Latest scaffold under inspection: `~/Documents/skyn3t/Projects/homelab-dashboard-v46/scaffold/`

## Resolution status (2026-05-15)

| Proposal | Status | Where |
|---|---|---|
| #1 Mount checker in consistency engine | **Landed** | `skyn3t/agents/consistency_engine.py:118` `_find_missing_router_mounts()`, wired in `check_consistency` at `:555`; covered by `tests/test_consistency_engine.py::test_consistency_flags_unmounted_router` + `::test_consistency_accepts_mounted_router`. Suggestion text and mount-prefix synthesis match this issue's spec. Tag on `BuildPatternScoreboard` (proposal #4) not yet implemented. |
| #2 Sandbox-content harvesting in `_run_capture` | **Landed (partial)** | `skyn3t/adapters/llm_client.py:425` `_collect_sandbox_artifacts()` + `:454` `_append_sandbox_artifacts()`. Harvest filters by mtime; appends via `// === <relpath> ===` markers. Sandbox cwd is now per-call with cleanup (BR-012 fix on 2026-05-15) — previously a process singleton. Residual: marker syntax is JS-comment-shaped and confuses downstream parsers when content is Python/HTML. |
| #3 Frontend `vite build` dry-run inside critique loop | **Open** | Not implemented. Would shift Class-2 failures (v46) from end-of-run to inside critique. |
| #4 Per-class build-pattern scoreboard rule (`missing_mount` tag) | **Open** | The mount checker exists, but the build-patterns scoreboard doesn't tag `(stack, shape, "missing_mount")` yet, so the planner can't pre-warn. |

Class-1 (missing mount) and Class-3 (sandbox content lost) are now caught
by the consistency engine before reaching the integration verifier, and
by the harvest step before stdout is read. Class-2 (frontend build
failure inside critique) is still the remaining open work.
