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
  config?: {
    backend?: string;
    model?: string;
    system_prompt?: string;
    temperature?: number;
    max_tokens?: number;
  };
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

export const api = {
  status: () => fetchJson<SystemStatus>("/api/status"),
  swarmSnapshot: () => fetchJson<any>("/api/swarm/snapshot"),
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
    fetchJson<any>(`/api/agents/${encodeURIComponent(name)}/config`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  enableAgent: (name: string) =>
    fetchJson<{ ok?: boolean }>(
      `/api/agents/${encodeURIComponent(name)}/enable`,
      { method: "POST", body: "{}" },
    ),
  disableAgent: (name: string) =>
    fetchJson<{ ok?: boolean }>(
      `/api/agents/${encodeURIComponent(name)}/disable`,
      { method: "POST", body: "{}" },
    ),
  deleteAgent: (name: string) =>
    fetchJson<{ ok?: boolean }>(
      `/api/agents/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
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
};
