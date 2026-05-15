# Swarm audit — product improvement ideas (2026-05-14)

Five subagents reviewed Skyn3t in parallel from different angles:
UX/product, agent intelligence, reliability/cost, strategy/moat, and
DX/testability. This doc captures the raw findings plus a synthesis
of where they converged.

Branch at audit time: `skyn3t/auto/ui-rebuild`. Recent commits the
agents were primed with: reviewer artifact-cap bump (15f88af), code
stage skips critique on revision (624569e), per-agent token tracking
(4b81406), reviewer reclassified as heavy stage (16c6f5a).

---

## Convergent themes

The strongest signal is where multiple angles flagged the same thing
independently.

### A. "We measure but don't act"
- Cost #4: `TokenTracker` accumulates but never enforces — runaway
  critique loops are uncapped in $.
- Intelligence #8: per-agent token tracker is a metric, not a control
  input. Same backend keeps burning tokens with no auto-backoff.
- Intelligence #4: critique loop has no eval — revisions can silently
  make artifacts worse.
- DX #1: 50+ test files, zero quality regression — no signal whether
  prompt changes help or hurt.

Highest-leverage fix in the whole audit. Build the closed-loop
control: token budget → backoff, structured rubric →
revision-rejection, eval harness → prompt-gating.

### B. The README's promise is broken on first run
- UX #1: README says "describe what you want in the brief box" — there
  is no brief box on the landing page (`OverviewPage.tsx` is just
  stat tiles).
- DX #4: README links to deleted `PORTABILITY.md`; two competing setup
  scripts (`setup.sh` vs `setup-new-machine.sh`); top-level
  `Dockerfile` was deleted but `docker-compose.yml` still references
  it.

Cheap, brand-defining fix. The first 90 seconds of the product are
broken right now.

### C. Reflection / self-tuning is theater
- Intelligence #1: `AutoTuner.apply_adjustments` is never called from
  outside its own module. `LessonsLearnedKB` is queryable but no agent
  queries it. `share_learning` writes static platitudes.
- Intelligence #5: `learning_loop._summarize` writes lines like
  `[architect] succeeded at architecture: auto:architect` — the
  resulting "lesson" has no content for a model to learn from.
- Intelligence #6: RAG in `code_agent` only retrieves *failed*
  experiences (filtered by `success: False`); successes are excluded.
- Counter-point — Strategy #2: `LessonScoreboard` +
  `BuildPatternScoreboard` *are* a real moat IF the inputs aren't
  garbage.

The infrastructure is the moat; the data flowing through it is junk.
Fix the data pipeline before claiming "collective brain."

### D. Pipeline waste
- Cost #3: serial stages that could fan out (`business_analyst`,
  `marketer`, `designer`, `architect` all read the brief and have no
  real data dependency on each other).
- Cost #7: reviewer re-reads the whole scaffold every round; nothing
  is diffed across rounds.
- Cost #2: no prompt caching on the CLI hot path; cache breakpoint
  heuristic in `_user_prefix_is_cacheable` rarely matches real
  codegen prompts → ~100% miss rate on Anthropic API.
- UX #4: long runs give zero progress signal — users think it hung.

One coherent project: parallelize independent stages, cache stable
prefixes, expose live per-stage tokens/elapsed in the UI. Ship
together.

---

## Strategic positioning

Strategy agent's honest read: don't pitch "persistent collective
brain" — every framework has SQLite + RAG. The real defensible bets:

1. **Cortex proposal/review UX** — governed self-modification with a
   PR-shaped UI. `cortex/proposals.py`, `feature_suggester.py`,
   `gated_tuner.py`, `code_improver.py` already exist. Nobody else
   ships a "review-and-merge" surface for the agent's suggestions
   about itself. AutoGen/CrewAI/LangGraph treat the framework as code
   you run; Devin/Cursor do code-PRs but not framework-PRs.

2. **Outcome-attributed memory** — `LessonScoreboard` +
   `BuildPatternScoreboard` + `SkillLibrary` already form a closed
   loop (record_injection → record_outcome → filter at retrieval).
   Most frameworks dump everything to a vector store and trust
   similarity, so their memory monotonically rots. Currently broken
   per Intelligence #1, #5, #6 — fix the data and it becomes the
   moat.

3. **CLI heterogeneity** — `ClaudeCLIAgent`, `KimiCLIAgent`,
   `CopilotCLIAgent` plus `agent_selector.py` (per-capability EMA,
   p95 latency, A/B groups, cost-per-task). As Claude Code, Copilot
   CLI, Kimi CLI, Gemini CLI proliferate, "the CLI is the API" is a
   real bet that nobody else is structurally making.

Risks to each: see "Strategy agent — full output" below.

---

## Top-7 ranked roadmap

| # | Item | Source | Effort | Impact |
|---|------|--------|--------|--------|
| 1 | Add brief box to `OverviewPage`; render artifacts as openable links; surface `awaiting_clarification` questions in `ProjectDetailView` | UX #1, #2, #3 | S | Huge first-run lift |
| 2 | Per-project cost ceiling + per-backend token-rate auto-backoff; structured reviewer rubric (4×25); reject revisions that score lower | Cost #4, Intel #3, #4, #8 | M | Stops runaway $; gives revisions a gradient |
| 3 | Eval harness + LLM cassettes (`tests/eval/`, `LLMCassette` backend keyed by `(caller_name, prompt_hash)`) | DX #1, #2 | M | Unblocks all prompt iteration |
| 4 | Prompt caching on CLI hot path (claude `--session-id`, broaden cacheable prefix) + parallel stage groups + diff-only reviewer rounds | Cost #2, #3, #7 | M | 50–70% input-token savings; 30–50% wall-time |
| 5 | Live progress in Studio (elapsed timer, per-stage tokens, current substep) + clarification UI | UX #4, #2 | S | Trust + completion-rate |
| 6 | Fix the lesson data pipeline: failure hints + reviewer deltas in lessons; dual-query RAG (success + failure); skill bodies contain real code, not filenames | Intel #2, #5, #6 | M | Makes the moat-bet real |
| 7 | Setup hygiene — fix README link, single `setup.sh`, `make doctor`; per-call sandbox subdir + cleanup | DX #4, Cost #1 | S | First-machine friction + silent disk leak |

Items 1–4 form a single coherent sprint and would transform both the
perception and the substance of the product.

---

## Backlog

Worth doing, lower priority — captured here so they don't get lost.

- Streaming chat with stable `session_id` (UX #5)
- REPL: stop clearing scrollback (`repl.py:1356`) — switch to
  `rich.live.Live` (UX #7)
- Activity stream search/expand/copy (UX #8)
- `AgentSpec` decorator + `skyn3t agent scaffold <name>` to kill the
  copy-paste-six-files ritual (DX #3)
- Single typed `AgentConfig` (pydantic) with documented precedence
  (env > overrides.json > catalog default) (DX #6)
- `skyn3t dev exec` cheap-tier mode + `skyn3t replay <task_id>`
  (DX #5, #7)
- `EventBus` async drain to unblock publisher path (Cost #5)
- Docker subagent: `create_subprocess_exec` + warm-pool image
  (Cost #8)
- Migrate dead `TaskDecomposer` to drive `code_agent`'s per-file
  pipeline — or delete it (Intel #7)
- Kill `AutoTuner` if not rewired into the cortex proposal flow
  (Intel #1)
- Make `prompt_chars`/`response_chars` mandatory in `LLM_EXCHANGE`
  event schema; add `cost_per_1k` table per backend/model (Cost #6)
- Migrate legacy bots (`slack_bot.py`, `discord_bot.py`,
  `email_agent.py`) to `MessagingChannel` contract (DX #8)

---

## Raw output by angle

### UX agent — full output

Confirmed: README promises "describe what you want in the dashboard
brief box" on first run, but `OverviewPage` ships only stat tiles.
There is no brief box on the landing page; users have to discover
Studio in the sidebar.

1. **README's promised brief box doesn't exist on Overview.** The
   Overview page (`skyn3t/web/ui/src/routes/OverviewPage.tsx`) is
   three stat tiles. New users land, see numbers, and have no obvious
   next move — the entire "wow" path is buried one click away in
   `/studio`. Fix: add a quick-start brief textarea + the curated
   example cards (already returned by the backend at
   `/api/studio/quickstarts`, see `web/app.py:2050-2111`) directly on
   `OverviewPage.tsx`. Submit hits `/api/studio/start`. ~80 LOC.
2. **Studio detail view silently ignores `awaiting_clarification`.**
   `runner.py:798` pauses projects with
   `status="awaiting_clarification"` and stores
   `manifest["clarification"].questions`. Backend exposes
   `POST /api/studio/projects/{slug}/clarify`. But
   `ProjectDetailView` in `StudioPage.tsx` never reads
   `clarification` — yellow "blocked" pill, no questions, no input
   box, dead end. Fix: render question list + answer inputs +
   "Resume" button calling `api.studio.clarify(slug, answers)`.
3. **Generated artifacts are inert text, not openable/downloadable.**
   `StudioPage.tsx:327-338` renders artifacts as plain `<li>` strings,
   even though the backend exposes `/api/studio/projects/{slug}/file`,
   `/preview/{path}`, and `/zip` (`web/app.py:2225-2265`). After a
   5-minute build the user sees a path and can't click it.
4. **Long agent runs give zero progress signal in Studio.** Today the
   only feedback is a `StatusPill` plus polling every 4s. No elapsed
   timer, no current substep, no token-burn next to the running
   stage. Fix: in `StageRow` (`StudioPage.tsx:364`), when
   `status === "running"` show elapsed-since-`started_at`, the
   heartbeat from the most recent matching event, and per-stage token
   count.
5. **Chat is single-shot and forgets everything.** `ChatPage.tsx`
   posts each message to `/api/agents/{name}/exec` with no
   thread/session id. No conversation memory between turns even
   though the pitch is "persistent collective brain." Won't stream.
   Fix: pass a stable `session_id` (already used in
   `repl.py:85`), add a streaming endpoint or WS subscription.
6. **Backend-down banner tells users to run a shell script that may
   not exist.** `App.tsx:146` instructs `bash
   scripts/restart-backend.sh` — useless for packaged install or
   Docker. No "I started it, check now" affordance. Fix: add
   "Retry now" button; make restart hint conditional on dev origin.
7. **CLI REPL clears the whole screen on every prompt.**
   `repl.py:1356` calls `console.clear()` before each
   `_paint_snapshot()`. Destroys scrollback. Fix: replace with
   `rich.live.Live` running below the input, or `prompt_toolkit`'s
   `Application` with a bottom toolbar.
8. **Activity stream has no search and no copy-anything.**
   `ActivityPage.tsx` filters by `kind` chips but every event row
   truncates `label` with `title` tooltip-only. Fix: expandable rows,
   free-text filter, copy-as-JSON.

### Reliability/cost agent — full output

1. **CLI sandbox cwd grows unbounded — every reviewer/verifier
   rescans stale artifacts.** `adapters/llm_client.py:398-411,
   425-451` mints one process-wide `tempfile.mkdtemp` and never
   empties it. `_collect_sandbox_artifacts` then `rglob("*")` over
   the entire tree on every CLI call. After hundreds of calls this is
   O(N) IO per call plus disk leak. Fix: per-call subdir (`mkdtemp(...,
   dir=root)`), harvest, then `shutil.rmtree`.
2. **No prompt caching for the CLI hot path.** `_AnthropicBackend`
   handles `cache_control` (line 690) but every CLI backend
   re-passes the prompt verbatim. Reviewer/critique re-runs
   re-send the same brief + scaffold listing. The
   `_user_prefix_is_cacheable` heuristic requires a "# Recent
   successful diffs" prefix, so cache miss rate is ~100% for real
   codegen prompts. Fix: (a) for `claude_cli` use `--session-id` to
   reuse Claude Code's KV cache across stage calls in one project;
   (b) broaden the heuristic to mark any prompt ≥1024 chars whose
   first ~2KB is stable across a slug. ~50–70% input-token savings.
3. **Pipeline stages run strictly serial, including independent
   ones.** `runner.py:574 for stage in stages:` — every stage
   `await`s the previous one. `business_analyst`, `marketer`,
   `designer`, `architect` often have no real data dependency on each
   other (all read the brief). Fix: add `parallel_group` to
   `StageSpec`; in `_run_pipeline` use `asyncio.gather` for stages
   sharing a group. 30–50% wall-time win on the planning half.
4. **No cost ceiling / circuit breaker per project.**
   `token_tracker.py` accumulates but never enforces.
   `_critique_and_revise` does multi-round; on Anthropic API a stuck
   loop on a 30-file scaffold can burn 500K+ input tokens silently.
   `_fallback` retries without a budget — flapping CLIs can re-shell
   hundreds of times. Fix:
   `TokenTracker.check_project_budget(slug)`; consult before each
   critique round and at `LLMClient.complete` entry. Wire
   `FallbackManager` (already imported in `orchestrator.py:14`) to
   LLM backends.
5. **EventBus callbacks block the publisher; LLM_EXCHANGE fan-out
   scales linearly.** `events.py:131-151` — `publish` calls
   subscribers synchronously in the publisher's thread.
   `TokenTracker._on_exchange` does dict mutation under `RLock` per
   event. On a hot stage that's 50–100 publishes/sec gating real
   work. Fix: push callbacks onto an `asyncio.Queue` drained by a
   single worker, or batch-flush LLM_EXCHANGE every N ms.
6. **Token tracker uses the truncated-to-2000-char preview as
   ground-truth fallback.** `token_tracker.py:76-86` — when
   `prompt_chars` not present, falls back to redacted preview, then
   bumps with constants. Third-party publishers that don't supply
   `prompt_chars` undercount by 5–50x. No per-backend cost
   coefficient. Fix: make `prompt_chars`/`response_chars` mandatory;
   add `cost_per_1k` table keyed by backend/model.
7. **Reviewer reads the whole scaffold every round — nothing diffed
   across rounds.** `runner.py:2210` loop calls
   `reviewer.critique(...)` each round, which stuffs all
   `produced_files` into a 2500-token prompt. Round 2 re-sends the
   same files. Fix: pass `previously_reviewed_files` set; send full
   content only for files modified since last round, one-line summary
   for the rest. 60–80% input-token cut on multi-round runs.
8. **Docker subagent spawns are blocking and re-pull contract per
   call; no warm pool.** `docker_backend.py:160-187` — `Popen` +
   `proc.communicate(timeout=...)` via `loop.run_in_executor(None,
   ...)`. Many concurrent subagents will exhaust the default
   executor and silently queue. Every spawn is a fresh
   `python:3.11-slim` boot (~1.5s cold). Fix: use
   `asyncio.create_subprocess_exec`; for high-throughput, add a
   `--detach` keep-alive container or a baked `skyn3t-subagent`
   image with deps pre-installed.

Honorable mentions: no structured per-stage logging of token deltas;
`MessageBus.request` (`messaging.py:158`) has no max-pending-futures
cap; `EventBus._max_history=1000` deque silently drops old events.

### Intelligence agent — full output

1. **Reflection engine is cargo-cult — kill or rewire AutoTuner +
   LessonsKB.** `intelligence/reflection.py` builds an entire
   `AutoTuner` + `LessonsLearnedKB` + `PromptSuggestionEngine`, but
   `AutoTuner.apply_adjustments` is never called outside the module.
   `LessonsLearnedKB.add` records "Detected patterns: timeout"
   lessons that no agent queries. `share_learning` calls in every
   agent emit static strings that go to the UI and die there. Fix:
   delete AutoTuner; route `Lesson` entries into the same RAG store
   the `LearningLoop` reads from; have `share_learning` write
   outcome-tagged observations (success + reviewer score + verifier
   verdict).
2. **Skill library reads the wrong outputs (winner shapes, not winner
   content).** `memory/meta_agent.py:431` writes
   `{stack}-winning-shape` skills containing only a bullet list of
   filenames — no code, no rationale. `code_agent.py:898-921` then
   injects up to 8 of those into the prompt at 3500 chars each. The
   model gets "include `tests/test_health.py`" with zero example.
   Fix: on a successful build, snapshot the actual file bodies of
   distinguishing files into the skill body.
3. **Reviewer score (0-100) is unanchored — not a learning signal.**
   `agents/reviewer.py:_llm_review` asks "what's the most generous
   fair score" with no rubric, no calibration. The chain-of-thought
   preamble (line 437) explicitly invites grade inflation. Fix:
   structured rubric (completeness / runs / matches-brief /
   internally-consistent, each 0-25). Force JSON contract.
4. **Critique loop has no eval — revisions can silently make things
   worse.** `runner.py:_critique_and_revise` (line 2210) runs
   reviewer → targeted_fix → re-check, but nothing scores the
   post-fix artifact against the pre-fix one. Fix: re-run the
   structured rubric on changed files; reject revisions that drop
   score.
5. **`share_learning` and `learning_loop._summarize` produce useless
   lesson text.** `learning_loop.py:169-176` writes
   `[architect] succeeded at architecture: auto:architect` as the
   lesson body. Lessons carry no error tail, no diff. Fix:
   `_summarize` should pull `output.failure_hint`, reviewer rubric
   deltas, scaffold shape changes.
6. **RAG recall in `code_agent` only retrieves *failed*
   experiences.** `code_agent.py:945-986` queries with
   `filter_dict={"doc_type": "experience", "success": False}`.
   Successful builds for the same brief shape are never retrieved as
   positive examples. Fix: dual query — top-3 successes + top-3
   failures, present as "this worked / this failed" pairs.
7. **`TaskDecomposer` / `ResultAggregator` are dead code.**
   Referenced from `orchestrator.py:108-110`, but studio `runner.py`
   never calls `auto_decompose` — every studio project goes through
   linear `_run_pipeline`. Meanwhile code stage builds its own ad-hoc
   parallel job pool with no dependency tracking. Fix: either delete
   or actually drive `code_agent`'s per-file LLM jobs through
   `DependencyGraph` + `ResultAggregator` — would let cross-file
   imports be validated before sibling files are generated.
8. **Agent fallback chain ignores per-agent token tracker.** Recent
   commit adds per-agent token tracking, but `LLMClient._get_impl`
   still picks backends purely by env/skip-list. Fix: feed
   `token_tracker` into a per-agent backoff — when an agent's
   tokens-per-success ratio crosses a threshold on backend X,
   auto-skip it for the next N calls.

### Strategy agent — full output

After auditing the codebase, the honest read: most headline
differentiators ("collective brain", "self-healing", "shared
blackboard", "memory") are table stakes — every framework has
equivalents. The real, weirder bets buried in the code:

1. **Cortex proposal/review UX.** `cortex/proposals.py` +
   `feature_suggester.py` + `gated_tuner.py` + `handlers.py` +
   `auto_cleanup.py` together form an actual workflow where the
   system observes itself, files structured proposals (tuning,
   code_patch, feature, ingest), and humans approve/reject through a
   dashboard. Code patches go through `code_improver` with snapshots
   and rollback. **Governed self-modification**, not just "self-tune
   a config knob." AutoGen/CrewAI/LangGraph don't ship the
   review-and-merge UI for the agent's suggestions about itself.
   Devin/Cursor do code-PRs but not framework-PRs. Next bets:
   signed/auditable proposal log, per-proposal cost+risk score,
   GitHub-style discussion threads on proposals, A/B rollout of
   approved tunings. Risk: if proposals are noisy or shallow, users
   disable cortex and it becomes dead weight.

2. **Outcome-attributed memory.** `lesson_attribution.py` actually
   credits/debits each injected lesson against task outcomes;
   `build_patterns.py` records (stack, file-shape) → success-rate;
   `skill_library.py` writes durable, scored markdown skills. Most
   frameworks dump everything to a vector store and trust similarity
   — they have no causal link, so memory monotonically rots. Skyn3t
   has the closed loop. Next bets: "skill provenance" UI; curated
   public skill packs per stack (network effect); fork/share skill
   libraries; per-skill counterfactual evaluation. Risk: attribution
   is noisy at low N — need confidence intervals.

3. **Heterogeneous CLI orchestration with per-task model arbitrage.**
   `ClaudeCLIAgent`, `KimiCLIAgent`, `CopilotCLIAgent` as actual
   subprocesses; `agent_selector.py` tracks per-capability EMA, p95
   latency, error patterns, A/B groups, cost-per-task. The
   streaming-idle `_run_capture` (180s idle / 1200s hard) is built
   for real CLI reliability. AutoGen/Swarm/LangGraph are
   LLM-API-first; Cursor/Devin are single-vendor; Aider is
   single-process. Next bets: cost+latency dashboard per
   (model, capability); user-editable routing policies; auto-fallback
   chains with circuit breakers; nightly benchmark suite that runs
   the same task across configured CLIs and updates routing. Risk:
   CLIs change incompatibly (auth, output format) and break adapters;
   Claude Code itself is starting to look like an orchestrator.

4. **Deterministic stack-templated build verification as outer-loop
   training signal.** `build_verifier.py` + `stack_templates.py` +
   `BuildPatternScoreboard` + `runner.py` form a pipeline where the
   swarm generates a project, builds it, and learns from the build
   verdict — not just "did the LLM say it looks good." That
   ground-truth signal powers everything in #2. Most frameworks have
   no concept of "did the artifact actually work"; they end at
   `TaskResult.success=True` from the LLM. Risk: stack templates are
   a maintenance treadmill.

5. **Sandboxed heterogeneous execution backends.**
   `subagent_runner.py` + `docker_backend.py` define a one-JSON-line
   contract so any subagent can be reassigned to local subprocess or
   container without rewriting it. Plus the macOS seatbelt sandbox.
   AutoGen/CrewAI assume in-process Python; LangGraph leans on
   hosted LangSmith. Next bets: ship SSH/Modal/Singularity backends;
   per-agent resource quotas; team mode with worker-pool machines;
   security-tiering. Risk: hosted competitors own the "we manage the
   sandbox" pitch.

Top-3 bets: #1, #2, #3. Treat #4 and #5 as enablers rather than
top-line pitches.

### DX/testability agent — full output

1. **No eval / regression harness for swarm quality.** `tests/` has
   50+ files but every "agent" test mocks the LLM and asserts
   plumbing — `test_pipelines.py`, `test_studio_planner.py`. Zero
   golden-output regression. The only quality signal is "did the
   reviewer say go" — itself an LLM call you don't grade. Fix: add
   `tests/eval/` with ~20 frozen briefs, run real pipeline against
   fixed seeds + LLM cassettes, score with a rubric (length,
   required sections, codegen smoke-build). Gate prompt changes on
   delta vs baseline.
2. **No record/replay for LLM calls.** Tests monkeypatch
   `LLMClient._get_impl` per test with bespoke `FakeLLMBackend`
   classes. No shared cassette format, no way to replay a real
   production trace. Fix: ship an `LLMCassette` backend keyed by
   `(caller_name, prompt_hash)` reading JSON from `tests/cassettes/`.
   Add `SKYN3T_LLM_RECORD=1` mode that writes them.
3. **Adding an agent is a copy-paste-edit-six-files ritual.** New
   agent = subclass under `skyn3t/agents/`, export in
   `agents/__init__.py`, hand-add to `_CATALOG` in
   `registry/catalog.py`, hand-add to `DEFAULT_ROSTER` in
   `registry/defaults.py`, optionally add row to
   `data/agent_overrides.json`, add stage entry to
   `core/model_router.py`. Every existing agent re-implements the
   LLM-call-with-fallback pattern despite `BaseAgent.llm_complete`
   existing. Fix: `AgentSpec` dataclass + `@register_agent`
   decorator; `skyn3t agent scaffold <name>` command.
4. **Fresh-machine setup is undocumented and contradictory.**
   `README.md:9` links to a deleted `PORTABILITY.md`. Two competing
   scripts: `scripts/setup.sh` (pins `python3.11`, doesn't probe
   CLIs) and `scripts/setup-new-machine.sh` (accepts 3.10+, probes
   `claude`/`kimi`/`copilot`, generates `SECRET_KEY`). README points
   at the weaker one. Top-level `Dockerfile` was deleted but
   `docker-compose.yml` still references it. Fix: delete
   `setup.sh`, rename `setup-new-machine.sh` → `setup.sh`, fix
   README, drop dangling Docker references, add `make doctor`.
5. **Per-run trace exists but you can't replay an agent decision.**
   `observability/tracing.py` builds spans in memory, but spans
   don't capture LLM prompt+response, model used, token cost, or
   stage inputs/outputs together. Fix: persist
   `runs/<slug>/<task_id>/trace.json` with spans + LLM exchanges +
   final artifact paths. Add `skyn3t replay <task_id>`.
6. **Config sprawl — five places define an agent's behavior.**
   `data/agent_overrides.json`, `data/custom_agents.json`,
   `core/model_router.py` policy table, env-var
   `SKYN3T_MODEL_ROUTING`, code defaults in each agent's `__init__`.
   `apply_override` in `core/agent.py` is 250+ defensive lines
   because the schema is implicit. The `.bak-pre-router` file shows
   manual migration, not migrations. Fix: single typed `AgentConfig`
   (pydantic) with documented precedence (env > overrides.json >
   catalog default). Versioned config file with schema migration.
7. **No fast/cheap dev mode.** The `[deterministic-stub]` string
   appears in 20+ files as the only "no LLM" signal. No
   `SKYN3T_DEV=1` that pins every agent to a haiku-class model with
   low max_tokens; no `--dry-run` that prints the prompt the agent
   *would* send. Fix: `skyn3t dev exec <agent> <brief>` using
   cheap-tier across the board, prints prompts, records a cassette.
8. **Integration tests are brittle / channel sprawl.**
   `integrations/` ships 8 transports but `messaging.py`'s docstring
   promises "one class per platform" while
   `slack_bot.py`/`discord_bot.py`/`email_agent.py` predate it and
   don't conform. `test_messaging_signal_imessage_teams.py` exists
   for platforms with no production wiring. Fix: migrate legacy bots
   to `MessagingChannel`, delete speculative tests, add one contract
   test class run against every registered channel.
