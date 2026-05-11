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
  status?: string;
  queue_depth?: number;
  recent_errors?: number;
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

export type BuildPatternStats = {
  stack: string;
  shape: string[];
  success: number;
  failure: number;
  skipped: number;
  last_seen_at: number;
};

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...init,
  });
  if (!r.ok) {
    const body = await r.text();
    throw new Error(`${r.status} ${r.statusText} on ${path}: ${body.slice(0, 200)}`);
  }
  return (await r.json()) as T;
}

export const api = {
  status: () => fetchJson<{ agents: number; running_tasks: number }>("/api/status"),
  agents: () =>
    fetchJson<{ agents: AgentRow[] }>("/api/agents").then((d) => d.agents ?? []),
  execAgent: (name: string, message: string) =>
    fetchJson<{ data?: { response?: string }; error?: string }>(
      `/api/agents/${encodeURIComponent(name)}/exec`,
      { method: "POST", body: JSON.stringify({ message }) },
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
    }>(`/api/memory/build_patterns${q}`);
  },
};
