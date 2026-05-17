# Plan — Self-improving / self-healing loop

Three phases. Each lands independently and is testable on its own.
Phase 1 unblocks 2 & 3 (without it, the cortex never runs in
production, so anything 2 & 3 record is wasted).

## Context

The codebase has a lot of self-improvement scaffolding already:
- `MemoryStore`, `ExperienceIngestor`, `RAGEngine` — capture outcomes
- `SelfTuningEngine`, `GatedTuner` — translate outcomes into config proposals
- `BuildPatternScoreboard` — track per-(stack, shape) win/loss
- `CuriosityLoop`, `FeatureSuggester`, `ReviewWatcher`, `AutoCleanup` — autonomous cortex
- `SelfHealingManager` — restart broken agents

But three wiring gaps keep it from being an actual learning loop:
1. The cortex's `start()` method (`CortexBootstrap`) was never built —
   the components are imported lazily, fail silently, never run.
2. Failure experiences land in RAG as opaque prose; recall returns
   them but can't rank "this fix worked, that one didn't."
3. `BuildPatternScoreboard` records regret but `resolve_model_for_file`
   is pure static — the router never reads what the scoreboard learned.

```
                            ┌─────────────────────────┐
                            │       PHASE 1           │
                            │   CortexBootstrap       │
                            │  (5 components start)   │
                            └────────────┬────────────┘
                                         │ enables
                ┌────────────────────────┼────────────────────────┐
                │                                                 │
   ┌────────────▼─────────────┐                    ┌──────────────▼──────────────┐
   │       PHASE 2            │                    │         PHASE 3             │
   │  Structured experience   │                    │  Backend scoreboard ─→     │
   │  schema + ranked recall  │                    │  routing feedback           │
   │  (fixes become data)     │                    │  (router learns)            │
   └──────────────────────────┘                    └─────────────────────────────┘
```

Each phase has an env-var kill switch so we can disable the new
behavior without redeploying.

---

## Phase 1 — `CortexBootstrap` (the foundation)

### Why

`orchestrator._start_cortex` used to call `from skyn3t.cortex.bootstrap
import CortexBootstrap`, but the module didn't exist; the lazy import
was swallowed and a warning was logged. Production therefore never
actually started:
- `GatedTuner` — turns tuner suggestions into review-gated proposals
- `FeatureSuggester` — proposes new features from event patterns
- `CuriosityLoop` — looks for capability gaps
- `ReviewWatcher` — turns reviewer no-gos into auto-fix proposals
- `AutoCleanup` — janitors stale projects/branches/proposals

The `orchestrator.get_cortex_status()` placeholder currently returns
`warnings: ["Cortex bootstrap has not been initialized."]` for every
call. Five components on the floor.

### Approach

Create `skyn3t/cortex/bootstrap.py` with a `CortexBootstrap` class.
Each component's lifecycle is mixed (some sync `start`, some async,
some have no `stop`), so the bootstrap normalizes them through one
small dispatcher.

```python
# skyn3t/cortex/bootstrap.py  (new file, ~180 LOC)

@dataclass
class _Component:
    name: str                       # short id ("gated_tuner", "auto_cleanup", …)
    instance: Any                   # the object
    creates: tuple[str, ...]        # proposal kinds it produces
    handles: tuple[str, ...]        # proposal kinds it consumes
    subscriptions: tuple[str, ...]  # "EVENT_TYPE:kind" pairs
    started: bool = False
    error: str | None = None

class CortexBootstrap:
    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator
        self.event_bus = orchestrator.event_bus
        self._components: list[_Component] = []
        self._wired = False

    async def start(self) -> None:
        if self._wired: return
        self._register_default_components()
        for c in self._components:
            try:
                await self._call_lifecycle(c.instance.start)
                c.started = True
            except Exception as e:
                c.error = str(e); logger.exception("cortex start failed: %s", c.name)
        # Side effect of bootstrap: install proposal handlers
        try:
            from skyn3t.cortex.handlers import install_handlers
            install_handlers(self.orchestrator)
        except Exception:
            logger.exception("install_handlers failed")
        self._wired = True

    async def stop(self) -> None:
        for c in reversed(self._components):
            stop = getattr(c.instance, "stop", None)
            if stop is None: continue
            try: await self._call_lifecycle(stop)
            except Exception: logger.exception("cortex stop failed: %s", c.name)
        self._wired = False

    def status(self) -> dict:
        store = self._proposal_store()
        return {
            "running": True, "booted": self._wired,
            "components": [self._component_status(c) for c in self._components],
            "proposal_handlers": store.registered_handlers() if store else [],
            "proposal_counts": store.counts() if store else {},
            "recent_failures": store.recent_failures(limit=5) if store else [],
            "warnings": [],
        }

    async def _call_lifecycle(self, fn) -> None:
        result = fn()
        if inspect.iscoroutine(result): await result

    def _register_default_components(self) -> None:
        # Honor SKYN3T_CORTEX_DISABLE="auto_cleanup,curiosity" env var.
        disabled = set(filter(None, os.environ.get("SKYN3T_CORTEX_DISABLE", "").split(",")))
        candidates = [
            ("gated_tuner",       GatedTuner(self.event_bus),                       ("tuning",),   ("tuning",),       ("SYSTEM_ALERT:tuning_suggestion",)),
            ("feature_suggester", FeatureSuggester(event_bus=self.event_bus),       ("feature",),  (),                ()),
            ("curiosity",         CuriosityLoop(orchestrator=self.orchestrator,
                                                event_bus=self.event_bus),          (),            (),                ()),
            ("review_watcher",    ReviewWatcher(event_bus=self.event_bus),          ("studio_debug",),("studio_debug",), ("PROJECT_REVIEW_ATTACHED",)),
            ("auto_cleanup",      AutoCleanup(event_bus=self.event_bus,
                                              projects_root=Path("data/projects"),
                                              proposals_root=Path("data/proposals"),
                                              repo_root=Path(".")),                  (),            (),                ()),
        ]
        for name, instance, creates, handles, subs in candidates:
            if name in disabled: continue
            self._components.append(_Component(name, instance, creates, handles, subs))
```

Then in `orchestrator._start_cortex`, re-add the block I deleted in
`922c71a` but pointing at the real class. Same `try/except` shape
so a broken cortex never takes down the orchestrator.

**Kill switch:** `SKYN3T_CORTEX_DISABLE=name1,name2` skips
individual components; `SKYN3T_CORTEX_DISABLE=*` skips everything.

### Critical files

| File | Action |
|---|---|
| `skyn3t/cortex/bootstrap.py` | **New** — the class above |
| `skyn3t/core/orchestrator.py` | Re-add `_start_cortex` block (lines ~330 originally) |
| `skyn3t/cortex/proposals.py` | Add `counts()`, `recent_failures(limit)`, `registered_handlers()` if missing (the status dict needs them) |
| `tests/test_cortex_bootstrap.py` | **New** — verify start/stop are idempotent, each component reports `started: bool`, env-var disables work, errors in one component don't abort others |

### Verification

- Static smoke test (already shipped in `tests/test_imports_smoke.py`)
  becomes green — `from skyn3t.cortex.bootstrap import CortexBootstrap`
  resolves.
- `await orch.start(); await asyncio.sleep(0); status = orch.get_cortex_status()`
  returns `booted: True`, `components` list contains all 5 with
  `started: True, error: None`.
- Existing `tests/test_web_app.py::test_cortex_status_reports_handlers_and_components`
  keeps passing (it mocks the orchestrator so isn't sensitive to the
  real class, but confirms shape compatibility).
- `SKYN3T_CORTEX_DISABLE=curiosity,auto_cleanup` reduces the
  `components` list to 3.

### Risks

- Adds 5 long-lived background coroutines/handlers to every
  orchestrator. Each currently logs its own exceptions, but the
  bootstrap should *also* publish a `SYSTEM_ALERT` event on
  component failure so it's visible without log-diving.
- `AutoCleanup` runs git operations against the cwd repo. The
  default `repo_root=Path(".")` is the cwd of the python process,
  which in `studio.runner` workers may be a project scaffold, not
  the SkyN3t repo. Use an explicit `SKYN3T_REPO_ROOT` env var or
  fall back to `Path(__file__).resolve().parents[2]`.

---

## Phase 2 — Structured experience schema + ranked recall

### Why

`CodeAgent._scaffold_from_brief` already calls RAG to recall past
experiences (`code_agent.py:868-915`), but `ExperienceIngestor.ingest_task_experience`
stores them as prose with a flat metadata dict:

```python
metadata = {
    "task_id": task_id, "agent_name": agent_name,
    "success": success, "execution_time_ms": ..., "error": error,
}
```

There's no structured `stack`, `stage`, `error_signature`, or
`fix_applied`. So when the agent recalls "we saw this before,"
it can't say *which fix worked* or *how often*. The recall is
essentially a similarity-ranked dump of free-text logs.

### Approach

Extend the metadata schema and add a thin `experience_index` table
in `MemoryStore` that mirrors the structured fields for SQL queries
(RAG's vector search is good for similarity; SQL is what you want
for "rank fixes for error_signature X by success rate").

**1. Metadata extension** (backward-compatible — new fields nullable):

```python
metadata = {
    # existing
    "task_id": task_id, "agent_name": agent_name,
    "success": success, "execution_time_ms": ..., "error": error,
    # new
    "stack": "react_vite" | "fastapi" | …,
    "stage": "code" | "boot_verifier" | "build_verifier" | …,
    "error_signature": "vite_dryrun:missing_mount" | "boot:port_in_use" | …,
    "fix_applied": "targeted_fix:add_root_div" | None,
    "fix_worked": True | False | None,
    "brief_shape": ["dashboard", "integrations", "config_store"],  # tags
}
```

**2. New `experience_index` table** in `memory/store.py` —
1-row-per-experience denormalization for fast SQL ranking:

```sql
CREATE TABLE experience_index (
    embedding_id TEXT PRIMARY KEY,
    task_id TEXT, stack TEXT, stage TEXT,
    error_signature TEXT, fix_applied TEXT,
    fix_worked BOOLEAN, success BOOLEAN,
    created_at REAL
);
CREATE INDEX idx_exp_sig ON experience_index(error_signature, fix_worked);
```

`ExperienceIngestor._persist_doc` writes both the RAG embedding and
this row.

**3. New `MemoryStore.rank_fixes_for_signature(sig: str, k: int = 5)`**:

```sql
SELECT fix_applied, COUNT(*) FILTER (WHERE fix_worked) AS wins,
       COUNT(*) AS attempts
FROM experience_index
WHERE error_signature = ? AND fix_applied IS NOT NULL
GROUP BY fix_applied
ORDER BY (CAST(wins AS REAL) / attempts) DESC, attempts DESC
LIMIT ?;
```

**4. Wiring**:
- `studio/runner.py` already knows the `stage` and computes
  `stack` — pass them into `ingestor.ingest_task_experience(...)`.
- `agents/targeted_fix.apply_targeted_fix` knows the `fix_applied`
  label. Currently it returns a result dict; add `fix_label` and
  thread it through the runner's ingest call. `fix_worked` is
  resolved later when the *next* verifier pass runs (success of
  the post-fix re-verify).
- `agents/code_agent.py` calls `MemoryStore.rank_fixes_for_signature`
  in addition to the RAG query, and prepends the top-3 ranked fixes
  to the build system prompt as "Known fixes for $signature, ranked
  by historical win rate."

**Kill switch:** `SKYN3T_EXPERIENCE_SCHEMA=v1` to opt in;
unset = legacy path (just the prose log).

### Critical files

| File | Action |
|---|---|
| `skyn3t/memory/ingestor.py` | Extend metadata, persist to new table |
| `skyn3t/memory/store.py` | New table + migration; new `rank_fixes_for_signature` method |
| `skyn3t/studio/runner.py` | Pass `stack`, `stage`, `error_signature`, `fix_label` when ingesting |
| `skyn3t/agents/targeted_fix.py` | Return `fix_label` in result dict |
| `skyn3t/agents/code_agent.py` | Read ranked fixes + inject into build prompt |
| `tests/test_experience_recall.py` | **New** — given 10 seeded experiences, top-ranked fix wins |

### Verification

- Seed 10 fake experiences: 7 with `(sig="vite:missing_mount", fix="add_root_div", worked=True)`, 3 with `(sig="vite:missing_mount", fix="rewrite_main", worked=False)`. `rank_fixes_for_signature("vite:missing_mount")` returns `[("add_root_div", 7, 7), ("rewrite_main", 0, 3)]`.
- End-to-end: trigger a deliberate `missing_mount` failure twice, verify second build's CodeAgent prompt contains the recalled fix.
- Schema migration test: legacy `experiences` rows without the new
  metadata don't break recall (the new SQL falls back to "no ranked
  fixes available, use RAG prose").

### Risks

- The "fix_worked" signal is lagging — known only after the next
  verifier pass. Need a clean way to update the row later without
  fighting the immutable RAG embedding. Solution: `experience_index`
  is mutable SQL; the RAG embedding stays append-only.
- `error_signature` extraction needs to be deterministic. Reuse
  the existing `BuildPatternScoreboard` tag conventions
  (`missing_mount`, etc.) so the two systems share vocabulary.

---

## Phase 3 — `BuildPatternScoreboard` → router feedback

### Why

`BuildPatternStats` already tracks `success`/`failure`/`tags` per
`(stack, shape)`. But `resolve_model_for_file` (the per-file model
router) is pure static lookup:

```python
if rl.endswith(("app.jsx", "app.tsx", "main.jsx", "main.tsx")):
    return _TIERS["ui"]
if any(h in rl for h in _FRONTEND_PATH_HINTS):
    return _TIERS["cheap"]   # kimi_cli
```

The router never sees the scoreboard. If `kimi_cli` keeps timing
out on `src/components/*.jsx` files for the `react_vite` stack,
the routing decision stays the same on every retry. We log regret;
we don't act on it.

### Approach

Extend `BuildPatternStats` with a `by_backend` sub-tally and
make `resolve_model_for_file` consult it for the *tier candidate*
before returning.

**1. Schema extension** (backward-compatible):

```python
@dataclass
class BuildPatternStats:
    stack: str
    shape: List[str] = field(default_factory=list)
    success: int = 0
    failure: int = 0
    skipped: int = 0
    tags: Dict[str, int] = field(default_factory=dict)
    # NEW: per-backend tally for the same shape.
    by_backend: Dict[str, Dict[str, int]] = field(default_factory=dict)
    last_seen_at: float = field(default_factory=time.time)

    def record_backend(self, backend: str, verdict: str) -> None:
        slot = self.by_backend.setdefault(backend, {"success": 0, "failure": 0, "skipped": 0})
        slot[verdict] = slot.get(verdict, 0) + 1

    def backend_success_rate(self, backend: str) -> Optional[float]:
        slot = self.by_backend.get(backend)
        if not slot: return None
        denom = slot["success"] + slot["failure"]
        return None if denom < _MIN_SAMPLES else slot["success"] / denom
```

**2. New router input — `resolve_model_for_file(rel_path, stack=None, scoreboard=None)`**:

```python
def resolve_model_for_file(rel_path, stage_name="code", *, stack=None, scoreboard=None):
    # ... existing static logic to pick (backend, model) ...
    backend, model = pick_static(...)
    if stack and scoreboard:
        rate = _backend_rate(scoreboard, stack, backend)
        if rate is not None and rate < _LOSING_RATE_THRESHOLD:
            # Demote: try the next-best alternative for this file type.
            alt = _alternative_for(backend, rel_path)
            if alt:
                logger.info("router demoting %s for %s (win rate %.2f); using %s",
                            backend, rel_path, rate, alt[0])
                return alt
    return backend, model
```

`_LOSING_RATE_THRESHOLD = 0.4` and `_MIN_SAMPLES = 5` so we don't
demote on a single bad run. Both are env-tunable
(`SKYN3T_ROUTER_DEMOTE_BELOW`, `SKYN3T_ROUTER_DEMOTE_AFTER`).

**3. Recording**: `studio/runner.py` already calls
`scoreboard.record(stack, shape, verdict)` at stage completion.
Add a paired `scoreboard.record_backend(stack, shape, backend, verdict)`
when the stage is `code` (so we attribute outcomes to the actual
backend used).

**4. CodeAgent plumbing**: `_scaffold_from_brief` already knows
`stack`. Pass `scoreboard=get_default_scoreboard()` into
`resolve_model_for_file(rel, stack=stack, scoreboard=…)` at lines
1297, 1529.

**Kill switch:** `SKYN3T_ROUTER_ADAPTIVE=0` reverts to pure static.

### Critical files

| File | Action |
|---|---|
| `skyn3t/intelligence/build_patterns.py` | Add `by_backend`, `record_backend`, `backend_success_rate` |
| `skyn3t/core/model_router.py` | Accept `stack`/`scoreboard` kwargs; demotion logic + alternative table |
| `skyn3t/studio/runner.py` | Call `record_backend` on code-stage verdict |
| `skyn3t/agents/code_agent.py` | Pass `stack`/`scoreboard` into router calls (lines 1297, 1529) |
| `tests/test_model_router_adaptive.py` | **New** — seed scoreboard with 6 failures on kimi_cli for react_vite jsx; router returns alt backend |
| `tests/test_build_patterns.py` | Extend — `by_backend` roundtrip, `backend_success_rate` with `<MIN_SAMPLES` returns None |

### Verification

- Pure-static test (default): `resolve_model_for_file("src/components/X.jsx")` → `_TIERS["cheap"]` (kimi_cli). Unchanged.
- Seeded scoreboard test: record 6 failures and 0 successes for
  `(stack="react_vite", backend="kimi_cli")`; same call now returns
  the alternative (`copilot_cli`).
- Below `_MIN_SAMPLES`: 3 failures → still returns kimi_cli (not
  enough data to demote).
- `SKYN3T_ROUTER_ADAPTIVE=0` short-circuits the whole adaptive
  block; static decisions only.

### Risks

- Adaptive routing has hysteresis: if we demote kimi_cli for jsx,
  it never recovers because it never gets attempted. Mitigate by
  giving the demoted backend a 1-in-N exploration attempt (epsilon-
  greedy). Default ε=0.1.
- Scoreboard is process-local in tests; the runner uses
  `get_default_scoreboard()` which reads from disk. Make sure
  the router's `scoreboard` arg accepts either and doesn't
  hold a stale reference across reloads.

---

## Order of operations

1. **Phase 1 first** — without it, Phases 2 & 3 do work that no
   running component will read.
2. **Phase 3 next** — smallest, most contained, immediate value
   (~80 LOC + tests). Validates the "feedback loop" pattern with
   the simplest possible signal (success/failure ratio).
3. **Phase 2 last** — biggest design surface (schema migration,
   cross-component data flow). Worth doing after the loop is proven
   to be safe with Phase 3.

Each phase is independently revertable via its env var, so we can
roll back without a code deploy if any of them misbehaves.

## Cross-cutting: observability

Add one new event type — `EventType.CORTEX_DECISION` — published
whenever:
- `CortexBootstrap` skips a component
- `code_agent` injects a ranked fix
- `model_router` demotes a backend

Payload: `{"system": "cortex|recall|router", "action": "...", "reason": "...", "input": {...}}`.

The web UI's Activity page already renders events; this gives
operators a single timeline of "decisions the system made on its
own" so it stays auditable.
