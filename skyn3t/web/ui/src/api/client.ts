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
  status: string;
  created_at?: number;
  completed_at?: number;
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
