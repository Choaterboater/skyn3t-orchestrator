# SkyN3t — Session Handoff (2026-06-13)

Read this first next session. It reflects the current worktree implementation
state only; nothing was committed or pushed by this docs pass.

## TL;DR

The worktree now documents the post-implementation operator state: paid
OpenRouter ladder allowed, no-Claude enforced, OpenRouter requests throttled,
skills/GitHub memory paths clarified, and the build loop now uses objective
verifier results for repair and reviewer re-score. Do **not** claim live build
quality is restored until a real cohort proves ReviewerAgent `go` / score >=85.

## Operator state

- **Routing:** `SKYN3T_FREE_ONLY=0` means paid OpenRouter tiers may be used;
  switch to `1` only when forcing a free-only run. `SKYN3T_NO_CLAUDE=1` keeps
  Claude CLI/API and Claude/Anthropic OpenRouter models out of the route.
- **Current OpenRouter pins:** `deepseek/deepseek-v4-flash` handles cheap/UI/
  backend; `deepseek/deepseek-v4-pro` handles strong; docs remain on a free
  docs model. The catalog scorer favors current-generation/newer and cheaper
  near-tier models, while manual locks protect these picks.
- **Throttle/health:** effective OpenRouter concurrency defaults to 4 via
  `SKYN3T_OPENROUTER_MAX_CONCURRENCY` and is visible in `/api/llm/backends`.
  The CLI health check accepts `python3` when `python` is absent.
- **Paths:** run `skyn3t start` from the repo so `.env` is loaded into the
  process. Default `DATA_DIR`/`LOGS_DIR` are repo-local; `PROJECTS_DIR` points to
  the sibling `Skyn3t/Projects` directory, not a repo-internal scratch path.
- **Skills:** Skills Hub roots are repo-relative. `skyn3t skills install` and
  `/api/skills/install` can import a local or git repo with multiple `SKILL.md`
  skills or loose skill-frontmatter markdown. Relevant installed skills reach
  build planning as advisory prompt hints.
- **GitHub memory:** use `skyn3t github ingest ...` or `POST /api/github/ingest`.
  Register/add the agent as `github_ingestor`; orchestrator RAG wiring covers
  both GitHub ingestor and explorer aliases. Ingest status reports missing
  clients/seeds, rate limits, skip counts, and RAG availability without logging
  credentials.
- **Build loop:** build/boot/integration verifiers run even after reviewer
  no-go. Objective failures drive self-heal/fix loops; objective verification is
  passed into reviewer re-score; entrypoint import/export mismatches are repaired
  before build verification.

## Next validation

Automated validation passed, but live pass-rate is still unproven. A controlled
in-process build from the modified worktree reached codegen, contract verifier,
packaging, and then stalled in `consistency_reviewer`; it was stopped rather
than allowed to run indefinitely. Do not count that as a ReviewerAgent `go`.

1. Restart the server from the repo only when no build is in flight.
2. Let a small cohort run and verify whether any post-change build reaches
   ReviewerAgent `go` / score >=85. Until then, launch-quality pass rate remains
   unproven.
3. Spot-check `/api/llm/backends`, `/api/github/ingest`, and Skills Hub install
   flows after restart.

## Operational reference

- Check listener before restart: `lsof -nP -iTCP:6660 -sTCP:LISTEN`; terminate a
  specific PID with `kill <PID>` if needed.
- Start server from repo: `.venv/bin/skyn3t start --host 127.0.0.1 --port 6660`.
- Trigger one build: `POST http://127.0.0.1:6660/api/studio/start` with a brief
  and `mission_setup`.
- Smoke-test an output: `bash scripts/studio_smoke.sh <slug>`.
