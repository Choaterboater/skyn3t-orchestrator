// Thin API client over the FastAPI backend.
//
// Every endpoint is a one-line wrapper around fetch — TanStack Query
// owns retry/cache/refetch on top. We deliberately don't pull in axios
// or any other dep; fetch is enough.
//
// Auth: the backend uses a token from SKYN3T_WEB_TOKEN env. When
// unset (loopback dev), no token required. When set, the user navigates
// to /?token=… once, gets a session cookie, and subsequent requests
// authenticate automatically via cookie.

export type AgentRow = {
  name: string;
  agent_type?: string;
  provider?: string;
  backend?: string | null;
  model?: string | null;
  effective_backend?: string | null;
  effective_model?: string | null;
  effective_source?: string | null;
  status?: string;
  queue_depth?: number;
  recent_errors?: number;
};

export type AgentConfigView = {
  name: string;
  agent_type?: string;
  provider?: string;
  enabled?: boolean;
  capabilities?: string[];
  effective_backend?: string | null;
  effective_model?: string | null;
  effective_source?: string | null;
  effective_stage?: string | null;
  effective_tier?: string | null;
  effective_policy_source?: string | null;
  config?: {
    backend?: string;
    model?: string;
    system_prompt?: string;
    temperature?: number;
    max_tokens?: number;
  };
};

export type RoutingTier = {
  name: string;
  backend?: string | null;
  model?: string | null;
};

export type RoutingRoute = {
  stage: string;
  tier: string;
  backend?: string | null;
  model?: string | null;
  source: string;
  persisted_via?: string | null;
};

export type RoutingPolicyUpdate =
  | string
  | {
      tier: string;
      applied_via?: "manual" | "recommendation";
    };

export type RoutingPreset = {
  label: string;
  description?: string;
  policies: Record<string, string>;
};

export type ExecutionBackendView = {
  configured: string;
  resolved: string;
  resolved_class: string;
  docker_available: boolean;
  valid_backends: string[];
  auto_retry: boolean;
};

export type BudgetView = {
  daily_budget_usd: number;
  max_build_cost_usd: number;
  defaults: { daily_budget_usd: number; max_build_cost_usd: number };
};

export type RoutingView = {
  free_only: boolean;
  default: boolean;
};

export type RoutingTierModel = {
  tier: string;
  default_model?: string | null;
  override_model?: string | null;
  locked: boolean;
  effective_model?: string | null;
  backend?: string | null;
};

export type RoutingTiersView = {
  tiers: RoutingTierModel[];
  free_only: boolean;
};

export type RoutingRecommendation = {
  stage: string;
  current_tier: string;
  current_backend?: string | null;
  current_model?: string | null;
  current_source?: string | null;
  recommended_tier: string;
  recommended_backend?: string | null;
  recommended_model?: string | null;
  default_tier?: string | null;
  recommendation_kind: string;
  confidence: "low" | "medium" | "high";
  reasons: string[];
  signals: {
    live_stage_tokens?: number;
    trajectory_stage_tokens?: number;
    avg_latency_seconds?: number;
    trajectory_samples?: number;
    mixed_route_samples?: number;
    current_success_rate?: number | null;
    recommended_success_rate?: number | null;
  };
  applyable: boolean;
};

// Mutation responses from /api/agents/* endpoints. Backend signals
// partial failure by leaving `ok` true but populating `persisted=false`
// and/or `persist_error`. UI callers should surface these so partial
// outcomes aren't reported as full success.
export type AgentMutateResponse = {
  ok?: boolean;
  persisted?: boolean;
  persist_error?: string;
  [key: string]: unknown;
};

export type AgentDeleteResponse = {
  ok?: boolean;
  cleanup?: Record<string, boolean>;
  errors?: string[];
  persist_error?: string;
};

export type ProjectRow = {
  slug: string;
  title?: string;
  brief?: string;
  template?: string;
  status: string;
  next_action?: string;
  created_at?: number;
  started_at?: number;
  completed_at?: number;
  current_stage?: string | null;
  current_agent?: string | null;
  quality_summary?: {
    verdict?: string;
    score?: number;
    summary?: string;
  } | null;
  artifacts?: string[];
  build_verification?: {
    verdict?: string;
    stack?: string;
    summary?: string;
    command?: string;
  } | null;
};

export type ProjectDetail = ProjectRow & {
  stages?: Array<{
    name: string;
    agent?: string;
    capability?: string;
    expected_artifact?: string;
    status?: string;
    started_at?: number;
    completed_at?: number;
    summary?: string;
    files?: string[];
    error?: string;
  }>;
  history?: Array<{
    event: string;
    ts: number;
    message?: string;
    status?: string;
  }>;
  workflow_summary?: {
    title?: string;
    description?: string;
    agents?: string[];
  };
  clarification?: {
    asked_by?: string;
    questions?: string[];
    asked_at?: number;
  } | null;
};

export type Template = {
  key: string;
  title: string;
  description?: string;
  stages?: Array<{ name: string; agent: string }>;
};

export type Skill = {
  name: string;
  tags: string[];
  body: string;
  success_count: number;
  failure_count: number;
  score: number;
  source: string;
};

export type Proposal = {
  id: string;
  kind: string;
  title: string;
  summary: string;
  detail: string;
  payload?: Record<string, any>;
  source?: string;
  status: string;
  created_at: number;
  decided_at?: number | null;
  applied_at?: number | null;
  error?: string | null;
  requires_approval?: boolean;
  origin?: string;
};

export type CortexComponentStatus = {
  name: string;
  class_name: string;
  started: boolean;
  subscriptions: string[];
  creates_proposals: string[];
  handles_proposals: string[];
  details?: Record<string, unknown>;
  error?: string | null;
};

export type ScoutLastResult = {
  ok?: boolean;
  filed?: number;
  error?: string;
  warnings?: string[];
  proposals?: Array<{
    proposal_id?: string;
    kind?: string;
    repo?: string;
    lane?: string;
  }>;
};

export type GithubScoutStatus = {
  available: boolean;
  state?: string;
  busy_reason?: string | null;
  last_result?: ScoutLastResult;
};

export type GithubScoutConfig = {
  mode: string;
  default_limit: number;
  discovery_lanes: string[];
  summary?: string;
};

export type CortexStatus = {
  running: boolean;
  booted: boolean;
  components: CortexComponentStatus[];
  proposal_handlers: string[];
  proposal_counts: Record<string, number>;
  recent_failures: Array<{
    id: string;
    kind: string;
    title: string;
    error?: string | null;
  }>;
  warnings: string[];
};

export type BuildPatternStats = {
  stack: string;
  shape: string[];
  success: number;
  failure: number;
  skipped: number;
  last_seen_at: number;
};

export class HttpError extends Error {
  constructor(public status: number, public path: string, public body: string) {
    super(`${status} on ${path}: ${body.slice(0, 200)}`);
  }
}

// Token handling. The backend accepts the SKYN3T_WEB_TOKEN as either a
// session cookie (set by visiting /?token=…) or an Authorization: Bearer
// header. Vite dev runs on :5173 so the cookie path never fires — stash
// the token in localStorage and forward it on every request.
const TOKEN_KEY = "skyn3t_token";

(function bootstrapToken() {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  const t = url.searchParams.get("token");
  if (t) {
    try {
      localStorage.setItem(TOKEN_KEY, t);
    } catch {
      /* incognito with no storage */
    }
    url.searchParams.delete("token");
    window.history.replaceState({}, "", url.toString());
  }
})();

export function getAuthToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function clearAuthToken() {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* */
  }
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers ?? {});
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const token = getAuthToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const r = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers,
  });
  if (!r.ok) {
    const body = await r.text();
    throw new HttpError(r.status, path, body);
  }
  return (await r.json()) as T;
}

export type SystemStatus = {
  // /api/status returns the orchestrator's get_system_status() shape.
  // `agents` is a dict keyed by agent name — use `total_agents` for the count.
  running?: boolean;
  total_agents?: number;
  agents?: Record<string, unknown>;
  running_tasks?: number;
  completed_tasks?: number;
};

export type FleetSlotStatus = {
  slot_id: number;
  state: string;
  current_slug?: string | null;
  current_brief?: string | null;
  learning_kind?: string | null;
  tokens_today?: number;
  last_error?: string | null;
};

export type FleetStatus = {
  available?: boolean;
  running?: boolean;
  fleet_size?: number;
  configured_size?: number;
  active_builds?: number;
  active_learning?: number;
  daily_builds?: number;
  daily_cap?: number;
  queue_depth?: number;
  backpressure?: string | null;
  slots?: FleetSlotStatus[];
  error?: string;
};

export type AutonomousStatus = {
  available?: boolean;
  autonomous_learning?: boolean;
  autonomous_builds?: boolean;
  autonomous_proof_run?: boolean;
  daily_builds?: number;
  daily_cap?: number;
  daily_spend_usd?: number;
  daily_budget_usd?: number;
  queue_depth?: number;
  last_build_slug?: string | null;
  last_skip_reason?: string | null;
  last_proof_slug?: string | null;
  last_proof_ok?: boolean | null;
  last_proof_summary?: string | null;
  last_proof_at?: number;
  last_tick_at?: number;
  running?: boolean;
  scout_schedule?: Record<string, unknown>;
  error?: string;
};

export type OpenRouterCatalog = {
  synced_at?: number;
  model_count?: number;
  sync_enabled?: boolean;
  stale?: boolean;
  models?: Array<{ id?: string; name?: string; pricing?: Record<string, unknown> }>;
  tier_validation?: Record<string, unknown>;
  evolution?: {
    enabled?: boolean;
    last_run_at?: number;
    runs_total?: number;
    models_promoted?: number;
    models_demoted?: number;
    [key: string]: unknown;
  };
};

export type ImprovementStatus = {
  available?: boolean;
  enabled?: boolean;
  running?: boolean;
  last_tick_at?: number;
  ticks_total?: number;
  builds_today?: number;
  competitive_practice_today?: number;
  model_evolutions_total?: number;
  last_model_evolution_at?: number;
  last_model_sync_at?: number;
  cheaper_routing_applied?: number;
  autonomous_queue_depth?: number;
  autonomous_builds_enabled?: boolean;
  model_evolution?: Record<string, unknown>;
  error?: string;
};

// ---------------------------------------------------------------------------
// Phase 4 command-center additions (ADDITIVE — appended, existing wrappers
// untouched). consciousness/memory/meta/llm/agents/skills typed surfaces for
// the operator dashboard. consciousness + meta follow the fleetStatus 503 ->
// graceful {enabled:false} pattern.
// ---------------------------------------------------------------------------

export type ConsciousnessStatus = {
  enabled: boolean;
  working_memory_keys?: number;
  active_sessions?: number;
  total_insights?: number;
  agents_with_insights?: string[];
};

export type MemoryLayers = {
  enabled: boolean;
  layers: {
    session: { active_sessions: number; sessions: string[] };
    operator: {
      insight_count: number;
      recent_insights: Array<{
        agent?: string;
        capability?: string;
        insight?: string;
        timestamp?: number;
      }>;
      skill_summary: Record<string, any>;
      top_skills: Array<{
        name: string;
        score: number;
        tags: string[];
        source: string;
      }>;
    };
    project: {
      tasks: number;
      messages: number;
      knowledge_documents: number;
      success_rate: number;
      recent_documents: Array<{
        title?: string;
        source?: string;
        doc_type?: string;
        created_at?: number;
      }>;
    };
  };
};

export type MemoryInsight = {
  agent?: string;
  capability?: string;
  insight?: string;
  timestamp?: number;
  [k: string]: any;
};

export type MetaStatus = {
  enabled: boolean;
  running?: boolean;
  interval_seconds?: number;
  observations_collected?: number;
  actions_taken?: number;
  recent_actions?: Array<Record<string, any>>;
};

export type LlmBackends = { backends: string[] };

export type AgentTypes = { types: string[] };

export type SkillHubCatalog = {
  roots: string[];
  markdown_skills: string[];
  agent_skill_dirs: string[];
  total: number;
};

export type SkillInstallResult = {
  installed?: string | string[];
  installed_count?: number;
  warnings?: string[];
  error?: string;
  flagged?: string[];
  skipped_count?: number;
  [k: string]: any;
};

export type HubInstallResult = {
  installed?: string[];
  skipped?: string[];
  flagged?: string[];
  [k: string]: any;
};

export type UsageAgentRow = {
  agent: string;
  prompt_tokens: number;
  response_tokens: number;
  total_tokens: number;
  calls: number;
  last_used_at: number;
  backend?: string;
  model?: string;
};

// Alias so consumers can name the existing GithubScoutConfig surface
// uniformly without re-declaring it.
export type GithubScoutConfigFull = GithubScoutConfig;

// New skills surface (GET /api/skills) — distinct from the existing
// /api/memory/skills wrapped by api.skills.
export type SkillCatalogEntry = {
  name: string;
  slug: string;
  description?: string;
  tags: string[];
  score: number;
  success_count: number;
  failure_count: number;
  source: string;
  last_used_at?: number;
  created_at?: number;
};

export type SkillCatalog = {
  skills: SkillCatalogEntry[];
  summary: Record<string, any>;
};

export const api = {
  status: () => fetchJson<SystemStatus>("/api/status"),
  autonomousStatus: () =>
    fetchJson<AutonomousStatus>("/api/autonomous/status").catch((err) => {
      if (err instanceof HttpError && err.status === 503) {
        return { available: false } as AutonomousStatus;
      }
      throw err;
    }),
  fleetStatus: () =>
    fetchJson<FleetStatus>("/api/fleet/status").catch((err) => {
      if (err instanceof HttpError && err.status === 503) {
        return { available: false } as FleetStatus;
      }
      throw err;
    }),
  improvementStatus: () =>
    fetchJson<ImprovementStatus>("/api/improvement/status").catch((err) => {
      if (err instanceof HttpError && err.status === 503) {
        return { available: false } as ImprovementStatus;
      }
      throw err;
    }),
  openrouterModels: (refresh = false) =>
    fetchJson<OpenRouterCatalog>(
      `/api/models/openrouter${refresh ? "?refresh=1" : ""}`,
    ),
  swarmSnapshot: () => fetchJson<any>("/api/swarm/snapshot"),
  usageTotals: () =>
    fetchJson<{
      total_tokens: number;
      total_calls: number;
      agents_tracked: number;
      projects_tracked: number;
    }>("/api/usage/totals"),
  usagePerAgent: () =>
    fetchJson<{
      agents: Array<{
        agent: string;
        prompt_tokens: number;
        response_tokens: number;
        total_tokens: number;
        calls: number;
        last_used_at: number;
        backend?: string;
        model?: string;
      }>;
    }>("/api/usage/agents").then((d) => d.agents ?? []),
  usagePerProject: () =>
    fetchJson<{
      projects: Array<{
        slug: string;
        total_tokens: number;
        prompt_tokens: number;
        response_tokens: number;
        calls: number;
        last_used_at: number;
        estimated_cost_usd?: number;
        stages: Array<{
          stage: string;
          total_tokens: number;
          calls: number;
          by_agent: Record<string, number>;
        }>;
      }>;
    }>("/api/usage/projects").then((d) => d.projects ?? []),
  usageForProject: (slug: string) =>
    fetchJson<any>(`/api/usage/projects/${encodeURIComponent(slug)}`),
  agents: () =>
    fetchJson<{ agents: AgentRow[] }>("/api/agents").then((d) => d.agents ?? []),
  execAgent: (name: string, message: string) =>
    fetchJson<{ data?: { response?: string }; error?: string }>(
      `/api/agents/${encodeURIComponent(name)}/exec`,
      { method: "POST", body: JSON.stringify({ message }) },
    ),
  agentConfig: (name: string) =>
    fetchJson<AgentConfigView>(
      `/api/agents/${encodeURIComponent(name)}/config`,
    ),
  agentModels: () =>
    fetchJson<{
      backends: Array<{
        backend: string;
        models: Array<{ id: string; label: string }>;
      }>;
    }>("/api/agents/models"),
  patchAgentConfig: (name: string, patch: Record<string, unknown>) =>
    fetchJson<AgentMutateResponse>(
      `/api/agents/${encodeURIComponent(name)}/config`,
      { method: "PATCH", body: JSON.stringify(patch) },
    ),
  resetAgentConfig: (name: string, keys?: string[]) =>
    fetchJson<AgentMutateResponse>(
      `/api/agents/${encodeURIComponent(name)}/config/reset`,
      { method: "POST", body: JSON.stringify({ keys: keys ?? ["backend", "model"] }) },
    ),
  enableAgent: (name: string) =>
    fetchJson<AgentMutateResponse>(
      `/api/agents/${encodeURIComponent(name)}/enable`,
      { method: "POST", body: "{}" },
    ),
  disableAgent: (name: string) =>
    fetchJson<AgentMutateResponse>(
      `/api/agents/${encodeURIComponent(name)}/disable`,
      { method: "POST", body: "{}" },
    ),
  deleteAgent: (name: string) =>
    fetchJson<AgentDeleteResponse>(
      `/api/agents/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  routingPolicy: () =>
    fetchJson<{
      tiers: RoutingTier[];
      routes: RoutingRoute[];
      presets?: Record<string, RoutingPreset>;
    }>("/api/routing/policy"),
  routingRecommendations: () =>
    fetchJson<{ recommendations: RoutingRecommendation[] }>("/api/routing/recommendations")
      .then((d) => d.recommendations ?? []),
  patchRoutingPolicy: (policies: Record<string, RoutingPolicyUpdate>) =>
    fetchJson<{ ok?: boolean; tiers: RoutingTier[]; routes: RoutingRoute[] }>(
      "/api/routing/policy",
      { method: "PATCH", body: JSON.stringify({ policies }) },
    ),
  resetRoutingPolicy: (stage: string) =>
    fetchJson<{ ok?: boolean; tiers: RoutingTier[]; routes: RoutingRoute[] }>(
      `/api/routing/policy/${encodeURIComponent(stage)}`,
      { method: "DELETE" },
    ),
  applyStudioQualityRouting: () =>
    fetchJson<{ ok?: boolean; tiers: RoutingTier[]; routes: RoutingRoute[] }>(
      "/api/routing/presets/studio-quality",
      { method: "POST", body: "{}" },
    ),
  executionBackend: () =>
    fetchJson<ExecutionBackendView>("/api/execution/backend"),
  patchExecutionBackend: (backend: string) =>
    fetchJson<{ ok?: boolean; configured: string; resolved_class: string }>(
      "/api/execution/backend",
      { method: "PATCH", body: JSON.stringify({ backend }) },
    ),
  budget: () => fetchJson<BudgetView>("/api/budget"),
  patchBudget: (payload: {
    daily_budget_usd?: number;
    max_build_cost_usd?: number;
    reset?: boolean;
  }) =>
    fetchJson<BudgetView & { ok?: boolean; reset?: boolean }>("/api/budget", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  routing: () => fetchJson<RoutingView>("/api/routing"),
  patchRouting: (payload: { free_only: boolean }) =>
    fetchJson<RoutingView & { ok?: boolean }>("/api/routing", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  routingTiers: () => fetchJson<RoutingTiersView>("/api/routing/tiers"),
  patchRoutingTier: (tier: string, model: string) =>
    fetchJson<RoutingTiersView>("/api/routing/tiers", {
      method: "PATCH",
      body: JSON.stringify({ tier, model }),
    }),
  resetRoutingTier: (tier?: string) =>
    fetchJson<RoutingTiersView>("/api/routing/tiers/reset", {
      method: "POST",
      body: JSON.stringify(tier ? { tier } : {}),
    }),
  projects: () =>
    fetchJson<{ projects: ProjectRow[] }>("/api/studio/projects").then(
      (d) => d.projects ?? [],
    ),
  project: (slug: string) =>
    fetchJson<ProjectDetail>(`/api/studio/projects/${encodeURIComponent(slug)}`),
  deleteProject: (slug: string) =>
    fetchJson<{ ok: boolean }>(`/api/studio/projects/${encodeURIComponent(slug)}`, {
      method: "DELETE",
    }),
  cancelProject: (slug: string) =>
    fetchJson<{ ok: boolean; cancelled?: boolean }>(
      `/api/studio/projects/${encodeURIComponent(slug)}/cancel`,
      { method: "POST" },
    ),
  // Approval gate (architect handoff): fetch architecture.md as plain
  // text, then approve / approve-with-edits / reject.
  fetchArchitecture: (slug: string) =>
    fetch(
      `/api/studio/projects/${encodeURIComponent(slug)}/file?path=architecture.md`,
    ).then((r) =>
      r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`)),
    ),
  approveProject: (slug: string) =>
    fetchJson<{ ok: boolean }>(
      `/api/studio/projects/${encodeURIComponent(slug)}/approve`,
      { method: "POST", body: "{}" },
    ),
  approveProjectWithEdits: (slug: string, content: string) =>
    fetchJson<{ ok: boolean }>(
      `/api/studio/projects/${encodeURIComponent(slug)}/approve-with-edits`,
      { method: "POST", body: JSON.stringify({ content }) },
    ),
  rejectProject: (slug: string, feedback: string) =>
    fetchJson<{ ok: boolean }>(
      `/api/studio/projects/${encodeURIComponent(slug)}/reject`,
      { method: "POST", body: JSON.stringify({ feedback }) },
    ),
  feedbackProject: (slug: string, helpful: boolean) =>
    fetchJson<{ ok: boolean; credited?: number }>(
      `/api/studio/projects/${encodeURIComponent(slug)}/feedback`,
      { method: "POST", body: JSON.stringify({ helpful }) },
    ),
  clarifyProject: (slug: string, answers: string[]) =>
    fetchJson<{ ok: boolean; resuming?: string; answer_count?: number }>(
      `/api/studio/projects/${encodeURIComponent(slug)}/clarify`,
      { method: "POST", body: JSON.stringify({ answers }) },
    ),
  templates: () =>
    fetchJson<{ templates: Template[]; mission_setup?: any }>("/api/studio/templates"),
  startStudio: (payload: {
    template: string;
    brief?: string;
    slug?: string;
    mission_setup?: Record<string, unknown>;
    repo_target?: Record<string, unknown>;
  }) =>
    fetchJson<{
      accepted?: boolean;
      slug?: string;
      template?: string;
      title?: string;
      status?: string;
      next_action?: string;
      error?: string;
    }>("/api/studio/start", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  proposals: (filter?: { status?: string; origin?: string }) => {
    const qs = new URLSearchParams();
    if (filter?.status) qs.set("status", filter.status);
    if (filter?.origin) qs.set("origin", filter.origin);
    const q = qs.toString();
    return fetchJson<{ proposals: Proposal[] }>(
      `/api/proposals${q ? `?${q}` : ""}`,
    ).then((d) => d.proposals ?? []);
  },
  approveProposal: (id: string) =>
    fetchJson<{ ok?: boolean; error?: string }>(
      `/api/proposals/${encodeURIComponent(id)}/approve`,
      { method: "POST", body: "{}" },
    ),
  rejectProposal: (id: string, reason?: string) =>
    fetchJson<{ ok?: boolean; error?: string }>(
      `/api/proposals/${encodeURIComponent(id)}/reject`,
      { method: "POST", body: JSON.stringify({ reason: reason ?? "" }) },
    ),
  fileFeatureIdea: (idea: string) =>
    fetchJson<{ ok?: boolean; proposal_id?: string; error?: string }>(
      "/api/proposals/feature",
      { method: "POST", body: JSON.stringify({ idea }) },
    ),
  previewFeatureIdea: (idea: string) =>
    fetchJson<{
      idea: string;
      target_file: string;
      keywords: string[];
      capability_hits: Array<{ keywords: string[]; related_files: string[] }>;
      next_action: {
        kind: "self_patch" | "blocked";
        summary: string;
        target_file?: string;
        agent: string | null;
      };
      would_create: {
        kind: string;
        origin: string;
        status: string;
        requires_approval: boolean;
      };
      error?: string;
    }>("/api/proposals/feature/preview", {
      method: "POST",
      body: JSON.stringify({ idea }),
    }),
  cortexStatus: () => fetchJson<CortexStatus>("/api/cortex/status"),
  githubScoutStatus: () => fetchJson<GithubScoutStatus>("/api/github/scout/status"),
  githubScoutConfig: () => fetchJson<GithubScoutConfig>("/api/github/scout/config"),
  runGithubScout: (payload: { limit?: number; queries?: string[] }) =>
    fetchJson<{ ok?: boolean; started?: boolean; state?: string; error?: string }>(
      "/api/github/scout/run",
      { method: "POST", body: JSON.stringify(payload) },
    ),
  traces: (limit = 50) =>
    fetchJson<{ traces: any[] }>(`/traces?limit=${limit}`).then(
      (d) => d.traces ?? [],
    ),
  ragStats: () => fetchJson<any>("/api/rag/stats"),
  ragRecent: (limit = 20) =>
    fetchJson<{ documents: any[] }>(`/api/rag/recent?limit=${limit}`).then(
      (d) => d.documents ?? [],
    ),
  ragQuery: (query: string, n_results = 5) =>
    fetchJson<any>("/api/rag/query", {
      method: "POST",
      body: JSON.stringify({ query, n_results }),
    }),
  ragAdd: (payload: {
    content: string;
    title?: string;
    source?: string;
    doc_type?: string;
  }) =>
    fetchJson<any>("/api/rag/add", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  skills: (tag?: string) => {
    const q = tag ? `?tag=${encodeURIComponent(tag)}` : "";
    return fetchJson<{ summary?: any; top?: Skill[]; skills?: Skill[] }>(
      `/api/memory/skills${q}`,
    );
  },
  buildPatterns: (stack?: string) => {
    const q = stack ? `?stack=${encodeURIComponent(stack)}` : "";
    return fetchJson<{
      summary?: any;
      per_stack?: Record<string, { best: BuildPatternStats | null; worst: BuildPatternStats | null }>;
      shapes?: BuildPatternStats[];
    }>(`/api/memory/build_patterns${q}`).catch((err) => {
      // Older backend builds didn't expose this endpoint. Treat 404 as
      // "no data" so the dashboard tile stays calm.
      if (err instanceof HttpError && err.status === 404) {
        return { summary: {}, per_stack: {}, shapes: [] };
      }
      throw err;
    });
  },

  // --- Phase 4 command-center methods (additive) -------------------------

  // Consciousness + meta follow fleetStatus's 503 -> graceful pattern: a
  // disabled subsystem must not surface as a hard error in the UI.
  consciousnessStatus: () =>
    fetchJson<ConsciousnessStatus>("/api/consciousness/status").catch((err) => {
      if (err instanceof HttpError && err.status === 503) {
        return { enabled: false } as ConsciousnessStatus;
      }
      throw err;
    }),
  memoryLayers: (limit?: number) => {
    const q = typeof limit === "number" ? `?limit=${limit}` : "";
    return fetchJson<MemoryLayers>(`/api/memory/layers${q}`).catch((err) => {
      if (err instanceof HttpError && err.status === 503) {
        return {
          enabled: false,
          layers: {
            session: { active_sessions: 0, sessions: [] },
            operator: {
              insight_count: 0,
              recent_insights: [],
              skill_summary: {},
              top_skills: [],
            },
            project: {
              tasks: 0,
              messages: 0,
              knowledge_documents: 0,
              success_rate: 0,
              recent_documents: [],
            },
          },
        } as MemoryLayers;
      }
      throw err;
    });
  },
  memoryInsights: (opts?: {
    agent?: string;
    capability?: string;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    if (opts?.agent) qs.set("agent", opts.agent);
    if (opts?.capability) qs.set("capability", opts.capability);
    if (typeof opts?.limit === "number") qs.set("limit", String(opts.limit));
    const q = qs.toString();
    return fetchJson<{ insights: MemoryInsight[] }>(
      `/api/memory/insights${q ? `?${q}` : ""}`,
    ).then((d) => d.insights ?? []);
  },
  metaStatus: () =>
    fetchJson<MetaStatus>("/api/meta/status").catch((err) => {
      if (err instanceof HttpError && err.status === 503) {
        return { enabled: false } as MetaStatus;
      }
      throw err;
    }),
  pauseMeta: () =>
    fetchJson<{ status?: string; error?: string }>("/api/meta/pause", {
      method: "POST",
      body: "{}",
    }),
  resumeMeta: () =>
    fetchJson<{ status?: string; error?: string }>("/api/meta/resume", {
      method: "POST",
      body: "{}",
    }),
  llmBackends: () =>
    fetchJson<LlmBackends>("/api/llm/backends").then((d) => d.backends ?? []),
  agentTypes: () =>
    fetchJson<AgentTypes>("/api/agents/types").then((d) => d.types ?? []),
  createAgent: (payload: {
    name: string;
    base_type?: string;
    [k: string]: unknown;
  }) =>
    fetchJson<{ ok?: boolean; name?: string; error?: string }>(
      "/api/agents/create",
      { method: "POST", body: JSON.stringify(payload) },
    ),
  skillsList: (tag?: string) => {
    const q = tag ? `?tag=${encodeURIComponent(tag)}` : "";
    return fetchJson<SkillCatalog>(`/api/skills${q}`);
  },
  skillsHub: () => fetchJson<SkillHubCatalog>("/api/skills/hub"),
  installSkill: (source: string) =>
    fetchJson<SkillInstallResult>("/api/skills/install", {
      method: "POST",
      body: JSON.stringify({ source }),
    }),
  installFromHub: (onlyMissing?: boolean) =>
    fetchJson<HubInstallResult>("/api/skills/hub/install", {
      method: "POST",
      body: JSON.stringify({ only_missing: onlyMissing ?? false }),
    }),
  deleteSkill: (name: string) =>
    fetchJson<{ ok?: boolean; error?: string }>(
      `/api/skills/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  usageAgents: () =>
    fetchJson<{ agents: UsageAgentRow[] }>("/api/usage/agents").then(
      (d) => d.agents ?? [],
    ),
};
