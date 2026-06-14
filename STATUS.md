# SkyN3t — Status

SkyN3t is a solo-built multi-agent code-generation orchestrator. It turns a short
build request into a working app by running **brief -> plan -> generate -> verify
-> ship** through tier-aware model routing, shared memory/RAG, learned skills,
and approval-gated autonomous improvement loops.

## Current state (2026-06-13 evening)

- **Focus:** harden the core Studio build loop. Do not treat live launch quality
  as restored until a measured cohort proves ReviewerAgent `go` / score >=85.
- **OpenRouter routing:** `.env` currently has `SKYN3T_FREE_ONLY=0`, so paid
  OpenRouter tiers are allowed. `SKYN3T_NO_CLAUDE=1` disables Claude CLI/API and
  rejects Claude/Anthropic OpenRouter picks by rewriting them to non-Claude
  OpenRouter tiers. Setting `SKYN3T_FREE_ONLY=1` forces all OpenRouter tiers to
  real `:free` catalog models.
- **Current model policy:** per-tier OpenRouter choices are manually locked in
  `data/model_tier_overrides.json`: `deepseek/deepseek-v4-flash` for cheap/UI/
  backend and `deepseek/deepseek-v4-pro` for strong; docs stay on a free model.
  Catalog evolution still prefers current-generation/newer and cheaper near-tier
  models, but does not overwrite manual locks.
- **OpenRouter ops:** `SKYN3T_OPENROUTER_MAX_CONCURRENCY` defaults to 4, clamps
  to >=1, and is exposed in `/api/llm/backends`. Health checks now accept
  `python3` as satisfying the Python CLI requirement and report the resolved
  command/path.
- **Paths:** start the server from the repo so `.env` and relative paths resolve
  correctly. Runtime data/logs are repo-local (`data/`, `logs/`) unless env
  overrides them; generated projects are configured to the sibling
  `Skyn3t/Projects` directory outside the repo.
- **Skills:** installed skills live under the configured `DATA_DIR/skills` (the
  local tree currently has about 25 skill markdown files). Skills Hub roots
  resolve repo-relative (`examples/skills_seed`, `skills`, or
  `SKYN3T_SKILLS_HUB_PATHS`) independent of cwd. CLI/API install now handles a
  local or git multi-skill repo containing many `SKILL.md` files or loose skill
  frontmatter markdown. Relevant skills are surfaced to build planning as
  non-binding advice.
- **Learnings playbook:** the active LLM learning corpus is configured with
  `SKYN3T_LEARNINGS_DIR=/Volumes/Projects/skynetllm`, containing
  `playbook.json` and `playbook.md` (`smb://ugnas/Projects/skynetllm/`).
  It stores curated model winners, build-pattern shapes, and skill guidance
  that should be retrieved into Studio prompts through the unified LLM path.
- **GitHub memory:** `GitHubIngestorAgent` can be registered as
  `github_ingestor`, submitted through `POST /api/github/ingest`, or invoked via
  `skyn3t github ingest`. The orchestrator wires shared RAG into GitHub ingestor
  and explorer aliases; ingest results report missing client/seeds, rate limits,
  skip counts, and whether RAG storage was available.
- **Safe GitHub learning:** external repos are learning sources, not mutation
  targets. `skyn3t/intelligence/domain_corpus.py` includes
  `assess_github_learning_source()` so public/approved status, license review,
  redaction, read-only originals, and local candidate-copy workflow are explicit
  before a repo is promoted as a pattern source.
- **Build loop:** objective build/boot/integration verifier records now feed a
  reviewer re-score. Self-heal/fix loops are driven by objective verifier
  failures instead of the earlier reviewer verdict. CodeAgent also repairs
  entrypoint named/default import-export mismatches before verification.

## Where things live

- `skyn3t/studio/runner.py` — build pipeline, objective verification, fix loops,
  reviewer re-score.
- `skyn3t/agents/code_agent.py` — code generation and entrypoint repair gates.
- `skyn3t/core/model_router.py`, `skyn3t/core/model_evolution.py` — OpenRouter
  tier policy and current/newer/cheaper catalog scoring.
- `skyn3t/intelligence/skill_library.py`, `skyn3t/intelligence/skills_hub.py` —
  installed skills, hub roots, repo import.
- `skyn3t/intelligence/learnings_store.py` — compiled learnings playbook; point
  `SKYN3T_LEARNINGS_DIR` at `/Volumes/Projects/skynetllm` for the NAS corpus.
- `skyn3t/agents/github_ingestor.py`, `skyn3t/core/orchestrator.py` — GitHub RAG
  ingest and shared-memory wiring.

## Archived plans

Old planning and audit docs live in [`docs/archive/`](docs/archive/). They are
historical only; this file is the living operator status.
